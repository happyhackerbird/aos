"""`aos integrate --backend openshell` (design docs/design/2026-10-13-integrate-openshell.md): the merged
candidate's checks in a fresh OpenShell sandbox, against the adapter's fake `openshell` and the stub `docker`."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
import pytest

from agent_os import integrate, openshell_integrate, openshell_policy, openshell_runner as adapter, openshell_services, resources
from agent_os.resources import ServiceError
from test_integrate import _base_env, _git, _make_branch, _task_to_review, client, repos_root, source_repo, worktree_root  # noqa: F401
from test_openshell_runner import BASE_ENV, FakeShell, POLICY, git, make_repo, tar_member_text
from runner_fixtures import POLICY_TOML, _write_services

# Lane P: the bus prefix of the settings these tests use (BASE_ENV's AOS_URL).
PREFIX = adapter.bus_prefix(BASE_ENV["AOS_URL"])
BUS_IDENTITY = adapter.bus_identity(BASE_ENV["AOS_URL"])


def test_sandbox_name_and_attempt_id():
    assert openshell_integrate.sandbox_name("AO-12", PREFIX) == f"{PREFIX}-ao-12-int"
    assert openshell_integrate.sandbox_name("AO_12 x", PREFIX) == f"{PREFIX}-ao-12-x-int"
    assert len(openshell_integrate.sandbox_name("AO-12345678", PREFIX)) <= adapter.MAX_SANDBOX_NAME
    with pytest.raises(adapter.AdapterError, match="longer than"):
        openshell_integrate.sandbox_name("AO-123456789", PREFIX)
    assert openshell_integrate.attempt_id("AO-12", PREFIX) == f"{PREFIX}-integrate-AO-12"
    assert openshell_integrate.sandbox_name("AO-12", "zz") == "zz-ao-12-int"


def test_write_owner_records_kind_attempt_and_process(tmp_path):
    state = tmp_path / f"{PREFIX}-ao-12-int"
    state.mkdir()
    openshell_integrate.write_owner(state, task_id="AO-12", run_id="AO-12-r1", name=f"{PREFIX}-ao-12-int", host="mac",
                                    bus=BUS_IDENTITY, prefix=PREFIX)
    marker = state / "owner.json"
    assert oct(marker.stat().st_mode & 0o777) == "0o600"
    owner = json.loads(marker.read_text())
    assert owner["kind"] == "integrate" and owner["attempt"] == f"{PREFIX}-integrate-AO-12"
    assert owner["bus"] == BUS_IDENTITY and owner["prefix"] == PREFIX  # lane P (P4)
    assert owner["sandboxes"] == [f"{PREFIX}-ao-12-int"] and owner["run_id"] == "AO-12-r1" and owner["task_id"] == "AO-12"
    assert owner["pid"] == os.getpid() and owner["pid_created"] == psutil.Process().create_time()
    assert owner["worker"] == adapter.socket.gethostname() and owner["host"] == "mac" and owner["created"]


def test_attempt_alive_rules(monkeypatch):
    me = psutil.Process()
    assert openshell_integrate.attempt_alive({"pid": me.pid, "pid_created": me.create_time()}) is True
    assert openshell_integrate.attempt_alive({"pid": me.pid, "pid_created": me.create_time() - 1000}) is False
    gone = subprocess.Popen(["true"])
    gone.wait()
    assert openshell_integrate.attempt_alive({"pid": gone.pid, "pid_created": 0.0}) is False
    # No usable process identity: left alone rather than deleted.
    assert openshell_integrate.attempt_alive({}) is True
    assert openshell_integrate.attempt_alive({"pid": "x", "pid_created": 1.0}) is True
    assert openshell_integrate.attempt_alive({"pid": -1, "pid_created": 1.0}) is True

    def denied(pid):
        raise psutil.AccessDenied(pid=1)

    monkeypatch.setattr(openshell_integrate.psutil, "Process", denied)  # an unreadable process counts as alive
    assert openshell_integrate.attempt_alive({"pid": 1, "pid_created": 1.0}) is True


# -- Attempt: the fresh sandbox, the database, the verdict, the cleanup ------------------------------

class EchoingShell(FakeShell):
    """The acceptance output echoes the uploaded services.env, so the posted output must be redacted."""

    def direct_read(self, name, argv):
        # The output tail is a direct read (F137): `/usr/bin/tail -c 3000 /sandbox/accept-<i>.out`.
        if (argv[:3] == ("/usr/bin/tail", "-c", "3000") and re.fullmatch(r"/sandbox/accept-\d+\.out", argv[-1])
                and "/sandbox/services.env" in self.uploads):
            return 0, self.uploads["/sandbox/services.env"].decode()
        return super().direct_read(name, argv)


class NeverDoneShell(FakeShell):
    def exists(self, name, path):  # `/usr/bin/test -e <path>`, a direct read (F137)
        if path == "/sandbox/accept.done":
            return 1, ""
        return super().exists(name, path)


def _settings(tmp_path):
    return adapter.settings_from(dict(BASE_ENV, AOS_OPENSHELL_STATE_DIR=str(tmp_path / "state" / "nested")))


def _attempt(monkeypatch, tmp_path, shell, *, plan=None, policy_text=POLICY, task_id="AO-9"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(adapter, "openshell", shell)
    monkeypatch.setattr(adapter, "pause", lambda seconds: None)
    ticks = iter(range(0, 10**6, 50))  # the polling loops never wait and never spin on the real clock
    monkeypatch.setattr(adapter, "clock", lambda: next(ticks))
    repo = make_repo(tmp_path, policy_text)
    merge_sha = git(repo, "rev-parse", "HEAD")
    attempt = openshell_integrate.Attempt(_settings(tmp_path), plan, openshell_policy.load_policy_text(policy_text),
                                          task_id=task_id, run_id=f"{task_id}-r1", environ=dict(os.environ))
    return attempt, str(repo), merge_sha


def _successor(attempt, *, task_id=None, state_dir=None):
    settings = attempt.settings if state_dir is None else adapter.settings_from(
        dict(BASE_ENV, AOS_OPENSHELL_STATE_DIR=str(state_dir)))
    task_id = task_id or attempt.task_id
    return openshell_integrate.Attempt(settings, attempt.plan, attempt.repo_policy, task_id=task_id,
                                       run_id=f"{task_id}-r2", environ=attempt.environ)


def _lock_path(state):
    digest = hashlib.sha256(str(state.resolve()).encode()).hexdigest()
    return state.parent / ".integrate-locks" / f"workspace-{digest}.lock"


def _assert_lock_available(state):
    with resources.lock_worktree(str(state), str(state.parent / ".integrate-locks")):
        pass


def _state_files(state):
    return {path.name: path.read_bytes() for path in state.iterdir() if path.is_file()}


def _dead_attempt_state(tmp_path):
    state = Path(_settings(tmp_path).state_dir) / f"{PREFIX}-ao-9-int"
    state.mkdir(parents=True)
    openshell_integrate.write_owner(state, task_id="AO-9", run_id="AO-9-r1", name=state.name, host="mac",
                                    bus=BUS_IDENTITY, prefix=PREFIX)
    marker = state / adapter.OWNER_FILE
    owner = json.loads(marker.read_text())
    owner["pid_created"] -= 1000  # this PID exists, but cannot be the process recorded by the marker
    marker.write_text(json.dumps(owner))
    (state / adapter.SERVICES_FILE).write_text('{"postgres": true}')
    (state / "job.tgz").write_bytes(b"old job")
    return state, owner


def _container(task_id: str) -> str:
    return hashlib.sha256(f"aos-{PREFIX}-integrate-{task_id}-postgres".encode()).hexdigest()


PLAN = openshell_services.Plan("postgres:16@sha256:" + "0" * 64, "127.0.0.1", "192.168.5.2")


def test_check_passes_and_cleanup_removes_everything(monkeypatch, tmp_path):
    shell = FakeShell(accept_tail=b"8 passed")
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path, shell)
    status, output = attempt.check(repo, merge_sha, ["pytest -q"])
    assert status == "passed" and f"$ pytest -q\n8 passed\n[exit 0] (fresh sandbox {PREFIX}-ao-9-int)" == output
    state = tmp_path / "state" / "nested" / f"{PREFIX}-ao-9-int"
    assert oct(state.stat().st_mode & 0o777) == "0o700"
    owner = json.loads((state / "owner.json").read_text())
    assert owner["kind"] == "integrate" and owner["sandboxes"] == [f"{PREFIX}-ao-9-int"] and owner["pid"] == os.getpid()
    assert not (state / "services.json").exists()
    create = next(c for c in shell.calls if c[:2] == ("sandbox", "create"))
    assert create[create.index("--from") + 1] == BASE_ENV["AOS_OPENSHELL_IMAGE"]
    assert shell.events.index(("policy", f"{PREFIX}-ao-9-int")) < shell.events.index(("release", f"{PREFIX}-ao-9-int"))
    assert "aos-bus" in shell.policies[f"{PREFIX}-ao-9-int"] and "aos-services" not in shell.policies[f"{PREFIX}-ao-9-int"]
    job = tmp_path / "seen.tgz"
    job.write_bytes(shell.uploads["/sandbox/incoming/job.tgz"])
    with tarfile.open(job) as tar:
        names = set(tar.getnames())
    assert {"app.py", "aos-policy.toml", ".git/HEAD"} <= names
    assert tar_member_text(job, ".git/HEAD").strip() == "ref: refs/heads/integrate/AO-9"
    assert f"{PREFIX}-ao-9-int" in shell.live and attempt.created
    err = io.StringIO()
    lines = attempt.cleanup(err)
    assert len(lines) == 1 and lines[0].startswith(f"sandbox {PREFIX}-ao-9-int deleted after")
    assert attempt.cleanup_failed is False and err.getvalue() == ""
    assert f"{PREFIX}-ao-9-int" not in shell.live
    assert not (state / "owner.json").exists() and not (state / "job.tgz").exists() and (state / "policy.yaml").exists()


def test_check_with_a_database_grants_it_hands_it_over_and_redacts(monkeypatch, tmp_path, docker_stub):
    shell = EchoingShell()
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path, shell, plan=PLAN)
    status, output = attempt.check(repo, merge_sha, ["pytest -q"])
    assert status == "passed", output
    password = docker_stub.password()
    assert password and len(password) == 32
    state = tmp_path / "state" / "nested" / f"{PREFIX}-ao-9-int"
    assert (state / "owner.json").exists() and (state / "services.json").read_text() == '{"postgres": true}'
    assert not (state / "services.env").exists()
    # The pre-sweep (a `ps` listing for the attempt's label) comes before the container starts.
    calls = docker_stub.calls()
    first_ps = next(i for i, c in enumerate(calls) if c[:1] == ["ps"] and f"label=aos.run={PREFIX}-integrate-AO-9" in c)
    run_at = next(i for i, c in enumerate(calls) if c[:1] == ["run"])
    assert first_ps < run_at
    assert "- host: 192.168.5.2\n        port: 55432" in shell.policies[f"{PREFIX}-ao-9-int"]
    order = [e for e in shell.events if e[0] in ("policy", "upload", "release")]
    assert order.index(("policy", f"{PREFIX}-ao-9-int")) < order.index(("upload", f"{PREFIX}-ao-9-int", "/sandbox/services.env"))
    assert order.index(("upload", f"{PREFIX}-ao-9-int", "/sandbox/services.env")) < order.index(("upload", f"{PREFIX}-ao-9-int", "/sandbox/incoming/job.tgz"))
    # Two secrets: the container's admin password (the stub records it from `docker run`'s environment) and
    # the login password inside DATABASE_URL. Neither reaches an argv, the policy or the output.
    env_text = shell.uploads["/sandbox/services.env"].decode()
    assert env_text.startswith("export DATABASE_URL=") and "@192.168.5.2:55432/aos" in env_text
    login = re.search(r"postgresql://aos:([^@]+)@", env_text).group(1)
    assert login and login != password
    for secret in (password, login):
        assert secret not in output
        assert secret not in shell.policies[f"{PREFIX}-ao-9-int"]
        for call in shell.calls:
            assert secret not in " ".join(call)
        for argv in calls:
            assert secret not in " ".join(argv)
    assert "***" in output
    err = io.StringIO()
    lines = attempt.cleanup(err)
    assert lines[0].startswith(f"sandbox {PREFIX}-ao-9-int deleted") and lines[1] == "postgres container removed"
    assert docker_stub.removed(_container("AO-9")) and docker_stub.containers() == {}
    assert not (state / "services.json").exists() and not (state / "owner.json").exists()
    assert attempt.cleanup_failed is False and err.getvalue() == ""


def test_attempt_names_and_database_label_carry_the_prefix(monkeypatch, tmp_path, docker_stub):
    # Lane P (P2, P3, P6 6; e8's S3 route b): the attempt id is `<prefix>-integrate-<task>`, so the pre-sweep, the
    # container and the cleanup act on this bus's attempt only; the host path's `integrate-AO-9` leftover (or another
    # bus's attempt of the same task) is never swept.
    host_path = docker_stub.add_container("aos-integrate-AO-9-postgres", {"aos.run": "integrate-AO-9", "aos.service": "postgres"})
    shell = FakeShell()
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path, shell, plan=PLAN)
    assert attempt.attempt == f"{PREFIX}-integrate-AO-9"
    assert attempt.check(repo, merge_sha, ["true"])[0] == "passed"
    name = f"{PREFIX}-ao-9-int"
    assert attempt.name == name and ("create", name) in shell.events
    owner = json.loads((tmp_path / "state" / "nested" / name / "owner.json").read_text())
    assert owner["attempt"] == f"{PREFIX}-integrate-AO-9" and owner["bus"] == BUS_IDENTITY and owner["prefix"] == PREFIX
    started = next(argv for argv in docker_stub.calls() if argv[:1] == ["run"])
    assert started[started.index("--name") + 1] == f"aos-{PREFIX}-integrate-AO-9-postgres"
    assert started[started.index("--label") + 1] == f"aos.run={PREFIX}-integrate-AO-9"
    attempt.cleanup(io.StringIO())
    listings = [argv for argv in docker_stub.calls() if argv[:1] == ["ps"]]
    assert len(listings) >= 4 and all(f"label=aos.run={PREFIX}-integrate-AO-9" in argv for argv in listings)
    assert not any("label=aos.run=integrate-AO-9" in argv for argv in docker_stub.calls())
    assert list(docker_stub.containers()) == [host_path] and docker_stub.removed(_container("AO-9"))


def test_failed_command_is_failed_with_the_exit_code(monkeypatch, tmp_path, docker_stub):
    shell = FakeShell(accept_rc="1", accept_tail=b"FAIL new note is first in the list")
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path, shell, plan=PLAN)
    status, output = attempt.check(repo, merge_sha, ["pytest -q", "scripts/check.sh"])
    assert status == "failed" and "[exit 1]" in output and "FAIL new note is first" in output and "scripts/check.sh" not in output
    attempt.cleanup(io.StringIO())
    assert f"{PREFIX}-ao-9-int" not in shell.live and docker_stub.containers() == {}


def test_error_cases_post_error_and_cleanup_still_removes_what_exists(monkeypatch, tmp_path, docker_stub):
    # The name is live (not ours): refused, the foreign sandbox untouched, no create.
    shell = FakeShell()
    shell.live.add(f"{PREFIX}-ao-9-int")
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path, shell, plan=PLAN)
    status, output = attempt.check(repo, merge_sha, ["true"])
    assert status == "error" and output == f"sandbox {PREFIX}-ao-9-int already exists; this run did not create it and leaves it alone"
    assert attempt.created is False and ("create", f"{PREFIX}-ao-9-int") not in shell.events
    lines = attempt.cleanup(io.StringIO())
    assert ("delete", f"{PREFIX}-ao-9-int") not in shell.events and f"{PREFIX}-ao-9-int" in shell.live
    assert lines == ["postgres container removed"] and docker_stub.containers() == {}

    # A tracked path matches the policy's exclude: provision refuses, nothing created.
    shell = FakeShell()
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path / "b", shell)
    (Path(repo) / ".env").write_text("SECRET=1\n")
    git(Path(repo), "add", ".env")
    git(Path(repo), "commit", "-qm", "oops")
    status, output = attempt.check(repo, git(Path(repo), "rev-parse", "HEAD"), ["true"])
    assert status == "error" and "matches tracked path .env" in output and not shell.events
    assert attempt.cleanup(io.StringIO()) == []

    # The sandbox never reports done: a timeout is `error`, and the sandbox is deleted.
    shell = NeverDoneShell()
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path / "c", shell)
    status, output = attempt.check(repo, merge_sha, ["true"])
    assert status == "error" and output == "acceptance did not complete: /sandbox/accept.done did not appear within 900 s"
    attempt.cleanup(io.StringIO())
    assert f"{PREFIX}-ao-9-int" not in shell.live

    # Any other failure of the attempt is `error` with its message.
    def boom(*args, **kwargs):
        raise adapter.AdapterError("boom")

    shell = FakeShell()
    monkeypatch.setattr(adapter, "accept_fresh", boom)
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path / "d", shell)
    assert attempt.check(repo, merge_sha, ["true"]) == ("error", "boom")
    attempt.cleanup(io.StringIO())


def test_cleanup_failure_keeps_the_marker_and_says_so(monkeypatch, tmp_path, docker_stub):
    shell = FakeShell(delete_sticks=True)
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path, shell, plan=PLAN)
    assert attempt.check(repo, merge_sha, ["true"])[0] == "passed"
    err = io.StringIO()
    lines = attempt.cleanup(err)
    assert lines[0].startswith(f"sandbox {PREFIX}-ao-9-int cleanup failed: ") and lines[1] == "postgres container removed"
    assert attempt.cleanup_failed is True and err.getvalue() == openshell_integrate.CLEANUP_FAILED + "\n"
    state = tmp_path / "state" / "nested" / f"{PREFIX}-ao-9-int"
    assert (state / "owner.json").exists() and not (state / "job.tgz").exists() and not (state / "services.json").exists()
    _assert_lock_available(state)

    def stuck(run_id, environ):
        raise ServiceError("postgres: stop: 1 container(s) remain")

    shell = FakeShell()
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path / "b", shell, plan=PLAN)
    assert attempt.check(repo, merge_sha, ["true"])[0] == "passed"
    monkeypatch.setattr(openshell_services, "stop", stuck)
    err = io.StringIO()
    lines = attempt.cleanup(err)
    assert lines[0].startswith(f"sandbox {PREFIX}-ao-9-int deleted") and lines[1] == "postgres cleanup failed: postgres: stop: 1 container(s) remain"
    state = tmp_path / "b" / "state" / "nested" / f"{PREFIX}-ao-9-int"
    assert (state / "owner.json").exists() and (state / "services.json").exists() and attempt.cleanup_failed is True
    _assert_lock_available(state)


def test_a_surviving_marker_of_an_earlier_attempt_is_refused_and_left_untouched(monkeypatch, tmp_path, docker_stub):
    shell = FakeShell()
    shell.live.add(f"{PREFIX}-ao-9-int")
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path, shell, plan=PLAN)
    state = tmp_path / "state" / "nested" / f"{PREFIX}-ao-9-int"
    state.mkdir(parents=True)
    (state / "owner.json").write_text('{"earlier": true}')
    status, output = attempt.check(repo, merge_sha, ["true"])
    assert status == "error" and f"{PREFIX}-ao-9-int" in output and "still owns" in output
    # Lane F (F5): every reason reconcile skips a marker, lane P's two included.
    assert ("(an unreadable marker, a worker name that no longer matches this host, a marker of another bus, "
            "or a legacy marker without a bus field)") in output
    assert shell.calls == [] and docker_stub.calls() == []
    assert attempt.state is None and attempt.created is False
    _assert_lock_available(state)  # a refused marker ends the lock ownership immediately
    assert attempt.cleanup(io.StringIO()) == []
    assert (state / "owner.json").read_text() == '{"earlier": true}' and f"{PREFIX}-ao-9-int" in shell.live


@pytest.mark.parametrize("task_id, alias_root", [("AO-9", False), ("AO_9", True)])
def test_concurrent_attempt_is_refused_before_winner_writes_marker(
        monkeypatch, tmp_path, docker_stub, task_id, alias_root):
    shell = FakeShell()
    winner, repo, merge_sha = _attempt(monkeypatch, tmp_path, shell, plan=PLAN)
    root = Path(winner.settings.state_dir)
    alias = tmp_path / "state-alias"
    alias.symlink_to(root, target_is_directory=True)
    loser = _successor(winner, task_id=task_id, state_dir=alias if alias_root else None)
    state = root / f"{PREFIX}-ao-9-int"
    entered, release = threading.Event(), threading.Event()
    write_owner = openshell_integrate.write_owner

    def pause_first_owner(*args, **kwargs):
        if not entered.is_set():  # only the winner waits; a broken loser must never join a barrier
            entered.set()
            assert release.wait(10), "winner was not released"
        return write_owner(*args, **kwargs)

    monkeypatch.setattr(openshell_integrate, "write_owner", pause_first_owner)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(winner.check, repo, merge_sha, ["true"])
        try:
            assert entered.wait(10), "winner did not reach marker creation"
            assert not (state / adapter.OWNER_FILE).exists()
            (state / "result.tgz").write_bytes(b"winner scratch")
            before = _state_files(state)
            status, output = loser.check(repo, merge_sha, ["true"])
            assert status == "error" and "owns" in output
            assert loser.state is None and loser.name is None and not loser.database_owned and not loser.created
            assert loser.cleanup(io.StringIO()) == []
            assert _state_files(state) == before
            assert shell.calls == [] and docker_stub.calls() == []
            release.set()
            assert pending.result(timeout=10)[0] == "passed"
            calls, database_calls, files = list(shell.calls), docker_stub.calls(), _state_files(state)
            assert loser.cleanup(io.StringIO()) == []
            assert shell.calls == calls and docker_stub.calls() == database_calls and _state_files(state) == files
            assert state.name in shell.live
        finally:
            release.set()
            try:
                pending.result(timeout=10)
            finally:
                winner.cleanup(io.StringIO())
                loser.cleanup(io.StringIO())


def test_attempt_lock_excludes_another_process_and_survives_sequential_reuse(monkeypatch, tmp_path):
    shell = FakeShell()
    first, repo, merge_sha = _attempt(monkeypatch, tmp_path, shell)
    state = Path(first.settings.state_dir) / f"{PREFIX}-ao-9-int"
    probe = """import fcntl, os, sys
with os.fdopen(os.open(sys.argv[1], os.O_RDWR), "r+") as stream:
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(75)
"""

    def probe_lock(expected):
        result = subprocess.run([sys.executable, "-c", probe, str(_lock_path(state))],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == expected, (result.stdout, result.stderr)

    try:
        assert first.check(repo, merge_sha, ["true"])[0] == "passed"
        lock = _lock_path(state)
        inode = lock.stat().st_ino
        assert lock.parent == state.parent / ".integrate-locks" and lock.stat().st_mode & 0o777 == 0o600
        probe_lock(75)  # the check returned; ownership still covers result posting
    finally:
        first.cleanup(io.StringIO())
    assert lock.stat().st_ino == inode
    probe_lock(0)
    second = _successor(first)
    try:
        assert second.check(repo, merge_sha, ["true"])[0] == "passed"
        files, calls = _state_files(state), list(shell.calls)
        assert first.cleanup(io.StringIO()) == []
        assert _state_files(state) == files and shell.calls == calls and state.name in shell.live
        assert first.state is None and first.name is None and first.env_path is None
        assert not first.created and not first.database_owned
        assert lock.stat().st_ino == inode
        probe_lock(75)
    finally:
        second.cleanup(io.StringIO())
    assert lock.stat().st_ino == inode
    probe_lock(0)


def test_same_attempt_object_can_check_again_after_cleanup(monkeypatch, tmp_path):
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path, FakeShell())
    for _ in range(2):
        try:
            assert attempt.check(repo, merge_sha, ["true"])[0] == "passed"
        finally:
            attempt.cleanup(io.StringIO())


@pytest.mark.parametrize("step", ["marker", "acceptance"])
@pytest.mark.parametrize("exception", [RuntimeError("check failed"), KeyboardInterrupt("check cancelled")])
def test_check_exception_or_cancellation_releases_ownership_in_cleanup(monkeypatch, tmp_path, step, exception):
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path, FakeShell())
    state = Path(attempt.settings.state_dir) / f"{PREFIX}-ao-9-int"

    def fail(*args, **kwargs):
        raise exception

    with monkeypatch.context() as broken:
        broken.setattr(openshell_integrate if step == "marker" else adapter,
                       "write_owner" if step == "marker" else "accept_fresh", fail)
        try:
            if isinstance(exception, KeyboardInterrupt):
                with pytest.raises(KeyboardInterrupt, match="check cancelled"):
                    attempt.check(repo, merge_sha, ["true"])
            else:
                assert attempt.check(repo, merge_sha, ["true"]) == ("error", "check failed")
            with pytest.raises(BlockingIOError):
                with resources.lock_worktree(str(state), str(state.parent / ".integrate-locks")):
                    pass
        finally:
            attempt.cleanup(io.StringIO())
    assert not (state / adapter.OWNER_FILE).exists()
    _assert_lock_available(state)
    successor = _successor(attempt)
    try:
        assert successor.check(repo, merge_sha, ["true"])[0] == "passed"
    finally:
        successor.cleanup(io.StringIO())


@pytest.mark.parametrize("step", ["sandbox", "scratch"])
@pytest.mark.parametrize("exception", [RuntimeError("cleanup failed"), KeyboardInterrupt("cleanup cancelled")])
def test_cleanup_failure_or_interrupt_retains_marker_and_releases_descriptor(
        monkeypatch, tmp_path, step, exception):
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path, FakeShell())
    assert attempt.check(repo, merge_sha, ["true"])[0] == "passed"
    state = Path(attempt.settings.state_dir) / f"{PREFIX}-ao-9-int"
    marker = state / adapter.OWNER_FILE
    before = marker.read_bytes()

    def fail(*args, **kwargs):
        raise exception

    with monkeypatch.context() as broken:
        broken.setattr(adapter, "delete_sandbox" if step == "sandbox" else "remove_scratch", fail)
        err = io.StringIO()
        assert any("cleanup failed" in line for line in attempt.cleanup(err))
        assert attempt.cleanup_failed and openshell_integrate.CLEANUP_FAILED in err.getvalue()
    assert marker.read_bytes() == before
    _assert_lock_available(state)
    assert attempt.cleanup(io.StringIO()) == [] and marker.read_bytes() == before
    # Recovery, not an ended object, now owns the retained crash record.
    monkeypatch.setattr(openshell_integrate, "attempt_alive", lambda owner: False)
    assert openshell_integrate.reconcile_attempt(state, json.loads(before), io.StringIO(), PREFIX) == 1
    successor = _successor(attempt)
    try:
        assert successor.check(repo, merge_sha, ["true"])[0] == "passed"
        files = _state_files(state)
        assert attempt.cleanup(io.StringIO()) == [] and _state_files(state) == files
    finally:
        successor.cleanup(io.StringIO())


def test_cleanup_retains_marker_when_scratch_removal_is_unconfirmed(monkeypatch, tmp_path):
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path, FakeShell())
    assert attempt.check(repo, merge_sha, ["true"])[0] == "passed"
    state = Path(attempt.settings.state_dir) / f"{PREFIX}-ao-9-int"
    monkeypatch.setattr(adapter, "remove_scratch", lambda directory: None)
    lines = attempt.cleanup(io.StringIO())
    assert attempt.cleanup_failed and any("scratch files remain: job.tgz" in line for line in lines)
    assert (state / adapter.OWNER_FILE).exists() and (state / "job.tgz").exists()
    _assert_lock_available(state)


def test_reconcile_uses_attempt_lock_even_when_process_looks_dead(monkeypatch, tmp_path, docker_stub):
    shell = FakeShell()
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path, shell, plan=PLAN)
    try:
        assert attempt.check(repo, merge_sha, ["true"])[0] == "passed"
        state = Path(attempt.settings.state_dir) / f"{PREFIX}-ao-9-int"
        before = _state_files(state)
        calls, database_calls = list(shell.calls), docker_stub.calls()
        monkeypatch.setattr(openshell_integrate, "attempt_alive", lambda owner: False)
        assert openshell_integrate.reconcile_attempt(
            state, json.loads(before[adapter.OWNER_FILE]), io.StringIO(), PREFIX) == 0
        assert _state_files(state) == before and shell.calls == calls and docker_stub.calls() == database_calls
    finally:
        attempt.cleanup(io.StringIO())


@pytest.mark.parametrize("timing", ["before", "under-lock"])
@pytest.mark.parametrize("change", ["successor", "other-bus", "other-worker", "disappeared"])
def test_reconcile_revalidates_owner_snapshot_after_lock_acquisition(monkeypatch, tmp_path, change, timing):
    state, owner = _dead_attempt_state(tmp_path)
    marker = state / adapter.OWNER_FILE
    lock_worktree = resources.lock_worktree
    mutations = []

    def change_marker():
        if change == "disappeared":
            marker.unlink()
        else:
            current = dict(owner)
            if change == "successor":
                current.update(run_id="AO-9-r2", pid_created=psutil.Process().create_time())
            elif change == "other-bus":
                current["bus"] = "other:7777"
            else:
                current["worker"] = "another-host"
            marker.write_text(json.dumps(current))

    def acquire_then_change(*args, **kwargs):
        ownership = lock_worktree(*args, **kwargs)
        change_marker()
        return ownership

    if timing == "before":
        change_marker()
    else:
        monkeypatch.setattr(resources, "lock_worktree", acquire_then_change)
    monkeypatch.setattr(adapter, "delete_sandbox", lambda name: mutations.append(("delete", name)))
    monkeypatch.setattr(openshell_services, "stop", lambda *args: mutations.append(("stop", args)))
    assert openshell_integrate.reconcile_attempt(state, owner, io.StringIO(), PREFIX) == 0
    assert mutations == [] and (state / "job.tgz").read_bytes() == b"old job"
    assert (state / adapter.SERVICES_FILE).exists()
    if change == "disappeared":
        assert not marker.exists()
    else:
        assert json.loads(marker.read_text()) != owner
    monkeypatch.setattr(resources, "lock_worktree", lock_worktree)
    _assert_lock_available(state)


def test_reconcile_dead_owner_releases_lock_and_allows_successor(monkeypatch, tmp_path):
    state, owner = _dead_attempt_state(tmp_path)
    shell = FakeShell()
    shell.live.add(state.name)
    monkeypatch.setattr(adapter, "openshell", shell)
    stops = []
    monkeypatch.setattr(openshell_services, "stop", lambda *args: stops.append(args[0]))
    assert openshell_integrate.reconcile_attempt(state, owner, io.StringIO(), PREFIX) == 1
    assert stops == [f"{PREFIX}-integrate-AO-9"] and state.name not in shell.live
    assert not (state / adapter.OWNER_FILE).exists() and not (state / "job.tgz").exists()
    assert not (state / adapter.SERVICES_FILE).exists()
    _assert_lock_available(state)
    attempt, repo, merge_sha = _attempt(monkeypatch, tmp_path, shell)
    try:
        assert attempt.check(repo, merge_sha, ["true"])[0] == "passed"
        before, calls = _state_files(state), list(shell.calls)
        assert openshell_integrate.reconcile_attempt(state, owner, io.StringIO(), PREFIX) == 0
        assert _state_files(state) == before and shell.calls == calls
    finally:
        attempt.cleanup(io.StringIO())


@pytest.mark.parametrize("exception", [adapter.AdapterError("delete failed"), KeyboardInterrupt("reconcile cancelled")])
def test_reconcile_failure_or_cancellation_retains_marker_and_releases_descriptor(monkeypatch, tmp_path, exception):
    state, owner = _dead_attempt_state(tmp_path)
    before = _state_files(state)
    stops = []

    def fail(*args, **kwargs):
        raise exception

    monkeypatch.setattr(adapter, "delete_sandbox", fail)
    monkeypatch.setattr(openshell_services, "stop", lambda *args: stops.append(args))
    if isinstance(exception, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt, match="reconcile cancelled"):
            openshell_integrate.reconcile_attempt(state, owner, io.StringIO(), PREFIX)
    else:
        err = io.StringIO()
        assert openshell_integrate.reconcile_attempt(state, owner, err, PREFIX) == 0
        assert "not confirmed deleted: delete failed" in err.getvalue()
    assert _state_files(state) == before and stops == []
    _assert_lock_available(state)


# -- integrate_task on the OpenShell backend, end to end against the bus app ------------------------

@pytest.fixture
def openshell_env(tmp_path, repos_root, worktree_root) -> dict[str, str]:
    env = {**os.environ, **BASE_ENV}
    env.update(AOS_REPO_ROOT=str(repos_root), AOS_WORKTREE_ROOT=str(worktree_root), AOS_HOST="mac",
               AOS_ACCEPT_TIMEOUT_SECONDS="10", AOS_OPENSHELL_STATE_DIR=str(tmp_path / "state"))
    return env


def _shell(monkeypatch, shell=None, **kw) -> FakeShell:
    shell = shell or FakeShell(**kw)
    monkeypatch.setattr(adapter, "openshell", shell)
    monkeypatch.setattr(adapter, "pause", lambda seconds: None)
    ticks = iter(range(0, 10**6, 50))  # the polling loops never wait and never spin on the real clock
    monkeypatch.setattr(adapter, "clock", lambda: next(ticks))
    return shell


def _v2_policy(source: Path, text: str = POLICY) -> None:
    (source / "aos-policy.toml").write_text(text)
    _git("add", "aos-policy.toml", cwd=source)
    _git("commit", "-q", "-m", "policy", cwd=source)


def _review(store, source: Path, name: str, acceptance: list[str] | None = None) -> dict:
    branch = _make_branch(source, name, f"{name}.txt", "x\n")
    sha = _git("rev-parse", branch, cwd=source).stdout.strip()
    return _task_to_review(store, repo="repo-a", head_sha=sha, acceptance=["true"] if acceptance is None else acceptance)


def _integrate(client, env, task_id: str, *, keep: bool = False, backend: str = "openshell"):
    out, err = io.StringIO(), io.StringIO()
    code = integrate.integrate_task(client, task_id=task_id, keep=keep, environ=env, out=out, err=err, backend=backend)
    return code, out.getvalue(), err.getvalue()


def _row(store, made):
    return store.get_run(made["run_id"])["integration"]


def _name(made) -> str:
    return f"{PREFIX}-{made['task_id'].lower()}-int"


def test_clean_merge_passes_in_a_fresh_sandbox_and_leaves_nothing(store, client, source_repo, worktree_root,
                                                                   tmp_path, monkeypatch, openshell_env):
    _v2_policy(source_repo)
    made = _review(store, source_repo, "task-os", ["pytest -q"])
    (source_repo / "other.txt").write_text("main moved\n")  # the base moves after the branch: the tree must be the merge
    _git("add", "other.txt", cwd=source_repo)
    _git("commit", "-q", "-m", "main moved", cwd=source_repo)
    (source_repo / "aos-workflow.toml").write_text('test_command = "test -f other.txt"\n')
    _git("add", "aos-workflow.toml", cwd=source_repo)
    _git("commit", "-q", "-m", "workflow checks", cwd=source_repo)
    shell = _shell(monkeypatch, accept_tail=b"8 passed")
    name = _name(made)
    code, out, err = _integrate(client, openshell_env, made["task_id"])
    assert code == 0, (out, err)
    row = _row(store, made)
    assert row["status"] == "passed" and row["base_branch"] == "main"
    assert row["output"].count("[exit 0] (fresh sandbox " + name + ")") == 2 and "$ test -f other.txt" in row["output"]
    assert f"integration passed {made['task_id']}" in out and "integration cleanup:" in out and f"sandbox {name} deleted" in out
    job = tmp_path / "seen.tgz"
    job.write_bytes(shell.uploads["/sandbox/incoming/job.tgz"])
    with tarfile.open(job) as tar:
        names = set(tar.getnames())
    assert {"README.md", "task-os.txt", "other.txt", ".git/HEAD"} <= names
    assert tar_member_text(job, ".git/HEAD").strip() == f"ref: refs/heads/integrate/{made['task_id']}"
    assert name not in shell.live
    assert not (tmp_path / "state" / name / "owner.json").exists()
    assert list((worktree_root / "repo-a").iterdir()) == []
    assert f"integrate/{made['task_id']}" not in _git("branch", "--list", cwd=source_repo).stdout  # no ref in the source


def test_missing_run_snapshot_is_refused_before_sandbox(store, client, source_repo, monkeypatch, openshell_env):
    _v2_policy(source_repo)
    made = _review(store, source_repo, "task-empty")
    with store.connection() as db:
        db.execute("UPDATE task_runs SET effective_acceptance=NULL WHERE id=?", (made["run_id"],))
    shell = _shell(monkeypatch)
    code, out, err = _integrate(client, openshell_env, made["task_id"])
    assert code == 1, (out, err)
    assert _row(store, made)["status"] == "error"
    assert shell.calls == []


def test_environ_may_be_the_process_environment(store, client, source_repo, tmp_path, monkeypatch, openshell_env):
    _v2_policy(source_repo)
    made = _review(store, source_repo, "task-environ", ["true"])
    _shell(monkeypatch)
    for key, value in openshell_env.items():
        monkeypatch.setenv(key, value)
    code, out, err = _integrate(client, os.environ, made["task_id"])
    assert code == 0, (out, err)
    assert _row(store, made)["status"] == "passed"


@pytest.mark.parametrize("policy_text, phrase", [
    (None, "aos-policy.toml is missing from the default branch"),
    (POLICY_TOML, "policy: "),
])
def test_without_a_v2_policy_the_check_is_error_before_any_sandbox(store, client, source_repo, worktree_root,
                                                                   monkeypatch, openshell_env, policy_text, phrase):
    if policy_text is not None:
        _v2_policy(source_repo, policy_text)
    made = _review(store, source_repo, "task-nopolicy")
    shell = _shell(monkeypatch)
    code, out, err = _integrate(client, openshell_env, made["task_id"])
    assert code == 1
    row = _row(store, made)
    assert row["status"] == "error" and phrase in row["output"] and row["base_branch"] == "main"
    assert shell.calls == [] and not (worktree_root / "repo-a").exists()


def test_gateway_down_is_error_before_any_worktree(store, client, source_repo, worktree_root, monkeypatch, openshell_env):
    _v2_policy(source_repo)
    made = _review(store, source_repo, "task-gw")

    class Down(FakeShell):
        def handle(self, args):
            if args[0] == "status":
                return 1, ""
            return super().handle(args)

    shell = _shell(monkeypatch, Down())
    code, out, err = _integrate(client, openshell_env, made["task_id"])
    assert code == 1
    row = _row(store, made)
    assert row["status"] == "error" and row["output"] == integrate.GATEWAY_UNREACHABLE
    assert shell.calls == [("status",)] and not (worktree_root / "repo-a").exists()

    def missing(*args, **kwargs):
        raise FileNotFoundError("openshell")

    monkeypatch.setattr(adapter, "openshell", missing)
    made = _review(store, source_repo, "task-gw2")
    code, out, err = _integrate(client, openshell_env, made["task_id"])
    assert code == 1 and _row(store, made)["output"] == integrate.GATEWAY_UNREACHABLE


def test_adapter_settings_refusal_is_error_with_nothing_created(store, client, source_repo, worktree_root,
                                                               monkeypatch, openshell_env):
    _v2_policy(source_repo)
    made = _review(store, source_repo, "task-settings")
    shell = _shell(monkeypatch)
    env = dict(openshell_env)
    del env["AOS_OPENSHELL_IMAGE"]
    code, out, err = _integrate(client, env, made["task_id"])
    assert code == 1
    row = _row(store, made)
    assert row["status"] == "error" and "AOS_OPENSHELL_IMAGE is required" in row["output"] and row["base_branch"] == ""
    assert out.startswith(f"integration error {made['task_id']} ")
    assert shell.calls == [] and not (worktree_root / "repo-a").exists()


def test_conflict_is_posted_before_any_sandbox_or_database(store, client, source_repo, docker_stub, monkeypatch, openshell_env):
    _v2_policy(source_repo)
    _write_services(source_repo)
    _make_branch(source_repo, "task-x", "README.md", "hello from x\n")
    branch_y = _make_branch(source_repo, "task-y", "README.md", "hello from y\n")
    sha_y = _git("rev-parse", branch_y, cwd=source_repo).stdout.strip()
    _git("merge", "--no-ff", "-q", "-m", "merge task-x", "task-x", cwd=source_repo)
    made = _task_to_review(store, repo="repo-a", head_sha=sha_y)
    shell = _shell(monkeypatch)
    code, out, err = _integrate(client, openshell_env, made["task_id"])
    assert code == 1
    assert _row(store, made)["status"] == "conflict" and "README.md" in _row(store, made)["output"]
    assert ("create", _name(made)) not in shell.events
    assert [argv for argv in docker_stub.calls() if argv[:1] == ["run"]] == []  # no database was started


def test_a_dead_earlier_attempt_of_the_same_task_is_swept_first(store, client, source_repo, tmp_path, monkeypatch, openshell_env):
    _v2_policy(source_repo)
    made = _review(store, source_repo, "task-again", ["true"])
    name = _name(made)
    shell = _shell(monkeypatch)
    shell.live.add(name)
    gone = subprocess.Popen(["true"])
    gone.wait()
    stale = tmp_path / "state" / name
    stale.mkdir(parents=True)
    (stale / "owner.json").write_text(json.dumps({
        "run_id": made["run_id"], "task_id": made["task_id"], "sandboxes": [name], "host": "mac",
        "worker": adapter.socket.gethostname(), "kind": "integrate", "attempt": f"{PREFIX}-integrate-{made['task_id']}",
        "pid": gone.pid, "pid_created": 0.0, "bus": BUS_IDENTITY, "prefix": PREFIX}))
    code, out, err = _integrate(client, openshell_env, made["task_id"])
    assert code == 0, (out, err)
    assert _row(store, made)["status"] == "passed"
    deletes = [i for i, e in enumerate(shell.events) if e == ("delete", name)]
    create = shell.events.index(("create", name))
    assert len(deletes) == 2 and deletes[0] < create < deletes[1]
    assert f"deleted the sandbox of {PREFIX}-integrate-{made['task_id']}" in err
    assert not (stale / "owner.json").exists()


def test_keep_retains_the_scratch_worktree_only(store, client, source_repo, docker_stub, worktree_root, monkeypatch, openshell_env):
    _v2_policy(source_repo)
    _write_services(source_repo)
    made = _review(store, source_repo, "task-keep", ["true"])
    shell = _shell(monkeypatch)
    code, out, err = _integrate(client, openshell_env, made["task_id"], keep=True)
    assert code == 0, (out, err)
    scratch, = (worktree_root / "repo-a").iterdir()
    assert (scratch / "task-keep.txt").exists()
    assert _name(made) not in shell.live and docker_stub.containers() == {}
    password = docker_stub.password()
    assert password and password not in out and password not in err and password not in _row(store, made)["output"]


@pytest.mark.parametrize("post_fails", [False, True])
def test_ownership_lock_covers_result_post_and_cleanup(
        store, client, source_repo, tmp_path, monkeypatch, openshell_env, post_fails):
    _v2_policy(source_repo)
    made = _review(store, source_repo, "task-ownership", ["true"])
    shell = _shell(monkeypatch)
    state = tmp_path / "state" / _name(made)
    post, delete_sandbox = client.post, adapter.delete_sandbox
    seen = []

    def assert_held():
        with pytest.raises(BlockingIOError):
            with resources.lock_worktree(str(state), str(state.parent / ".integrate-locks")):
                pass

    def post_with_lock(path, **kwargs):
        if path == f"/tasks/{made['task_id']}/integration":
            assert_held()
            seen.append("post")
            if post_fails:
                raise RuntimeError("posting failed")
        return post(path, **kwargs)

    def delete_with_lock(name):
        assert_held()
        seen.append("cleanup")
        return delete_sandbox(name)

    monkeypatch.setattr(client, "post", post_with_lock)
    monkeypatch.setattr(adapter, "delete_sandbox", delete_with_lock)
    if post_fails:
        with pytest.raises(RuntimeError, match="posting failed"):
            _integrate(client, openshell_env, made["task_id"])
        assert _row(store, made) is None
    else:
        code, out, err = _integrate(client, openshell_env, made["task_id"])
        assert code == 0, (out, err)
        assert _row(store, made)["status"] == "passed"
    assert seen == ["post", "cleanup"] and _name(made) not in shell.live
    assert not (state / adapter.OWNER_FILE).exists()
    _assert_lock_available(state)


def test_cleanup_failure_keeps_the_verdict_posted_first(store, client, source_repo, tmp_path, monkeypatch, openshell_env):
    _v2_policy(source_repo)
    made = _review(store, source_repo, "task-sticky", ["true"])
    seen = {}

    class Sticky(FakeShell):
        def handle(self, args):
            if args[:2] == ("sandbox", "delete"):
                seen.setdefault("posted_before_delete", _row(store, made) is not None)
                if "termed" not in seen:  # a TERM between the verdict and the end of cleanup is ignored
                    seen["termed"] = True
                    os.kill(os.getpid(), signal.SIGTERM)
            return super().handle(args)

    _shell(monkeypatch, Sticky(delete_sticks=True))
    code, out, err = _integrate(client, openshell_env, made["task_id"])
    assert code == 0, (out, err)
    assert _row(store, made)["status"] == "passed" and seen["posted_before_delete"] is True and seen["termed"] is True
    assert openshell_integrate.CLEANUP_FAILED in err and "cleanup failed" in out
    assert (tmp_path / "state" / _name(made) / "owner.json").exists()


def test_sigterm_during_the_check_cleans_up_posts_nothing_and_restores_the_handler(
        store, client, source_repo, docker_stub, monkeypatch, openshell_env):
    _v2_policy(source_repo)
    _write_services(source_repo)
    made = _review(store, source_repo, "task-term", ["true"])
    shell = _shell(monkeypatch)
    previous = signal.getsignal(signal.SIGTERM)

    def terminate_then_wait(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)
        raise AssertionError("the SIGTERM did not interrupt the check")

    monkeypatch.setattr(adapter, "accept_fresh", terminate_then_wait)
    with pytest.raises(KeyboardInterrupt, match="integrate received SIGTERM"):
        _integrate(client, openshell_env, made["task_id"])
    assert _row(store, made) is None
    assert ("delete", _name(made)) in shell.events and docker_stub.containers() == {}
    assert signal.getsignal(signal.SIGTERM) == previous


