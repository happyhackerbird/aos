"""aos dispatch: start a runner for every ready task this host may take (task-flow Part B).

A long-running command on each worker host (systemd on the server). It is stateless; the bus is
the state. Every AOS_DISPATCH_POLL_SECONDS it lists the ready tasks whose host is this host or
`any`, skips tier 3 (the claim route refuses it without an explicit confirmation) and tasks on
cooldown, counts the live runs (claimed or running on the bus with this host's worker name, plus
its own runners still alive) and, below AOS_DISPATCH_MAX_RUNS, spawns one runner per candidate in
task-number order:

    <python> -P -m agent_os.openshell_runner --token-fd <fd> run <task>

with the bus token on a pipe and AOS_TOKEN absent from the child's environment (the runner refuses
otherwise), in its own session, stdout and stderr to <AOS_DISPATCH_LOG_DIR>/<task>-<UTC stamp>.log.
No dispatch key is passed: the runner keeps its own random key, and the claim route's "task has a
live run" conflict is what stops a second run. The bus's claim rule stays the only authority.

A runner that exits while its task is still ready (a refusal before claim, a claim conflict) puts
the task on an AOS_DISPATCH_COOLDOWN_SECONDS cooldown; after three such exits in a row the task is
moved back to backlog (POST /tasks/<id>/unready) and the last log's path is logged; a move the bus
refuses or cannot receive keeps the cooldown and the count, and the next such exit retries it. The
dispatcher never requeues a failed or blocked task, never merges, never pushes. SIGTERM (and SIGINT) stop the
polling and exit; the runners keep running. `aos dispatch --once` runs one poll and returns.

Environment: AOS_URL, AOS_TOKEN (read once at start and removed from this process's environment),
AOS_HOST (mac|server), AOS_DISPATCH_POLL_SECONDS (60), AOS_DISPATCH_MAX_RUNS (1),
AOS_DISPATCH_COOLDOWN_SECONDS (900), AOS_DISPATCH_LOG_DIR (~/.aos/logs/dispatch), plus everything
the runner reads (AOS_REPO_ROOT, AOS_OPENSHELL_*, AOS_PUSH*, ...), which the runners inherit.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

import httpx

RUN_LIVE = ("claimed", "running")
RUNNER_MODULE = "agent_os.openshell_runner"
STRIKES_TO_UNREADY = 3
HOSTS = ("mac", "server")
clock = time.monotonic  # indirection so tests can move the cooldown clock


class DispatchError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Settings:
    url: str
    host: str
    poll_seconds: int
    max_runs: int
    cooldown_seconds: int
    log_dir: str


def _number(environ: dict[str, str], key: str, default: str, *, minimum: int) -> int:
    value = environ.get(key, default)
    if not value.isdecimal() or int(value) < minimum:
        raise DispatchError(f"{key} must be a whole number of at least {minimum} (got {value!r})")
    return int(value)


def settings_from(environ: dict[str, str]) -> Settings:
    host = environ.get("AOS_HOST", "mac")
    if host not in HOSTS:
        raise DispatchError(f"AOS_HOST must be mac or server (got {host!r})")
    return Settings(
        url=environ.get("AOS_URL", "http://127.0.0.1:8020"),
        host=host,
        poll_seconds=_number(environ, "AOS_DISPATCH_POLL_SECONDS", "60", minimum=1),
        max_runs=_number(environ, "AOS_DISPATCH_MAX_RUNS", "1", minimum=1),
        cooldown_seconds=_number(environ, "AOS_DISPATCH_COOLDOWN_SECONDS", "900", minimum=0),
        log_dir=os.path.expanduser(environ.get("AOS_DISPATCH_LOG_DIR", "~/.aos/logs/dispatch")),
    )


def task_number(task_id: str) -> int:
    """Lowest task number first; an id of another shape sorts last."""
    tail = task_id.rsplit("-", 1)[-1]
    return int(tail) if tail.isdecimal() else 10**9


@dataclasses.dataclass
class Child:
    task_id: str
    process: Any            # subprocess.Popen (a test may substitute an object with poll() and pid)
    log_path: Path
    started: float


class Dispatcher:
    def __init__(self, client: httpx.Client, settings: Settings, token: str, environ: dict[str, str], *,
                 out: IO[str], err: IO[str]) -> None:
        self.client, self.settings, self.token = client, settings, token
        self.environ = {key: value for key, value in environ.items() if key != "AOS_TOKEN"}
        self.out, self.err = out, err
        self.worker = socket.gethostname()
        self.children: dict[str, Child] = {}
        self.cooldown_until: dict[str, float] = {}
        self.strikes: dict[str, int] = {}
        self.stop = threading.Event()
        self.polls = 0

    def log(self, line: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"{stamp} {line}", file=self.out, flush=True)

    # -- the bus --------------------------------------------------------------------------

    def _get(self, path: str, **params: str) -> Any | None:
        """A GET answered with JSON, or None (logged) when the bus refuses or cannot be reached."""
        try:
            response = self.client.get(path, params=params or None)
        except httpx.HTTPError as exc:
            self.log(f"bus unreachable: GET {path} ({type(exc).__name__})")
            return None
        if response.status_code != 200:
            self.log(f"bus refused GET {path}: {response.status_code} {response.text[:200]}")
            return None
        try:
            return response.json()
        except ValueError:  # a 200 that is not the bus (a proxy page, a wrong AOS_URL)
            self.log(f"bus answered GET {path} without JSON: {response.text[:200]}")
            return None

    def _post(self, path: str, body: dict[str, Any]) -> Any | None:
        """Best-effort publication; a bus refusal never changes the poll's decisions."""
        try:
            response = self.client.post(path, json=body)
        except httpx.HTTPError as exc:
            self.log(f"bus unreachable: POST {path} ({type(exc).__name__})")
            return None
        if not response.is_success:
            self.log(f"bus refused POST {path}: {response.status_code} {response.text[:200]}")
            return None
        try:
            return response.json()
        except ValueError:
            self.log(f"bus answered POST {path} without JSON: {response.text[:200]}")
            return None

    def candidates(self) -> list[dict[str, Any]]:
        """Ready tasks for this host or any, tier below 3, not on cooldown, not already this dispatcher's
        child, lowest task number first. A bus error means no candidates for this poll."""
        tasks: list[dict[str, Any]] = []
        for host in (self.settings.host, "any"):
            listing = self._get("/tasks", status="ready", host=host)
            if listing is None:
                return []
            tasks.extend(listing)
        now = clock()
        chosen = [task for task in tasks
                  if int(task.get("tier") or 0) < 3 and task["id"] not in self.children
                  and self.cooldown_until.get(task["id"], 0.0) <= now]
        return sorted(chosen, key=lambda task: task_number(task["id"]))

    def live_on_bus(self) -> set[str] | None:
        """Task ids with a claimed or running run whose worker is this host's name; None on a bus error."""
        ids: set[str] = set()
        for status in RUN_LIVE:
            listing = self._get("/tasks", status=status)
            if listing is None:
                return None
            for task in listing:
                detail = self._get(f"/tasks/{task['id']}")
                if detail is None:
                    return None
                if any(run.get("status") in RUN_LIVE and run.get("worker") == self.worker
                       for run in detail.get("runs") or []):
                    ids.add(task["id"])
        return ids

    # -- children -----------------------------------------------------------------------

    def _log_path(self, task_id: str) -> Path:
        directory = Path(self.settings.log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"{task_id}-{stamp}.log"
        n = 1
        while path.exists():
            n += 1
            path = directory / f"{task_id}-{stamp}-{n}.log"
        return path

    def spawn(self, task_id: str) -> Child:
        """One runner for the task: the token on a pipe, AOS_TOKEN absent from the child's environment, its own
        session (a SIGTERM to the dispatcher never reaches it), output to a per-run log."""
        log_path = self._log_path(task_id)
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, self.token.encode("utf-8"))  # bounded: the token fits a pipe's atomic write
            os.close(write_fd)
            write_fd = -1
            argv = [sys.executable, "-P", "-m", RUNNER_MODULE, "--token-fd", str(read_fd), "run", task_id]
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                process = subprocess.Popen(argv, env=self.environ, stdin=subprocess.DEVNULL, stdout=fd,
                                           stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(read_fd,))
            finally:
                os.close(fd)
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
        child = Child(task_id, process, log_path, clock())
        self.children[task_id] = child
        self.log(f"{task_id}: runner started, pid {process.pid}, log {log_path}")
        return child

    def reap(self) -> None:
        """Account for runners that exited: a task still ready afterwards goes on cooldown, three times in a
        row back to backlog; a task that was claimed resets the count."""
        for task_id, child in list(self.children.items()):
            rc = child.process.poll()
            if rc is None:
                continue
            del self.children[task_id]
            task = self._get(f"/tasks/{task_id}")
            status = task.get("status") if isinstance(task, dict) else None
            if status != "ready":
                if status is not None:
                    self.strikes.pop(task_id, None)
                self.log(f"{task_id}: runner exited {rc}; task {status or 'unknown'} (log {child.log_path})")
                if status is None:  # the bus could not say: hold the task off until it can
                    self.cooldown_until[task_id] = clock() + self.settings.cooldown_seconds
                continue
            strikes = self.strikes.get(task_id, 0) + 1
            if strikes >= STRIKES_TO_UNREADY:
                try:
                    response = self.client.post(f"/tasks/{task_id}/unready")
                    moved = response.status_code == 200
                    outcome = "moved back to backlog" if moved else \
                        f"unready refused: {response.status_code} {response.text[:200]}"
                except httpx.HTTPError as exc:
                    moved = False
                    outcome = f"unready failed: bus unreachable ({type(exc).__name__})"
                if moved:
                    self.strikes.pop(task_id, None)
                    self.cooldown_until.pop(task_id, None)
                else:
                    # Not moved: the task is still ready on the bus. Keep it off this host for a
                    # cooldown and keep the count, so the next such exit retries the move instead
                    # of starting the three strikes over.
                    self.strikes[task_id] = strikes
                    self.cooldown_until[task_id] = clock() + self.settings.cooldown_seconds
                self.log(f"{task_id}: runner exited {rc} with the task still ready, {strikes} times in a row; "
                         f"{outcome}; last log {child.log_path}")
                continue
            self.strikes[task_id] = strikes
            self.cooldown_until[task_id] = clock() + self.settings.cooldown_seconds
            self.log(f"{task_id}: runner exited {rc} with the task still ready ({strikes}/{STRIKES_TO_UNREADY}); "
                     f"cooldown {self.settings.cooldown_seconds} s; log {child.log_path}")

    def alive(self) -> list[Child]:
        return [child for child in self.children.values() if child.process.poll() is None]

    # -- the loop -----------------------------------------------------------------------

    def poll(self) -> int:
        """One round: reap, count, start. Returns the number of runners started."""
        self.polls += 1
        self.reap()
        on_bus = self.live_on_bus()
        if on_bus is None:
            self.log("live runs unknown (bus error); nothing started")
            return 0
        live = on_bus | set(self.children)
        self._post(f"/hosts/{self.worker}/capacity", {"max_runs": self.settings.max_runs, "live": len(live)})
        candidates = self.candidates()
        if not candidates:
            return 0
        if len(live) >= self.settings.max_runs:
            self.log(f"{len(live)} live run(s) at the cap {self.settings.max_runs}; "
                     f"{len(candidates)} ready task(s) wait: {', '.join(t['id'] for t in candidates)}")
            return 0
        started = 0
        for task in candidates:
            if len(live) >= self.settings.max_runs:
                self.log(f"cap {self.settings.max_runs} reached; still waiting: "
                         + ", ".join(t["id"] for t in candidates[started:]))
                break
            try:
                self.spawn(task["id"])
            except OSError as exc:
                self.log(f"{task['id']}: runner could not be started: {exc}")
                self.cooldown_until[task["id"]] = clock() + self.settings.cooldown_seconds
                continue
            live.add(task["id"])
            started += 1
        return started

    def run(self, *, once: bool = False) -> int:
        self.log(f"dispatcher on {self.worker} (host {self.settings.host}): poll every "
                 f"{self.settings.poll_seconds} s, at most {self.settings.max_runs} live run(s)"
                 + (", one poll" if once else ""))
        while True:
            try:
                self.poll()
            except httpx.HTTPError as exc:  # _get and reap catch their own; this is the last net
                self.log(f"poll failed: bus unreachable ({type(exc).__name__})")
            if once:
                break
            remaining = float(self.settings.poll_seconds)
            while remaining > 0 and not self.stop.is_set():
                self.stop.wait(min(1.0, remaining))
                remaining -= 1.0
            if self.stop.is_set():
                break
        self.log(f"dispatcher stopping; {len(self.alive())} runner(s) keep running")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aos dispatch", description="start runners for ready tasks on this host")
    parser.add_argument("--once", action="store_true", help="one poll, then exit (runners started keep running)")
    args = parser.parse_args(argv)
    token = os.environ.pop("AOS_TOKEN", "")  # read once; gone from this process's environment from here on
    if not token:
        print("AOS_TOKEN is required", file=sys.stderr)
        return 2
    try:
        settings = settings_from(dict(os.environ))
    except DispatchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    with httpx.Client(base_url=settings.url, headers={"Authorization": f"Bearer {token}"}, timeout=30) as client:
        dispatcher = Dispatcher(client, settings, token, dict(os.environ), out=sys.stdout, err=sys.stderr)
        previous: dict[int, Any] = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, lambda *_: dispatcher.stop.set())
        try:
            return dispatcher.run(once=args.once)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
