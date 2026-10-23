"""The dispatcher (task-flow Part B): candidates, the cap, cooldown and unready, the token pipe, children
that outlive a dispatcher SIGTERM, --once. Process tests run a stub runner module that shadows
agent_os.openshell_runner through PYTHONPATH: a real subprocess, not a mock."""
from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

import agent_os
from agent_os import dispatch
from agent_os.api import create_app
from agent_os.config import Settings

BASE_ENV = {"AOS_URL": "http://bus", "AOS_HOST": "mac"}


# -- settings ------------------------------------------------------------------------------------

def test_settings_defaults_and_refusals():
    settings = dispatch.settings_from(BASE_ENV)
    assert (settings.poll_seconds, settings.max_runs, settings.cooldown_seconds) == (60, 1, 900)
    assert settings.log_dir == os.path.expanduser("~/.aos/logs/dispatch") and settings.host == "mac"
    settings = dispatch.settings_from(dict(BASE_ENV, AOS_HOST="server", AOS_DISPATCH_POLL_SECONDS="5",
                                           AOS_DISPATCH_MAX_RUNS="3", AOS_DISPATCH_COOLDOWN_SECONDS="0",
                                           AOS_DISPATCH_LOG_DIR="~/x/logs"))
    assert (settings.host, settings.poll_seconds, settings.max_runs, settings.cooldown_seconds) == ("server", 5, 3, 0)
    assert settings.log_dir == os.path.expanduser("~/x/logs")
    for key, value in (("AOS_HOST", "any"), ("AOS_DISPATCH_POLL_SECONDS", "0"), ("AOS_DISPATCH_MAX_RUNS", "0"),
                       ("AOS_DISPATCH_MAX_RUNS", "two"), ("AOS_DISPATCH_COOLDOWN_SECONDS", "-1")):
        with pytest.raises(dispatch.DispatchError, match=key):
            dispatch.settings_from(dict(BASE_ENV, **{key: value}))


def test_task_number_orders_by_number_and_unknown_last():
    assert sorted(["AO-10", "AO-2", "AO-1", "X"], key=dispatch.task_number) == ["AO-1", "AO-2", "AO-10", "X"]


# -- an in-process fake bus and fake runner processes ------------------------------------------

class FakeBus:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.down = False
        self.capacity_code = 200
        self.capacity_posts: list[dict[str, int]] = []
        self.unready_answer: httpx.Response | Exception | None = None   # a refusal or transport error for unready
        self.listing_body: str | None = None                            # a 200 for GET /tasks that is not JSON

    def add(self, task_id: str, *, host: str = "mac", tier: int = 1, status: str = "ready",
            runs: list[dict[str, Any]] | None = None) -> None:
        self.tasks[task_id] = {"id": task_id, "repo": "demo", "host": host, "tier": tier, "status": status,
                               "runs": runs or []}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path, dict(request.url.params)))
        if self.down:
            return httpx.Response(503, text="down")
        if request.method == "POST" and path.startswith("/hosts/") and path.endswith("/capacity"):
            body = json.loads(request.content)
            self.capacity_posts.append(body)
            return httpx.Response(self.capacity_code, json=body)
        if request.method == "GET" and path == "/tasks":
            if self.listing_body is not None:
                return httpx.Response(200, text=self.listing_body)
            status, host = request.url.params.get("status"), request.url.params.get("host")
            rows = [{k: v for k, v in t.items() if k != "runs"} for t in self.tasks.values()
                    if (status is None or t["status"] == status) and (host is None or t["host"] == host)]
            return httpx.Response(200, json=sorted(rows, key=lambda t: dispatch.task_number(t["id"])))
        if request.method == "GET" and path.startswith("/tasks/"):
            task = self.tasks.get(path[len("/tasks/"):])
            return httpx.Response(200, json=task) if task else httpx.Response(404, json={"detail": "Task not found"})
        if request.method == "POST" and path.endswith("/unready"):
            if isinstance(self.unready_answer, Exception):
                raise self.unready_answer
            if self.unready_answer is not None:
                return self.unready_answer
            task = self.tasks[path[len("/tasks/"):-len("/unready")]]
            task["status"] = "backlog"
            return httpx.Response(200, json=task)
        return httpx.Response(404)

    def unready_posts(self) -> list[str]:
        return [path.split("/")[2] for method, path, _ in self.calls if method == "POST" and path.endswith("/unready")]


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid, self.returncode = pid, None

    def poll(self) -> int | None:
        return self.returncode


def make_dispatcher(bus: FakeBus, tmp_path: Path, **env: str) -> tuple[dispatch.Dispatcher, list[str], io.StringIO]:
    """A dispatcher on the fake bus whose spawn records the task and hands back a fake process."""
    settings = dispatch.settings_from(dict(BASE_ENV, AOS_DISPATCH_LOG_DIR=str(tmp_path / "logs"), **env))
    client = httpx.Client(transport=httpx.MockTransport(bus), base_url="http://bus")
    out = io.StringIO()
    dispatcher = dispatch.Dispatcher(client, settings, "tok", {}, out=out, err=out)
    started: list[str] = []

    def spawn(task_id: str) -> dispatch.Child:
        child = dispatch.Child(task_id, FakeProcess(1000 + len(started)), tmp_path / "logs" / f"{task_id}.log", 0.0)
        dispatcher.children[task_id] = child
        started.append(task_id)
        return child

    dispatcher.spawn = spawn  # type: ignore[method-assign]
    return dispatcher, started, out


def test_candidates_filter_host_tier_cooldown_and_order(tmp_path, monkeypatch):
    bus = FakeBus()
    bus.add("AO-3", host="mac")
    bus.add("AO-1", host="any")
    bus.add("AO-2", host="server")          # another host's task
    bus.add("AO-4", host="mac", tier=3)     # tier 3 is never dispatched
    bus.add("AO-10", host="any")
    bus.add("AO-5", host="mac", status="backlog")
    dispatcher, started, out = make_dispatcher(bus, tmp_path)
    assert [t["id"] for t in dispatcher.candidates()] == ["AO-1", "AO-3", "AO-10"]
    now = [100.0]
    monkeypatch.setattr(dispatch, "clock", lambda: now[0])
    dispatcher.cooldown_until["AO-3"] = 900.0
    assert [t["id"] for t in dispatcher.candidates()] == ["AO-1", "AO-10"]   # on cooldown at 100
    now[0] = 2000.0
    assert [t["id"] for t in dispatcher.candidates()] == ["AO-1", "AO-3", "AO-10"]  # over at 2000


def test_cap_counts_this_hosts_bus_runs_and_own_children(tmp_path):
    bus = FakeBus()
    me = socket.gethostname()
    bus.add("AO-7", status="claimed", runs=[{"id": "AO-7-r1", "status": "claimed", "worker": me}])
    bus.add("AO-8", status="running", runs=[{"id": "AO-8-r1", "status": "running", "worker": "other-host"}])
    bus.add("AO-9", status="review", runs=[{"id": "AO-9-r1", "status": "review", "worker": me}])
    bus.add("AO-1")
    bus.add("AO-2")
    bus.add("AO-3")
    dispatcher, started, out = make_dispatcher(bus, tmp_path, AOS_DISPATCH_MAX_RUNS="2")
    assert dispatcher.live_on_bus() == {"AO-7"}       # another host's run and an ended run do not count
    assert dispatcher.poll() == 1 and started == ["AO-1"]
    assert "cap 2 reached; still waiting: AO-2, AO-3" in out.getvalue()
    assert dispatcher.poll() == 0 and started == ["AO-1"]   # the child counts while it is alive
    assert "2 live run(s) at the cap 2; 2 ready task(s) wait: AO-2, AO-3" in out.getvalue()
    bus.tasks["AO-7"]["status"] = "review"
    bus.tasks["AO-7"]["runs"][0]["status"] = "review"
    assert dispatcher.poll() == 1 and started == ["AO-1", "AO-2"]


def test_cap_one_by_default_starts_one_and_the_rest_wait(tmp_path):
    bus = FakeBus()
    bus.add("AO-1")
    bus.add("AO-2")
    dispatcher, started, out = make_dispatcher(bus, tmp_path)
    assert dispatcher.poll() == 1 and started == ["AO-1"]
    assert dispatcher.poll() == 0 and started == ["AO-1"]


def test_bus_error_starts_nothing(tmp_path):
    bus = FakeBus()
    bus.add("AO-1")
    bus.down = True
    dispatcher, started, out = make_dispatcher(bus, tmp_path)
    assert dispatcher.poll() == 0 and started == []
    assert "live runs unknown (bus error); nothing started" in out.getvalue()


def test_exit_while_ready_cools_down_then_unreadies_after_three(tmp_path, monkeypatch):
    bus = FakeBus()
    bus.add("AO-1")
    now = [1000.0]
    monkeypatch.setattr(dispatch, "clock", lambda: now[0])
    dispatcher, started, out = make_dispatcher(bus, tmp_path, AOS_DISPATCH_COOLDOWN_SECONDS="300")
    for strike in (1, 2):
        assert dispatcher.poll() == 1 and len(started) == strike
        dispatcher.children["AO-1"].process.returncode = 1   # a refusal before claim or a claim conflict
        assert dispatcher.poll() == 0                        # reaped, on cooldown
        assert f"AO-1: runner exited 1 with the task still ready ({strike}/3); cooldown 300 s" in out.getvalue()
        assert dispatcher.cooldown_until["AO-1"] == now[0] + 300
        assert dispatcher.poll() == 0 and len(started) == strike  # still on cooldown
        now[0] += 301
    assert bus.unready_posts() == []
    assert dispatcher.poll() == 1 and len(started) == 3
    dispatcher.children["AO-1"].process.returncode = 1
    assert dispatcher.poll() == 0
    assert bus.unready_posts() == ["AO-1"] and bus.tasks["AO-1"]["status"] == "backlog"
    assert ("AO-1: runner exited 1 with the task still ready, 3 times in a row; moved back to backlog; "
            f"last log {tmp_path / 'logs' / 'AO-1.log'}") in out.getvalue()
    assert "AO-1" not in dispatcher.strikes and "AO-1" not in dispatcher.cooldown_until
    now[0] += 1000
    assert dispatcher.poll() == 0 and len(started) == 3   # backlog: not a candidate


def test_exit_after_a_claim_resets_the_strikes(tmp_path, monkeypatch):
    bus = FakeBus()
    bus.add("AO-1")
    now = [1000.0]
    monkeypatch.setattr(dispatch, "clock", lambda: now[0])
    dispatcher, started, out = make_dispatcher(bus, tmp_path, AOS_DISPATCH_COOLDOWN_SECONDS="10")
    assert dispatcher.poll() == 1
    dispatcher.children["AO-1"].process.returncode = 1
    assert dispatcher.poll() == 0 and dispatcher.strikes["AO-1"] == 1
    now[0] += 11
    assert dispatcher.poll() == 1 and len(started) == 2
    bus.tasks["AO-1"]["status"] = "review"   # this attempt claimed and ended on the bus
    dispatcher.children["AO-1"].process.returncode = 0
    dispatcher.poll()
    assert "AO-1: runner exited 0; task review" in out.getvalue()
    assert "AO-1" not in dispatcher.strikes and bus.unready_posts() == []


@pytest.mark.parametrize("failure", ["refused", "unreachable"])
def test_a_failed_unready_keeps_the_cooldown_and_the_count(tmp_path, monkeypatch, failure):
    """Three strikes and the bus cannot take the move: the task is not redispatched at once, and the
    next exit retries the move instead of starting the three strikes over."""
    bus = FakeBus()
    bus.add("AO-1")
    now = [1000.0]
    monkeypatch.setattr(dispatch, "clock", lambda: now[0])
    dispatcher, started, out = make_dispatcher(bus, tmp_path, AOS_DISPATCH_COOLDOWN_SECONDS="300")
    for strike in (1, 2):
        assert dispatcher.poll() == 1 and len(started) == strike
        dispatcher.children["AO-1"].process.returncode = 1
        assert dispatcher.poll() == 0
        now[0] += 301
    assert dispatcher.poll() == 1 and len(started) == 3
    dispatcher.children["AO-1"].process.returncode = 1
    bus.unready_answer = (httpx.Response(503, text="restarting") if failure == "refused"
                          else httpx.ConnectError("connection refused"))
    assert dispatcher.poll() == 0
    assert bus.unready_posts() == ["AO-1"] and bus.tasks["AO-1"]["status"] == "ready"
    expected = ("unready refused: 503 restarting" if failure == "refused"
                else "unready failed: bus unreachable (ConnectError)")
    assert f"AO-1: runner exited 1 with the task still ready, 3 times in a row; {expected}" in out.getvalue()
    assert dispatcher.strikes["AO-1"] == 3 and dispatcher.cooldown_until["AO-1"] == now[0] + 300
    assert dispatcher.poll() == 0 and len(started) == 3      # still ready on the bus, but on cooldown here
    now[0] += 301
    bus.unready_answer = None
    assert dispatcher.poll() == 1 and len(started) == 4      # cooldown over: one more attempt
    dispatcher.children["AO-1"].process.returncode = 1
    assert dispatcher.poll() == 0                            # the move is retried at once, not after three more
    assert bus.unready_posts() == ["AO-1", "AO-1"] and bus.tasks["AO-1"]["status"] == "backlog"
    assert "AO-1: runner exited 1 with the task still ready, 4 times in a row; moved back to backlog" in out.getvalue()
    assert "AO-1" not in dispatcher.strikes and "AO-1" not in dispatcher.cooldown_until


def test_an_answer_that_is_not_json_is_logged_and_the_poll_goes_on(tmp_path):
    """A 200 without JSON (a proxy page, a wrong AOS_URL) is a bus error for this poll, not a crash of
    the dispatcher process."""
    bus = FakeBus()
    bus.add("AO-1")
    bus.listing_body = "<html>maintenance</html>"
    dispatcher, started, out = make_dispatcher(bus, tmp_path)
    assert dispatcher.run(once=True) == 0 and started == []
    assert "bus answered GET /tasks without JSON: <html>maintenance</html>" in out.getvalue()
    assert "live runs unknown (bus error); nothing started" in out.getvalue()
    bus.listing_body = None
    assert dispatcher.poll() == 1 and started == ["AO-1"]


def test_once_runs_exactly_one_poll(tmp_path):
    bus = FakeBus()
    bus.add("AO-1")
    dispatcher, started, out = make_dispatcher(bus, tmp_path)
    assert dispatcher.run(once=True) == 0
    assert dispatcher.polls == 1 and started == ["AO-1"]
    assert "one poll" in out.getvalue() and "dispatcher stopping; 1 runner(s) keep running" in out.getvalue()


def test_stop_ends_the_loop_without_touching_children(tmp_path, monkeypatch):
    bus = FakeBus()
    bus.add("AO-1")
    monkeypatch.setattr(dispatch, "clock", time.monotonic)
    dispatcher, started, out = make_dispatcher(bus, tmp_path, AOS_DISPATCH_POLL_SECONDS="1")
    threading.Timer(0.3, dispatcher.stop.set).start()
    assert dispatcher.run() == 0
    assert dispatcher.polls == 1 and dispatcher.children["AO-1"].process.poll() is None
    assert "dispatcher stopping; 1 runner(s) keep running" in out.getvalue()


# -- real runner processes: a stub agent_os.openshell_runner on PYTHONPATH -------------------------

STUB_RUNNER = textwrap.dedent("""\
    import json, os, sys, time
    argv = sys.argv[1:]
    token = None
    if argv[:1] == ["--token-fd"]:
        with os.fdopen(int(argv[1]), "rb") as stream:
            token = stream.read().decode()
        argv = argv[2:]
    task = argv[-1]
    mode = os.environ.get("STUB_MODE", "exit0")
    record = {"argv": sys.argv[1:], "token": token, "aos_token_in_env": "AOS_TOKEN" in os.environ,
              "pid": os.getpid(), "sid": os.getsid(0), "safe_path": sys.flags.safe_path, "mode": mode, "stub": True}
    with open(os.path.join(os.environ["STUB_RECORD_DIR"], task + ".json"), "w") as f:
        json.dump(record, f)
    print("stub runner " + task + " " + mode, flush=True)
    if mode == "exit1":
        sys.exit(1)
    if mode == "sleep":
        time.sleep(120)
    sys.exit(0)
    """)


def stub_package(tmp_path: Path) -> Path:
    """A shadow `agent_os` package for PYTHONPATH: its openshell_runner is the stub; every other module
    comes from the real package through the extended __path__ (so `-m agent_os.dispatch` stays real)."""
    root = tmp_path / "stubpkg"
    package = root / "agent_os"
    package.mkdir(parents=True)
    real = Path(agent_os.__file__).resolve().parent
    (package / "__init__.py").write_text(f"__path__.append({str(real)!r})\n")
    (package / "openshell_runner.py").write_text(STUB_RUNNER)
    return root


def child_env(tmp_path: Path, mode: str, **extra: str) -> dict[str, str]:
    records = tmp_path / "records"
    records.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
           "PYTHONPATH": str(stub_package(tmp_path)), "STUB_RECORD_DIR": str(records), "STUB_MODE": mode}
    env.update(extra)
    return env


def wait_for_records(tmp_path: Path, names: list[str], timeout: float = 30.0) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        paths = [tmp_path / "records" / f"{name}.json" for name in names]
        if all(p.exists() and p.stat().st_size > 0 for p in paths):
            return {name: json.loads(p.read_text()) for name, p in zip(names, paths)}
        time.sleep(0.05)
    raise AssertionError(f"stub records {names} did not appear")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_runner_gets_the_token_on_the_pipe_and_no_aos_token_in_its_environment(tmp_path):
    bus = FakeBus()
    settings = dispatch.settings_from(dict(BASE_ENV, AOS_DISPATCH_LOG_DIR=str(tmp_path / "logs")))
    client = httpx.Client(transport=httpx.MockTransport(bus), base_url="http://bus")
    out = io.StringIO()
    environ = child_env(tmp_path, "exit0", AOS_TOKEN="tok-123", AOS_URL="http://bus")
    dispatcher = dispatch.Dispatcher(client, settings, "tok-123", environ, out=out, err=out)
    assert "AOS_TOKEN" not in dispatcher.environ
    child = dispatcher.spawn("AO-1")
    assert child.process.wait(30) == 0
    record = wait_for_records(tmp_path, ["AO-1"])["AO-1"]
    assert record["stub"] is True and record["token"] == "tok-123" and record["aos_token_in_env"] is False
    assert record["argv"][0] == "--token-fd" and record["argv"][2:] == ["run", "AO-1"]
    assert record["safe_path"] is True                    # python -P
    assert record["sid"] == record["pid"]                 # its own session
    assert child.log_path.read_text() == "stub runner AO-1 exit0\n"
    assert child.log_path.stat().st_mode & 0o777 == 0o600
    assert child.log_path.name.startswith("AO-1-") and child.log_path.name.endswith("Z.log")
    assert f"AO-1: runner started, pid {child.process.pid}, log {child.log_path}" in out.getvalue()


def test_spawned_runner_exiting_while_ready_is_reaped_with_the_log_path(tmp_path, monkeypatch):
    bus = FakeBus()
    bus.add("AO-1")
    settings = dispatch.settings_from(dict(BASE_ENV, AOS_DISPATCH_LOG_DIR=str(tmp_path / "logs"),
                                           AOS_DISPATCH_COOLDOWN_SECONDS="60"))
    client = httpx.Client(transport=httpx.MockTransport(bus), base_url="http://bus")
    out = io.StringIO()
    dispatcher = dispatch.Dispatcher(client, settings, "tok", child_env(tmp_path, "exit1"), out=out, err=out)
    assert dispatcher.poll() == 1
    child = dispatcher.children["AO-1"]
    child.process.wait(30)
    assert dispatcher.poll() == 0
    assert f"AO-1: runner exited 1 with the task still ready (1/3); cooldown 60 s; log {child.log_path}" in out.getvalue()
    assert child.log_path.read_text() == "stub runner AO-1 exit1\n"


# -- the dispatcher as a process against a live bus -------------------------------------------------

@pytest.fixture
def live_bus(tmp_path: Path):
    settings = Settings(_env_file=None, BUS_TOKEN="live-token", BUS_DATA_DIR=tmp_path / "bus")
    app = create_app(settings, background=False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    client = httpx.Client(base_url=url, headers={"Authorization": "Bearer live-token"}, timeout=10, trust_env=False)
    deadline = time.monotonic() + 30
    while True:
        try:
            if client.get("/health").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError("the test bus did not answer /health")
        time.sleep(0.1)
    yield types.SimpleNamespace(url=url, token="live-token", client=client)
    server.should_exit = True
    thread.join(10)


def ready_task(bus, title: str) -> str:
    task = bus.client.post("/tasks", json={"repo": "demo", "title": title, "brief": "fix it",
                                           "acceptance": ["true"], "host": "mac"}).json()
    assert bus.client.post(f"/tasks/{task['id']}/ready").status_code == 200
    return task["id"]


def dispatcher_env(tmp_path: Path, bus, mode: str, **extra: str) -> dict[str, str]:
    return child_env(tmp_path, mode, AOS_URL=bus.url, AOS_TOKEN=bus.token, AOS_HOST="mac",
                     AOS_DISPATCH_POLL_SECONDS="1", AOS_DISPATCH_MAX_RUNS="2", AOS_DISPATCH_COOLDOWN_SECONDS="5",
                     AOS_DISPATCH_LOG_DIR=str(tmp_path / "logs"), **extra)


def test_children_survive_a_dispatcher_sigterm(tmp_path, live_bus):
    first, second, third = (ready_task(live_bus, t) for t in ("one", "two", "three"))
    proc = subprocess.Popen([sys.executable, "-m", "agent_os.dispatch"], env=dispatcher_env(tmp_path, live_bus, "sleep"),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []

    def pump() -> None:  # the dispatcher flushes every line, so they arrive as it logs them
        for line in iter(proc.stdout.readline, ""):
            lines.append(line)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    at_cap = f"2 live run(s) at the cap 2; 1 ready task(s) wait: {third}"
    pids: list[int] = []
    try:
        records = wait_for_records(tmp_path, [first, second])
        pids = [records[first]["pid"], records[second]["pid"]]
        assert all(r["token"] == "live-token" and r["aos_token_in_env"] is False for r in records.values())
        assert not (tmp_path / "records" / f"{third}.json").exists()   # the third waits at the cap
        # Two more polls at the cap, observed in the log rather than assumed from a fixed sleep:
        # a 2.5 s sleep failed on a loaded machine (2026-10-14), where the polls came slower.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and sum(at_cap in line for line in list(lines)) < 2:
            time.sleep(0.1)
        assert sum(at_cap in line for line in list(lines)) >= 2, "".join(lines)
        assert not (tmp_path / "records" / f"{third}.json").exists()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
        reader.join(timeout=10)
        output = "".join(lines)
        assert proc.returncode == 0, output
        assert "dispatcher stopping; 2 runner(s) keep running" in output, output
        assert all(alive(pid) for pid in pids)              # SIGTERM to the dispatcher never reached them
        assert all(records[t]["sid"] == records[t]["pid"] for t in (first, second))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_once_as_a_process_polls_once_and_leaves_the_runner_running(tmp_path, live_bus):
    task_id = ready_task(live_bus, "once")
    result = subprocess.run([sys.executable, "-m", "agent_os.dispatch", "--once"],
                            env=dispatcher_env(tmp_path, live_bus, "sleep"), capture_output=True, text=True, timeout=60)
    pid = None
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        record = wait_for_records(tmp_path, [task_id])[task_id]
        pid = record["pid"]
        assert alive(pid) and "dispatcher stopping; 1 runner(s) keep running" in result.stdout
        assert f"{task_id}: runner started, pid {pid}" in result.stdout
    finally:
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_main_reads_the_token_once_and_removes_it_from_its_environment(tmp_path, live_bus, monkeypatch, capsys):
    task_id = ready_task(live_bus, "main")
    for key, value in dispatcher_env(tmp_path, live_bus, "exit0").items():
        monkeypatch.setenv(key, value)
    assert dispatch.main(["--once"]) == 0
    assert "AOS_TOKEN" not in os.environ
    record = wait_for_records(tmp_path, [task_id])[task_id]
    assert record["token"] == "live-token" and record["aos_token_in_env"] is False
    assert f"{task_id}: runner started" in capsys.readouterr().out
    monkeypatch.delenv("AOS_TOKEN", raising=False)
    assert dispatch.main(["--once"]) == 2 and "AOS_TOKEN is required" in capsys.readouterr().err


def test_main_refuses_bad_settings_before_touching_the_bus(monkeypatch, capsys):
    monkeypatch.setenv("AOS_TOKEN", "tok")
    monkeypatch.setenv("AOS_HOST", "any")
    assert dispatch.main(["--once"]) == 2
    assert "AOS_HOST must be mac or server" in capsys.readouterr().err


@pytest.mark.parametrize("code", [200, 503])
def test_poll_publishes_its_capacity_before_counting_candidates(tmp_path, code):
    bus = FakeBus()
    bus.capacity_code = code
    bus.add("AO-1")
    dispatcher, started, _ = make_dispatcher(bus, tmp_path, AOS_DISPATCH_MAX_RUNS="1")
    assert dispatcher.poll() == 1 and started == ["AO-1"]
    assert bus.capacity_posts == [{"max_runs": 1, "live": 0}]
    path = f"/hosts/{dispatcher.worker}/capacity"
    index = next(i for i, call in enumerate(bus.calls) if call[0:2] == ("POST", path))
    assert all(call[2].get("status") != "ready" for call in bus.calls[:index])
    assert dispatcher.poll() == 0
    assert bus.capacity_posts[-1] == {"max_runs": 1, "live": 1}  # spawned, not yet claimed
    bus.tasks["AO-1"].update(status="running", runs=[{"id": "AO-1-r1", "status": "running", "worker": dispatcher.worker}])
    assert dispatcher.poll() == 0
    assert bus.capacity_posts[-1] == {"max_runs": 1, "live": 1}  # union, never double counted
