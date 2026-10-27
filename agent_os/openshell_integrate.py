"""`aos integrate` on the OpenShell backend (design: docs/design/2026-10-13-integrate-openshell.md).

The merged candidate's acceptance commands and the repository's test command run in a fresh, credential-free
OpenShell sandbox under the repository's acceptance policy, with a disposable PostgreSQL of their own when the
repository requests one: the operation a run's fresh acceptance uses, not a second verification pipeline.

The adapter's module attributes (`openshell`, `ensure_free`, `delete_sandbox`, `accept_fresh`, `provision_job`,
`render_acceptance`, `remove_scratch`) are looked up at call time, so the test fake patched onto
`agent_os.openshell_runner` applies here as it does for runs.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import psutil

from agent_os import openshell_policy, openshell_runner, openshell_services, resources
from agent_os.resources import ServiceError

KIND = "integrate"
SUFFIX = "-int"
CLEANUP_FAILED = "cleanup failed; `reconcile` retries it; the verdict is unchanged"


def attempt_id(task_id: str, prefix: str) -> str:
    """The per-attempt id, `<prefix>-integrate-<task>`: the database container's name and label, carrying the bus
    prefix so another bus's attempt of the same task is never swept. The host path's `RunResources` keeps the bare
    `integrate-<task>`, so the two backends no longer sweep each other's leftovers."""
    return f"{prefix}-integrate-{task_id}"


def sandbox_name(task_id: str, prefix: str) -> str:
    """`<prefix>-<task slug>-int`; run sandboxes carry a run id (`<prefix>-ao-12-r3`, `-acc`), so the names never meet."""
    name = f"{prefix}-" + re.sub(r"[^a-z0-9-]", "-", task_id.lower()).strip("-") + SUFFIX
    if len(name) > openshell_runner.MAX_SANDBOX_NAME:
        raise openshell_runner.AdapterError(
            f"task id {task_id!r} yields a sandbox name longer than {openshell_runner.MAX_SANDBOX_NAME} characters")
    return name


def write_owner(state: Path, *, task_id: str, run_id: str, name: str, host: str, bus: str, prefix: str) -> None:
    """The run marker's fields plus the attempt's kind and id and the integrate process's identity."""
    process = psutil.Process()
    openshell_runner._write_private(state / openshell_runner.OWNER_FILE, json.dumps({
        "run_id": run_id, "task_id": task_id, "sandboxes": [name], "host": host,
        "worker": socket.gethostname(), "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "bus": bus, "prefix": prefix,
        "kind": KIND, "attempt": attempt_id(task_id, prefix), "pid": process.pid, "pid_created": process.create_time(),
    }).encode())


def attempt_alive(owner: dict[str, Any]) -> bool:
    """True while a process with the marker's pid and create time exists on this host. A marker without a
    usable process identity and a process psutil cannot read both count as alive: `reconcile` then leaves the
    attempt alone rather than deleting a sandbox that may be in use."""
    try:
        pid, created = int(owner["pid"]), float(owner["pid_created"])
    except (KeyError, TypeError, ValueError):
        return True
    if pid <= 0:
        return True
    try:
        return abs(psutil.Process(pid).create_time() - created) < 1.0
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return True


def _lock_state(directory: Path) -> IO[str]:
    """A stable, local lifecycle lock, outside the scratch that cleanup removes."""
    directory = directory.expanduser().resolve()
    return resources.lock_worktree(str(directory), str(directory.parent / ".integrate-locks"))


def _remove_scratch(directory: Path) -> None:
    openshell_runner.remove_scratch(directory)
    remaining = [name for name in openshell_runner.SCRATCH_FILES if (directory / name).exists()]
    if remaining:
        raise OSError(f"scratch files remain: {', '.join(remaining)}")


def reconcile_attempt(directory: Path, owner: dict[str, Any], err: IO[str], prefix: str) -> int:
    """Recover a dead integrate attempt under its lifecycle lock. The caller's bus/host-checked marker
    snapshot must still match under the lock; an active owner or uncertain cleanup leaves the marker."""
    try:
        ownership = _lock_state(directory)
    except BlockingIOError:
        return 0
    with ownership:
        marker = directory / openshell_runner.OWNER_FILE
        try:
            current = json.loads(marker.read_text())
        except (OSError, ValueError):
            return 0
        if current != owner or attempt_alive(current):
            return 0
        attempt = current.get("attempt") or attempt_id(current["task_id"], prefix)
        try:
            for sandbox in current["sandboxes"]:
                openshell_runner.delete_sandbox(sandbox)
        except (openshell_runner.AdapterError, subprocess.SubprocessError, OSError) as exc:
            print(f"reconcile: {directory.name} not confirmed deleted: {exc}", file=err)
            return 0
        services = directory / openshell_runner.SERVICES_FILE
        if services.is_file():
            try:
                openshell_services.stop(attempt, os.environ)
                services.unlink(missing_ok=True)
            except (ServiceError, OSError, subprocess.SubprocessError) as exc:
                print(f"reconcile: {directory.name} database not confirmed removed: {exc}", file=err)
                return 0
        try:
            _remove_scratch(directory)
            marker.unlink()
        except OSError as exc:
            print(f"reconcile: {directory.name} cleanup not confirmed: {exc}", file=err)
            return 0
        print(f"reconcile: deleted the sandbox of {attempt} (its process is gone)", file=err)
        return 1


class Attempt:
    """One integrate attempt on the OpenShell backend. `check` creates the resources and returns the verdict
    with them still live; the caller posts it first, then calls `cleanup`."""

    def __init__(self, settings: openshell_runner.Settings, plan: openshell_services.Plan | None,
                 repo_policy: openshell_policy.PolicyV2, *, task_id: str, run_id: str,
                 environ: dict[str, str]) -> None:
        self.settings, self.plan, self.repo_policy = settings, plan, repo_policy
        self.task_id, self.run_id, self.environ = task_id, run_id, environ
        self.attempt = attempt_id(task_id, settings.sandbox_prefix)
        self.name: str | None = None
        self.state: Path | None = None
        self.database: openshell_services.Database | None = None
        self.database_owned = False  # services.json is on disk: the container may exist
        self.created = False  # a create was attempted on a confirmed-free name: ours to delete
        self.env_path: Path | None = None
        self.cleanup_failed = False
        self._ownership_lock: IO[str] | None = None

    def redact(self, text: str) -> str:
        return self.database.redact(text) if self.database is not None else text

    def check(self, scratch: str, merge_sha: str, commands: list[str]) -> tuple[str, str]:
        """(status, output): `passed` when every command exited 0, `failed` otherwise, `error` with the reason
        when no verdict was reached. Raises nothing of its own; a KeyboardInterrupt (the caller's SIGTERM
        handler) passes through."""
        try:
            passed, output = self._run(scratch, merge_sha, commands)
        except TimeoutError as exc:
            return "error", self.redact(f"acceptance did not complete: {exc}")
        except Exception as exc:  # noqa: BLE001 -- every refusal or failure of the attempt is an `error` verdict
            return "error", self.redact(str(exc) or type(exc).__name__)
        return ("passed" if passed == len(commands) else "failed"), self.redact(output)

    def _run(self, scratch: str, merge_sha: str, commands: list[str]) -> tuple[int, str]:
        settings = self.settings
        name = sandbox_name(self.task_id, settings.sandbox_prefix)
        state = Path(settings.state_dir).expanduser().resolve() / name
        marker = state / openshell_runner.OWNER_FILE
        try:
            self._ownership_lock = _lock_state(state)
        except BlockingIOError as exc:
            raise openshell_runner.AdapterError(
                f"an integrate attempt of {self.task_id} currently owns {name}; retry after its cleanup") from exc
        try:
            if marker.exists():  # a live or unconfirmed earlier attempt: never adopt its resources
                raise openshell_runner.AdapterError(
                    f"an earlier integrate attempt of {self.task_id} still owns {name} (marker {marker}); "
                    "`reconcile` retries its cleanup once its process is gone; delete the marker by hand if "
                    "reconcile keeps skipping it (an unreadable marker, a worker name that no longer matches this host, "
                    "a marker of another bus, or a legacy marker without a bus field)")
            self.name, self.state = name, state
            self.cleanup_failed = False
            state.mkdir(parents=True, exist_ok=True)
            os.chmod(state, 0o700)
            # The marker first: everything created after it is reachable by reconcile.
            write_owner(state, task_id=self.task_id, run_id=self.run_id, name=name, host=settings.host,
                        bus=settings.bus_id, prefix=settings.sandbox_prefix)
        except BaseException:
            if self.state is None:  # refusal before ownership: cleanup must not touch the earlier attempt
                self._release_ownership()
            raise
        services: tuple[tuple[str, int], ...] = ()
        if self.plan is not None:
            openshell_runner._write_private(state / openshell_runner.SERVICES_FILE,
                                            json.dumps({"postgres": True}).encode())
            self.database_owned = True
            openshell_services.stop(self.attempt, self.environ)  # an earlier attempt of this task that died before its cleanup
            self.database = openshell_services.start(self.plan, self.attempt, self.environ)
            services = self.database.grants
        rendered = openshell_runner.render_acceptance(settings, self.repo_policy, services=services)
        policy_path = state / "policy.yaml"
        policy_path.write_text(rendered)
        tarball = str(state / "job.tgz")
        openshell_runner.provision_job(scratch, merge_sha, f"integrate/{self.task_id}",
                                       openshell_policy.render_exclusions(self.repo_policy), tarball)
        openshell_runner.ensure_free(name)
        env_file = None
        if self.database is not None:
            self.env_path = state / "services.env"
            openshell_runner._write_private(self.env_path, self.database.env_file_text().encode())
            env_file = str(self.env_path)
        self.created = True
        try:
            return openshell_runner.accept_fresh(settings, name, str(policy_path), rendered, tarball, commands,
                                                 env_file=env_file)
        finally:
            if self.env_path is not None:  # the password file does not outlive the upload
                try:
                    self.env_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _release_ownership(self) -> None:
        ownership, self._ownership_lock = self._ownership_lock, None
        self.name, self.state, self.env_path = None, None, None
        self.created, self.database_owned = False, False
        self.database = None
        if ownership is not None:
            ownership.close()

    def cleanup(self, err: IO[str]) -> list[str]:
        """After the verdict is posted. Failed cleanup retains the recovery marker, but always ends this
        object's ownership. Repeated cleanup cannot touch a successor, even if recovery was incomplete."""
        if self._ownership_lock is None:
            return []
        lines: list[str] = []
        failed = False
        try:
            if self.env_path is not None:
                try:
                    self.env_path.unlink(missing_ok=True)
                except OSError:
                    pass  # scratch cleanup retries it and confirms its removal below
            if self.created and self.name is not None:
                try:
                    gone = openshell_runner.delete_sandbox(self.name)
                    lines.append(f"sandbox {self.name} deleted after {gone:.1f} s")
                except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
                    failed = True
                    lines.append(f"sandbox {self.name} cleanup failed: {self.redact(str(exc))[:300]}")
            if self.database_owned and self.state is not None:
                try:
                    openshell_services.stop(self.attempt, self.environ)
                    lines.append("postgres container removed")
                    (self.state / openshell_runner.SERVICES_FILE).unlink(missing_ok=True)
                except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
                    failed = True
                    lines.append(f"postgres cleanup failed: {self.redact(str(exc))[:300]}")
            if self.state is not None:
                try:
                    _remove_scratch(self.state)
                    if not failed:
                        (self.state / openshell_runner.OWNER_FILE).unlink(missing_ok=True)
                except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
                    failed = True
                    lines.append(f"scratch cleanup failed: {self.redact(str(exc))[:300]}")
            self.cleanup_failed = failed
            if failed:
                print(CLEANUP_FAILED, file=err)
            return lines
        finally:
            self._release_ownership()
