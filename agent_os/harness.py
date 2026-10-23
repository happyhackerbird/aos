"""Harness-process and acceptance helpers shared by the OpenShell runner, `aos integrate` and the
live sessions (moved out of the retired host runner on 2026-10-13, MVP decision 6): the runner
environment, the project verification-skill pointer, harness output parsing, process-group
spawn and kill, the acceptance environment scrub, the lease heartbeat and the acceptance loop.
"""
from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from agent_os import policy

if TYPE_CHECKING:
    from agent_os.resources import RunResources

ACCEPTANCE_CAP = 20000


# -- environment -------------------------------------------------------------

@dataclasses.dataclass
class RunnerEnv:
    repo_root: str
    worktree_root: str
    claude_bin: str
    resource_root: str = ""


def env_from(environ: dict[str, str]) -> RunnerEnv:
    return RunnerEnv(
        repo_root=os.path.expanduser(environ.get("AOS_REPO_ROOT", "~/code")),
        worktree_root=os.path.expanduser(environ.get("AOS_WORKTREE_ROOT", "~/.aos/worktrees")),
        claude_bin=os.path.expanduser(environ.get("AOS_CLAUDE_BIN", "claude")),
        resource_root=os.path.realpath(os.path.expanduser(environ.get("AOS_RESOURCE_ROOT") or str(Path(os.path.expanduser(
            environ.get("AOS_WORKTREE_ROOT", "~/.aos/worktrees"))).parent / "resources"))),
    )


# -- project verification skill ---------------------------------------------------------

VERIFY_SKILL_PATH = ".claude/skills/verify/SKILL.md"
VERIFY_SKILL_LINE = (
    f"Project verification: `{VERIFY_SKILL_PATH}` in the worktree says how to launch, check, drive "
    "and prove this app and where evidence goes. Read it before you change behaviour and follow its "
    "Launch, Doctor, Drive, Evidence and Cleanup sections; name the evidence directory in your report."
)


def verify_skill_on_default(source: str, default: policy.Default) -> bool:
    """True when the repository's default-branch commit carries the project verification skill."""
    if default.state == "empty":
        return False
    try:
        _, listing = policy.git_output(source, "ls-tree", "--full-tree", default.sha, "--", VERIFY_SKILL_PATH)
    except policy.PolicyError:
        return False
    fields = listing.split(b"\t", 1)[0].split()
    return len(fields) >= 2 and fields[1] == b"blob"


# -- harness output -------------------------------------------------------------------

def parse_harness_output(raw: bytes, harness: str) -> tuple[str | None, str | None]:
    """Return the final message and session id from the selected CLI format.

    Claude's json form accepts a single object with any formatting, as before; a stream-json transcript
    supplies its LAST result line. A document or line whose decode raises RecursionError is unparseable.
    """
    if harness == "claude":
        try:
            parsed = json.loads(raw.decode(errors="replace"))
        except (ValueError, RecursionError):
            result = session = None
            for line in reversed(raw.splitlines()):
                try:
                    parsed = json.loads(line.decode(errors="replace"))
                except (ValueError, RecursionError):
                    continue
                if isinstance(parsed, dict) and parsed.get("type") == "result":
                    result = parsed.get("result")
                    session = parsed.get("session_id")
                    break
        else:
            if not isinstance(parsed, dict):
                return None, None
            result = parsed.get("result")
            session = parsed.get("session_id")
    else:
        result = session = None
        completed = False
        for line in raw.splitlines():
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
            if event.get("type") == "thread.started":
                session = event.get("thread_id")
            elif event.get("type") == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    result = item.get("text")
            elif event.get("type") == "turn.completed":
                completed = True
            elif event.get("method") == "item/completed":
                item = event.get("params", {}).get("item")
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    result = item.get("text")
            elif event.get("method") == "turn/completed":
                completed = True
        if not completed:
            result = None
    return result if isinstance(result, str) else None, session if isinstance(session, str) else None


# -- process handling ------------------------------------------------------

def spawn(cmd: list[str], *, cwd: str, env: dict[str, str], stdin: Any, stdout: Any,
          stderr: Any) -> subprocess.Popen:
    return subprocess.Popen(cmd, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
                            start_new_session=True)


def kill_group(proc: subprocess.Popen) -> str | None:
    """Signal the group on clean exits too; detached resources need run cleanup.

    Never raises: an OSError other than ESRCH (AO-4 r2, 2026-10-12: EPERM from a
    nested macOS sandbox) comes back as a one-line description for the cleanup
    events, so the caller can still post its result and close its ledger.

    A resolved group <= 1 or the caller's own group is refused before any signal
    or wait, and never retargeted: a test passing pid 1 here signalled the host's
    real process groups and took the host down (2026-10, AO-21).
    """
    try:
        pgid = os.getpgid(proc.pid) if proc.poll() is None else proc.pid
    except OSError:
        pgid = proc.pid
    if pgid <= 1:
        return f"process group {pgid} cleanup refused: group id <= 1 is never signalled"
    if pgid == os.getpgrp():
        return f"process group {pgid} cleanup refused: it is the caller's own process group"
    failures: list[str] = []

    def signal_group(sig: int) -> None:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        except OSError as exc:
            failures.append(f"{type(exc).__name__}: {exc}")

    signal_group(signal.SIGTERM)
    if proc.poll() is None:
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            pass
    signal_group(signal.SIGKILL)
    if failures:
        return f"process group {pgid} cleanup failed: " + "; ".join(dict.fromkeys(failures))
    return None


# -- acceptance environment --------------------------------------------------

# Host-only variables that must not reach acceptance commands: the four GIT_* ones redirect every git
# the commands run (AO-4's hook test inherited GIT_DIR/GIT_INDEX_FILE and committed into the task
# branch); the rest leak the operator's Node/Python setup into what is meant to be the project's check.
HOST_ONLY_ENV = ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_COMMON_DIR", "NODE_EXTRA_CA_CERTS",
                 "NODE_OPTIONS", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "VIRTUAL_ENV")


def acceptance_env(base: dict[str, str], extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of every acceptance command: `base` without HOST_ONLY_ENV, `<VIRTUAL_ENV>/bin`
    taken off PATH, then `extra` (the runner's own additions such as CODEX_HOME) applied untouched.
    The run tags (AOS_RUN_ID, AOS_WORKTREE, COMPOSE_PROJECT_NAME, AOS_RESOURCE_ROOT, DATABASE_URL)
    are part of `base` and stay. One function owns this scrub (shared with MVP item 3)."""
    env = {key: value for key, value in base.items() if key not in HOST_ONLY_ENV}
    venv = base.get("VIRTUAL_ENV")
    if venv and "PATH" in env:
        venv_bin = os.path.normpath(os.path.join(venv, "bin"))
        env["PATH"] = os.pathsep.join(entry for entry in env["PATH"].split(os.pathsep)
                                      if os.path.normpath(entry) != venv_bin)
    env.update(extra or {})
    return env


# -- heartbeat -----------------------------------------------------------

class Heartbeat(threading.Thread):
    def __init__(self, client: httpx.Client, lock: threading.Lock, run_id: str, claim_token: str,
                 lease_until: datetime) -> None:
        super().__init__(daemon=True)
        self.client = client
        self.lock = lock
        self.run_id = run_id
        self.claim_token = claim_token
        self.lease_until = lease_until
        self.stop_event = threading.Event()
        self.foreign = threading.Event()
        self.foreign_status: str | None = None
        self._failures = 0

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            now = datetime.now(timezone.utc)
            wait_s = max(1.0, (self.lease_until - now).total_seconds() / 2)
            if self.stop_event.wait(wait_s):
                return
            try:
                with self.lock:
                    response = self.client.post(f"/runs/{self.run_id}/heartbeat",
                                                json={"claim_token": self.claim_token})
                if 400 <= response.status_code < 500:
                    self.foreign_status = f"http {response.status_code}"
                    self.foreign.set()
                    return
                if response.status_code >= 500:
                    response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("status"), str):
                    raise ValueError("invalid heartbeat status")
                status = data["status"]
                if status in ("claimed", "running"):
                    lease = data.get("lease_until")
                    if not isinstance(lease, str):
                        raise ValueError("invalid heartbeat lease")
                    lease_until = datetime.fromisoformat(lease.replace("Z", "+00:00"))
                    if lease_until.tzinfo is None:
                        raise ValueError("heartbeat lease must include a timezone")
            except (httpx.HTTPError, ValueError):
                self._failures += 1
                if self._failures >= 3:
                    self.foreign_status = "unreachable"
                    self.foreign.set()
                    return
                continue
            self._failures = 0
            if status in ("claimed", "running"):
                self.lease_until = lease_until
                continue
            self.foreign_status = status
            self.foreign.set()
            return


# -- acceptance ------------------------------------------------------------

def run_acceptance(commands: list[str], worktree: str, timeout: int, foreign: threading.Event,
                   env: dict[str, str], resources: RunResources | None = None,
                   prefix: list[str] | None = None, extra_env: dict[str, str] | None = None,
                   failed_output: list[str] | None = None,
                   results: list[dict[str, Any]] | None = None) -> tuple[int, int, str, bool]:
    passed = 0
    total = len(commands)
    parts: list[str] = []
    foreign_seen = False
    for command in commands:
        if foreign.is_set():
            foreign_seen = True
            break
        proc = spawn([*(prefix or []), "sh", "-c", command], cwd=worktree, env=acceptance_env(env, extra_env),
                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if resources is not None:
            resources.register(proc.pid)
        deadline = time.monotonic() + timeout
        raw_out = b""
        timed_out = False
        try:
            while True:
                if foreign.is_set():
                    foreign_seen = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    raw_out, _ = proc.communicate(timeout=min(1, remaining))
                    break
                except subprocess.TimeoutExpired as exc:
                    raw_out = exc.output or b""
        finally:
            if resources is not None:
                resources.snapshot_descendants()
            failure = kill_group(proc)
            if resources is not None:
                resources.events.append(failure or f"cleaned acceptance process group {proc.pid}")
            if proc.stdout is not None:
                proc.stdout.close()
        output = (raw_out or b"").decode(errors="replace")
        if timed_out:
            parts.append(f"$ {command}\n{output}\ntimeout after {timeout} s\n")
            if results is not None:
                results.append({"command": command, "exit": None, "note": f"timeout after {timeout} s",
                                "output": output})
            break
        if foreign_seen:
            parts.append(f"$ {command}\n{output}\nrun ended on bus\n")
            if results is not None:
                results.append({"command": command, "exit": None, "note": "run ended on bus", "output": output})
            break
        if results is not None:
            results.append({"command": command, "exit": proc.returncode, "note": "", "output": output})
        parts.append(f"$ {command}\n{output}\n")
        if proc.returncode == 0:
            passed += 1
        else:
            if failed_output is not None:
                failed_output.append(output)
            break
    accumulated = "".join(parts)
    return passed, total, accumulated[-ACCEPTANCE_CAP:], foreign_seen
