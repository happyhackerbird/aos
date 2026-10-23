"""Best-effort host resource ownership; task files have a separate lifecycle.

Tags survive session detachment, but can be stripped by tools. This is not a
security boundary. Path matches alone never authorize process termination.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import time
import tomllib
import threading
import fcntl
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

import psutil

from agent_os import policy as git_policy


def record_paths(worktree: str, run_id: str, environ: dict[str, str]) -> tuple[Path, Path]:
    root = environ.get("AOS_RESOURCE_ROOT")
    if root:
        digest = hashlib.sha256(str(Path(worktree).resolve()).encode()).hexdigest()[:16]
        record = Path(root).expanduser() / f"{run_id}-{digest}" / "resources.json"
    else:
        record = Path(worktree) / ".aos" / f"resources-{run_id}.json"
    return record, record.with_suffix(".processes.jsonl")


def lock_worktree(worktree: str, root: str):
    """A local lifecycle lock also prevents cache pruning during a successor run."""
    directory = Path(root).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(Path(worktree).resolve()).encode()).hexdigest()
    fd = os.open(directory / f"workspace-{digest}.lock",
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    stream = os.fdopen(fd, "r+")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise
    return stream


def provenance_path(worktree: str, root: str) -> Path:
    digest = hashlib.sha256(str(Path(worktree).resolve()).encode()).hexdigest()
    return Path(root).expanduser() / f"worktree-{digest}.provenance"


def write_provenance(worktree: str, root: str, git_dir: str) -> None:
    path = provenance_path(worktree, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    data = {"worktree": str(Path(worktree).resolve()), "git_dir": git_dir,
            "written": datetime.now(timezone.utc).isoformat()}
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def read_provenance(worktree: str, root: str) -> str | None:
    try:
        data = json.loads(provenance_path(worktree, root).read_text())
        if data.get("worktree") == str(Path(worktree).resolve()) and isinstance(data.get("git_dir"), str):
            return data["git_dir"]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def append_identity(path: Path, run_id: str, process: psutil.Process) -> None:
    identity = {"pid": process.pid, "created": process.create_time(), "run_id": run_id,
                "host": socket.gethostname()}
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(identity) + "\n").encode())
    finally:
        os.close(fd)


def resource_gate(process_path: Path):
    fd = os.open(process_path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    stream = os.fdopen(fd, "r+")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX)
    except OSError:
        stream.close()
        raise
    return stream


def shared_process(process: psutil.Process) -> bool:
    name = process.name().lower()
    return name in {"colima", "limactl", "vfkit", "dockerd", "containerd"} or name.startswith("qemu-system")


def cleanup_pending(events: list[str]) -> bool:
    return any(any(word in event for word in ("failed", "skipped", "incomplete", "denied"))
               or ("Docker" in event and any(word in event for word in ("unavailable", "disabled")))
               for event in events)


def own_command(command: list[str]) -> int:
    """Register this PID before exec, so even environment-hidden binaries are owned."""
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise ValueError("aos own requires a command")
    run_id = os.environ.get("AOS_RUN_ID", "")
    worktree = os.environ.get("AOS_WORKTREE", "")
    if not run_id or not worktree or not all(c.isalnum() or c in "-_" for c in run_id):
        raise ValueError("aos own requires a runner-owned environment")
    directory = Path(worktree) / ".aos"
    if directory.is_symlink():
        raise ValueError("aos own refuses a symbolic-link .aos directory")
    record_path, process_path = record_paths(worktree, run_id, os.environ)
    with resource_gate(process_path):
        record = json.loads(record_path.read_text())
        if (record.get("closed") or record.get("run_id") != run_id
                or record.get("closing")
                or record.get("worktree") != str(Path(worktree).resolve())
                or record.get("host") != socket.gethostname()):
            raise ValueError("aos own requires an active local run")
        append_identity(process_path, run_id, psutil.Process())
        # The lock fd is CLOEXEC: registration completes before cleanup can begin.
        os.execvpe(command[0], command, os.environ)
    return 127  # exec never returns on success


def disk_free(path: Path) -> int:
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def load_policy(source: str) -> list[dict]:
    path = Path(source) / ".aos" / "cleanup.toml"
    if not path.exists():
        return []
    with path.open("rb") as stream:
        entries = tomllib.load(stream).get("cache", [])
    if not isinstance(entries, list):
        raise ValueError("cleanup.toml: cache must be an array of tables")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("cleanup.toml: invalid cache entry")
        raw = entry.get("path")
        if not isinstance(raw, str) or not raw or raw in (".", ".."):
            raise ValueError("cleanup.toml: cache path must be a relative child path")
        p = Path(raw)
        if p.is_absolute() or any(part in ("..", ".git", ".aos") for part in p.parts):
            raise ValueError(f"cleanup.toml: unsafe cache path {raw!r}")
        for key in ("max_bytes", "max_age_seconds"):
            value = entry.get(key)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"cleanup.toml: {key} must be a positive integer")
    return entries


POSTGRES_IMAGE = "postgres:15-alpine@sha256:f7d23353e1b15400d22ebe31189f4d314b87a4c129cc400c8c2d8d4ca127bf81"
_PINNED_IMAGE = re.compile(r"[A-Za-z0-9][^\s@]*@sha256:[0-9a-f]{64}")


def load_services(source: str) -> dict:
    path = Path(source) / ".aos" / "services.toml"
    try:
        if not path.exists():
            return {}
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"services.toml: {exc}") from exc
    for name in sorted(data):
        if name != "postgres":
            raise ValueError(f"services.toml: unknown table or key {name}")
    if "postgres" not in data:
        return {}
    postgres = data["postgres"]
    if not isinstance(postgres, dict):
        raise ValueError("services.toml: postgres must be a table")
    for key in sorted(postgres):
        if key != "image":
            raise ValueError(f"services.toml: unknown key postgres.{key}")
    image = postgres.get("image", POSTGRES_IMAGE)
    if not isinstance(image, str):
        raise ValueError("services.toml: postgres.image must be a string")
    if not _PINNED_IMAGE.fullmatch(image):
        raise ValueError("services.toml: postgres.image must be pinned by an @sha256: digest")
    return {"postgres": {"image": image}}


def docker_available(environ: Mapping[str, str]) -> bool:
    docker = shutil.which("docker", path=environ.get("PATH"))
    if docker is None:
        return False
    env = {k: v for k, v in environ.items() if k != "AOS_TOKEN"}
    try:
        if not environ.get("DOCKER_HOST") and not environ.get("DOCKER_CONTEXT"):
            subprocess.run([docker, "context", "show"], env=env, capture_output=True, text=True,
                           check=True, timeout=30)
        subprocess.run([docker, "version", "--format", "{{.Server.Version}}"], env=env,
                       capture_output=True, text=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


POSTGRES_PULL_SECONDS = 600
POSTGRES_READY_SECONDS = 60


class ServiceError(RuntimeError):
    """A requested service could not be started; the message is `<service>: <stage>: <reason>`."""


def _first_line(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        for text in (exc.stderr, exc.stdout):
            for line in (text or "").splitlines():
                if line.strip():
                    return line.strip()
        return f"exit {exc.returncode}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"timed out after {exc.timeout:g} s"
    return str(exc) or type(exc).__name__


def prune_caches(worktree: str, policy: list[dict], minimum: int, git_dir: str | None = None) -> list[str]:
    """Only declared, wholly ignored, untracked paths; never follow symlinks."""
    root = Path(worktree).resolve()
    if not root.is_dir():
        return []
    events = []
    for entry in policy:
        raw = Path(entry["path"])
        if (raw.is_absolute() or not raw.parts or any(p in ("..", ".git", ".aos") for p in raw.parts)):
            events.append(f"retained unsafe cache path {entry['path']}")
            continue
        path = root / raw
        if not path.exists() and not path.is_symlink():
            continue
        try:
            if git_dir is None:
                events.append(f"retained cache {entry['path']}: worktree git state unverified")
                continue
            parents = list(path.relative_to(root).parents)
            if path.is_symlink() or any((root / p).is_symlink() for p in parents):
                events.append(f"retained cache {entry['path']}: symbolic link")
                continue
            tracked = git_policy.git_output(str(root), "--literal-pathspecs", "ls-files", "--", entry["path"],
                                            git_dir=git_dir)[1]
            ignored, _ = git_policy.git_output(str(root), "check-ignore", "-q", "--", entry["path"],
                                               ok=(0, 1), git_dir=git_dir)
            if tracked or ignored != 0:
                events.append(f"retained cache {entry['path']}: must be ignored and contain no tracked files")
                continue
            files = [path] if path.is_file() else [p for p in path.rglob("*") if not p.is_symlink()]
            stats = [p.stat() for p in files]
            size = sum(s.st_size for s in stats if stat.S_ISREG(s.st_mode))
            newest = max([path.stat().st_mtime, *(s.st_mtime for s in stats)])
            pressure = disk_free(root) < minimum
            oversize = entry.get("max_bytes") is not None and size > entry["max_bytes"]
            stale = (entry.get("max_age_seconds") is not None
                     and time.time() - newest > entry["max_age_seconds"])
            if not (pressure or oversize or stale):
                events.append(f"retained cache {entry['path']}: reusable ({size} logical bytes)")
                continue
            before = disk_free(root)
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            freed = max(0, disk_free(root) - before)
            events.append(f"pruned cache {entry['path']}: {size} logical bytes; free-space increase {freed} bytes")
        except (OSError, subprocess.SubprocessError) as exc:
            events.append(f"cache cleanup failed for {entry['path']}: {type(exc).__name__}")
    return events


class RunResources:
    def __init__(self, run_id: str, worktree: str, environ: dict[str, str]):
        self.run_id = run_id
        self.worktree = str(Path(worktree).resolve())
        # Run IDs are bus-generated; preserve uniqueness rather than silently sanitizing.
        if not all(c.isalnum() or c in "-_" for c in run_id) or not run_id:
            raise ValueError("invalid run ID for resource ownership")
        self.project = "aos-" + run_id.lower()
        self.tags = {"AOS_RUN_ID": run_id, "AOS_WORKTREE": self.worktree,
                     "COMPOSE_PROJECT_NAME": self.project}
        if environ.get("AOS_RESOURCE_ROOT"):
            self.tags["AOS_RESOURCE_ROOT"] = str(Path(environ["AOS_RESOURCE_ROOT"]).expanduser().resolve())
        self.env = {k: v for k, v in environ.items() if k != "AOS_TOKEN"} | self.tags
        self.record_path, self.process_path = record_paths(self.worktree, run_id, self.env)
        self.task_id: str | None = None
        self.git_dir: str | None = None
        self.bus_url: str | None = None
        self.events: list[str] = []
        self.closed = False
        self.closing = False
        self.started = time.time()
        self.runner_pid = os.getpid()
        self.runner_created = psutil.Process().create_time()
        self._tracking_stop = threading.Event()
        self._tracker: threading.Thread | None = None
        self._launch_gate = None
        self._observed: set[tuple[int, float]] = set()
        self._tracking_lock = threading.Lock()
        self.docker = shutil.which("docker", path=environ.get("PATH"))
        self.docker_prefix = [self.docker] if self.docker else []
        self.docker_status = "pending" if self.docker else "not_applicable"
        if self.docker and not environ.get("DOCKER_HOST") and not environ.get("DOCKER_CONTEXT"):
            try:
                context = self._docker("context", "show")
                self.docker_prefix += ["--context", context.strip()]
            except (OSError, subprocess.SubprocessError):
                self.events.append("Docker context discovery failed; Docker cleanup unavailable")
                self.docker = None
                self.docker_status = "unavailable"
        self.docker_endpoint = None if self.docker_status == "unavailable" else self.endpoint_key()
        self.postgres_container: str | None = None
        self.postgres_image: str | None = None

    def endpoint_key(self) -> str:
        # Preserve endpoint identity without writing possible Docker-host credentials.
        endpoint = [self.docker_prefix, self.env.get("DOCKER_HOST"), self.env.get("DOCKER_CONTEXT")]
        return hashlib.sha256(json.dumps(endpoint).encode()).hexdigest()

    def _docker(self, *args: str, timeout: float = 30, env: dict[str, str] | None = None) -> str:
        result = subprocess.run([*self.docker_prefix, *args], env=self.env | (env or {}), capture_output=True,
                                text=True, check=True, timeout=timeout)
        return result.stdout

    def restore_docker(self, record: dict) -> None:
        """Keep the original endpoint binding even across repeated failed recovery."""
        self.postgres_container = record.get("postgres_container")
        self.postgres_image = record.get("postgres_image")
        self.docker_endpoint = record.get("docker_endpoint")
        if record.get("docker_status") == "not_applicable":
            self.docker = None
            self.docker_status = "not_applicable"
        elif self.docker_endpoint is None:
            self.docker = None
            self.docker_status = "unavailable"
            self.events.append("Docker recovery skipped: original endpoint is unknown")
        elif not self.docker:
            self.docker_status = "unavailable"
            self.events.append("Docker recovery failed: Docker is unavailable")
        elif self.endpoint_key() != self.docker_endpoint:
            self.docker = None
            self.docker_status = "pending"
            self.events.append("Docker recovery skipped: endpoint changed")
        else:
            self.docker_status = "pending"

    def start_postgres(self, image: str) -> str:
        """Start this run's disposable PostgreSQL; the runner records, and the label teardown removes it."""
        if not self.docker:
            raise ServiceError("postgres: start: Docker is unavailable")
        password = secrets.token_urlsafe(24)
        stage = "pull"
        try:
            try:
                self._docker("image", "inspect", image)
            except subprocess.CalledProcessError:
                self._docker("pull", image, timeout=POSTGRES_PULL_SECONDS)
            stage = "start"
            container = self._docker(
                "run", "-d", "--name", f"aos-{self.run_id}-postgres", "--label", f"aos.run={self.run_id}",
                "--label", "aos.service=postgres", "--tmpfs", "/var/lib/postgresql/data",
                "-p", "127.0.0.1::5432", "-e", "POSTGRES_USER=aos", "-e", "POSTGRES_DB=aos",
                "-e", "POSTGRES_PASSWORD", image, env={"POSTGRES_PASSWORD": password}).strip()
            self.postgres_container = container
            self.postgres_image = image
            self.events.append(f"started Docker container {container[:12]} (postgres)")
            stage = "port"
            line = self._docker("port", container, "5432/tcp").splitlines()[0].strip()
            host, _, port = line.rpartition(":")
            if host != "127.0.0.1" or not port.isdigit() or not 0 < int(port) < 65536:
                raise ValueError(f"unexpected port mapping {line}")
            stage = "readiness"
            deadline = time.monotonic() + POSTGRES_READY_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                try:
                    self._docker("exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "aos", "-d", "aos",
                                 timeout=max(1.0, min(30.0, remaining)))
                    break
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.5)
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
            raise ServiceError(f"postgres: {stage}: {_first_line(exc)}") from None
        return f"postgresql://aos:{password}@127.0.0.1:{port}/aos"

    def record(self) -> None:
        directory = Path(self.worktree) / ".aos"
        if directory.is_symlink() and self.record_path.parent == directory:
            self.events.append("resource log not written: .aos is a symbolic link")
            return
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        target = self.record_path
        temporary = target.with_suffix(f".{os.getpid()}.tmp")
        data = {"run_id": self.run_id, "worktree": self.worktree, "host": socket.gethostname(),
                "started": self.started, "closed": self.closed, "compose_project": self.project,
                "closing": self.closing, "task_id": self.task_id, "bus_url": self.bus_url,
                "cleanup_complete": (self.closed and self.docker_status in ("complete", "not_applicable")
                                     and not cleanup_pending(self.events)),
                "runner_pid": self.runner_pid, "runner_created": self.runner_created,
                "docker_endpoint": self.docker_endpoint, "docker_status": self.docker_status,
                "postgres_container": self.postgres_container, "postgres_image": self.postgres_image,
                "events": self.events}
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
        temporary.replace(target)
        if target.parent != directory and directory.is_dir() and not directory.is_symlink():
            # The host registry is authoritative; the worktree copy is for inspection.
            mirror = directory / f"resources-{self.run_id}.json"
            if not mirror.is_symlink():
                mirror.write_text(json.dumps(data, indent=2))

    def registered_processes(self) -> dict[int, psutil.Process]:
        owned = {}
        if self.process_path.is_symlink():
            event = "registered process ledger inspection failed: symbolic link"
            if event not in self.events:
                self.events.append(event)
            return owned
        try:
            lines = self.process_path.read_text().splitlines()
        except FileNotFoundError:
            return owned
        except UnicodeError:
            event = "registered process ledger inspection failed: invalid encoding"
            if event not in self.events:
                self.events.append(event)
            return owned
        for number, line in enumerate(lines, 1):
            try:
                identity = json.loads(line)
                if (identity["run_id"] != self.run_id or identity["host"] != socket.gethostname()
                        or identity["pid"] == os.getpid()):
                    continue
                process = psutil.Process(identity["pid"])
                if process.create_time() == identity["created"]:
                    owned[process.pid] = process
                elif f"retained reused PID {process.pid}" not in self.events:
                    self.events.append(f"retained reused PID {process.pid}")
            except psutil.NoSuchProcess:
                continue
            except (ValueError, KeyError, TypeError):
                event = f"registered process ledger inspection failed: invalid entry at line {number}"
                if event not in self.events:
                    self.events.append(event)
            except psutil.AccessDenied:
                self.events.append("registered process identity inspection denied")
        return owned

    def import_registry(self, path: Path) -> None:
        if path == self.process_path:
            return
        target = self.process_path
        self.process_path = path
        try:
            valid = self.registered_processes()
        finally:
            self.process_path = target
        # A partial migration would strand unresolved identities in the old ledger
        # once recovery starts preferring the new host record.
        if cleanup_pending([event for event in self.events if event.startswith("registered process ")]):
            raise OSError("cannot migrate an incompletely inspected process ledger")
        for process in valid.values():
            append_identity(target, self.run_id, process)

    def begin_closing(self) -> None:
        if not self.closing:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            self._launch_gate = resource_gate(self.process_path)
            self.closing = True
            self.record()

    def register(self, pid: int) -> None:
        try:
            process = psutil.Process(pid)
            append_identity(self.process_path, self.run_id, process)
        except psutil.NoSuchProcess:
            pass

    def snapshot_descendants(self) -> None:
        with self._tracking_lock:
            try:
                registered = self.registered_processes()
            except OSError:
                if "descendant tracking failed" not in self.events:
                    self.events.append("descendant tracking failed")
                return
            for parent in registered.values():
                try:
                    if shared_process(parent):
                        continue
                    # Do not traverse into shared infrastructure and accidentally
                    # claim its helper processes as run-owned descendants.
                    pending = list(parent.children())
                    while pending:
                        child = pending.pop()
                        if shared_process(child):
                            continue
                        identity = (child.pid, child.create_time())
                        if identity not in self._observed:
                            append_identity(self.process_path, self.run_id, child)
                            self._observed.add(identity)
                        pending.extend(child.children())
                except psutil.NoSuchProcess:
                    pass
                except (psutil.AccessDenied, OSError):
                    if "descendant tracking incomplete" not in self.events:
                        self.events.append("descendant tracking incomplete")

    def start_tracking(self) -> None:
        def track():
            while not self._tracking_stop.wait(.02):
                try:
                    self.snapshot_descendants()
                except (OSError, psutil.Error):
                    if "descendant tracking failed" not in self.events:
                        self.events.append("descendant tracking failed")
        self._tracker = threading.Thread(target=track, daemon=True)
        self._tracker.start()

    def stop_tracking(self) -> None:
        self._tracking_stop.set()
        if self._tracker is not None:
            self._tracker.join(timeout=5)
        self.snapshot_descendants()

    def stop_processes(self) -> None:
        owned = self.registered_processes()
        inaccessible = 0
        processes = list(psutil.process_iter())
        protected = set()
        for process in processes:
            try:
                if shared_process(process):
                    protected.update(child.pid for child in process.children(recursive=True))
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                pass
        if protected:
            self.events.append(f"retained shared-service subtrees ({len(protected)} processes)")
        for process in processes:
            if process.pid == os.getpid():
                continue
            try:
                if process.pid in protected:
                    owned.pop(process.pid, None)
                    continue
                # psutil's Process signal methods check creation identity to reject PID reuse.
                process.create_time()
                try:
                    env = process.environ()
                except psutil.AccessDenied:
                    env = {}
                    inaccessible += 1
                if (env.get("AOS_RUN_ID") == self.run_id
                        and env.get("AOS_WORKTREE") == self.worktree):
                    owned[process.pid] = process
                    continue
                if process.pid in owned:
                    continue
                cwd = process.cwd()
                command = process.cmdline()
                if (Path(cwd).resolve().is_relative_to(self.worktree)
                        or any(arg == self.worktree or arg.startswith(self.worktree + "/")
                               for arg in command)):
                    self.events.append(f"retained unexplained process {process.pid} ({process.name()}): path only")
            except psutil.NoSuchProcess:
                pass
            except (psutil.AccessDenied, OSError):
                inaccessible += 1
        if inaccessible:
            self.events.append(f"process inspection unavailable for {inaccessible} processes")
        signalled = []
        for process in owned.values():
            try:
                name = process.name()
                if shared_process(process):
                    self.events.append(f"retained shared service {process.pid} ({name})")
                    continue
                process.terminate()
                signalled.append(process)
                self.events.append(f"sent TERM to owned process {process.pid} ({name})")
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                self.events.append(f"process cleanup failed: permission denied for {process.pid}")
        _, alive = psutil.wait_procs(signalled, timeout=3)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                self.events.append(f"process cleanup failed: cannot kill {process.pid}")
        _, survivors = psutil.wait_procs(alive, timeout=3)
        survivor_ids = set()
        for process in survivors:
            try:
                if process.status() != psutil.STATUS_ZOMBIE:
                    survivor_ids.add(process.pid)
            except psutil.NoSuchProcess:
                pass
        for process in signalled:
            self.events.append(f"{'cleanup failed for' if process.pid in survivor_ids else 'stopped'} owned process {process.pid}")

    def stop_docker(self) -> None:
        if not self.docker:
            return
        self.docker_status = "pending"
        failed = False
        # Labels allow teardown without the original Compose files, cwd, or profiles.
        # Explicit docker run resources must opt in with the aos.run label.
        try:
            for kind, listing, removal in (("container", ["ps", "-aq"], ["rm", "-f"]),
                                           ("network", ["network", "ls", "-q"], ["network", "rm"])):
                ids = set()
                for label in (f"com.docker.compose.project={self.project}", f"aos.run={self.run_id}"):
                    ids.update(self._docker(*listing, "--filter", "label=" + label).split())
                for identity in sorted(ids):
                    try:
                        inspected = json.loads(self._docker(kind, "inspect", identity))[0]
                        labels = (inspected.get("Config", {}).get("Labels") if kind == "container"
                                  else inspected.get("Labels")) or {}
                        if labels.get("aos.shared", "").lower() in ("true", "1", "yes"):
                            self.events.append(f"retained shared Docker {kind} {identity}")
                            continue
                        if (labels.get("aos.run") != self.run_id
                                and labels.get("com.docker.compose.project") != self.project):
                            self.events.append(f"retained Docker {kind} {identity}: ownership changed")
                            continue
                        self._docker(*removal, identity)
                        self.events.append(f"removed owned Docker {kind} {identity}")
                    except (OSError, subprocess.SubprocessError, ValueError, IndexError, TypeError):
                        failed = True
                        self.events.append(f"Docker cleanup failed for {kind} {identity}")
            if not failed:
                self.docker_status = "complete"
        except (OSError, subprocess.SubprocessError):
            self.docker_status = "unavailable"
            self.events.append("Docker resource inspection failed")

    def close(self, policy: list[dict], minimum: int) -> str:
        if not self.closed:
            try:
                self.begin_closing()
                self.stop_tracking()
            except (OSError, psutil.Error):
                self.events.append("descendant tracking failed at cleanup")
            for stop in (self.stop_processes, self.stop_docker):
                try:
                    stop()
                except (OSError, psutil.Error, subprocess.SubprocessError) as exc:
                    self.events.append(f"resource cleanup failed: {type(exc).__name__}")
            if any("retained unexplained process" in event for event in self.events) or cleanup_pending(self.events):
                self.events.append("retained caches: process ownership or termination is unresolved")
                if self.git_dir is None:
                    self.events.extend(prune_caches(self.worktree, policy, minimum, self.git_dir))
            else:
                self.events.extend(prune_caches(self.worktree, policy, minimum, self.git_dir))
            self.events.append("retained task worktree, undeclared artifacts, Docker volumes/build cache and shared services")
            self.closed = True
            try:
                self.record()
            except OSError as exc:
                self.events.append(f"resource log write failed: {type(exc).__name__}")
            finally:
                if self._launch_gate is not None:
                    self._launch_gate.close()
                    self._launch_gate = None
        return "Run cleanup:\n" + "\n".join(self.events)


def recover_runs(runs: list[dict], current: RunResources) -> list[str]:
    """After a successful task claim, retry unfinished local teardown from earlier attempts."""
    events = []
    directory = Path(current.worktree) / ".aos"
    if directory.is_symlink():
        return events
    for run in runs:
        run_id = run["id"]
        if run_id == current.run_id or run.get("status") in ("claimed", "running"):
            continue
        if not all(c.isalnum() or c in "-_" for c in run_id):
            continue
        path, process_path = record_paths(current.worktree, run_id, current.env)
        if not path.exists():
            flat = path.parent.with_suffix(".json")
            path = flat if flat.exists() else directory / f"resources-{run_id}.json"
            process_path = path.with_suffix(".processes.jsonl")
        try:
            record = json.loads(path.read_text())
            if (record.get("host") != socket.gethostname() or record.get("run_id") != run_id
                    or record.get("worktree") != current.worktree):
                continue
            if (record.get("closed") and record.get("docker_status") not in ("pending", "unavailable")
                    and not cleanup_pending(record.get("events", []))):
                continue
            if not record.get("closed") and record.get("runner_pid"):
                try:
                    owner = psutil.Process(record["runner_pid"])
                    if (owner.create_time() == record.get("runner_created") and owner.is_running()
                            and owner.status() != psutil.STATUS_ZOMBIE):
                        events.append(f"retained resources from {run_id}: runner is still alive")
                        continue
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    # Gone, or the PID now belongs to a process this user cannot inspect:
                    # either way it is not the runner that wrote the record.
                    pass
            previous = RunResources(run_id, current.worktree, current.env)
            previous.record_path.parent.mkdir(parents=True, exist_ok=True)
            previous.import_registry(process_path)
            previous.task_id = record.get("task_id") or current.task_id
            previous.bus_url = record.get("bus_url") or current.bus_url
            previous.started = record["started"]
            previous.restore_docker(record)
            events.append(f"recovering resources from {run_id}\n" + previous.close([], 0))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError, psutil.Error) as exc:
            events.append(f"resource recovery failed for {run_id}: {type(exc).__name__}")
    return events
