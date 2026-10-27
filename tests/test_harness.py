"""agent_os.harness: the harness-process and acceptance helpers the OpenShell runner, `aos integrate`
and the live sessions share (moved out of the host runner on 2026-10-13)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from agent_os import harness, policy


def test_env_from_defaults_and_path_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    env = harness.env_from({"AOS_RESOURCE_ROOT": "resources"})
    assert env.resource_root == str(tmp_path / "resources")  # absolute, for harnesses with another cwd
    assert env.repo_root == os.path.expanduser("~/code")
    assert env.worktree_root == os.path.expanduser("~/.aos/worktrees")
    assert env.claude_bin == "claude"
    configured = harness.env_from({"AOS_REPO_ROOT": "~/repos", "AOS_WORKTREE_ROOT": str(tmp_path / "worktrees"),
                                  "AOS_CLAUDE_BIN": "~/bin/claude"})
    assert configured.repo_root == os.path.expanduser("~/repos")
    assert configured.worktree_root == str(tmp_path / "worktrees")
    assert configured.resource_root == str(tmp_path / "resources")
    assert configured.claude_bin == os.path.expanduser("~/bin/claude")


def test_parse_harness_output_claude_and_codex_formats() -> None:
    assert harness.parse_harness_output(json.dumps({"result": "done", "session_id": "s1"}).encode(), "claude") == ("done", "s1")
    assert harness.parse_harness_output(b"[]", "claude") == (None, None)
    codex = b"\n".join(json.dumps(e).encode() for e in [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "final"}},
        {"type": "turn.completed"}])
    assert harness.parse_harness_output(codex, "codex") == ("final", "t1")
    incomplete = b"\n".join(json.dumps(e).encode() for e in [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}])
    assert harness.parse_harness_output(incomplete, "codex") == (None, "t1")


def test_parse_harness_output_claude_single_object_is_unchanged() -> None:
    obj = {"type": "result", "result": "done", "session_id": "s"}
    assert harness.parse_harness_output(json.dumps(obj).encode(), "claude") == ("done", "s")
    assert harness.parse_harness_output(json.dumps(obj, indent=2).encode(), "claude") == ("done", "s")
    assert harness.parse_harness_output(b'{"result":"typeless","session_id":"s2"}', "claude") == ("typeless", "s2")
    assert harness.parse_harness_output(b"[]", "claude") == (None, None)


def test_parse_harness_output_claude_stream_takes_the_last_result_line() -> None:
    records = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "working"}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tool-1", "content": "ok", "is_error": False}]}},
        {"type": "result", "result": "done", "session_id": "s"},
    ]
    stream = b"".join(json.dumps(record).encode() + b"\n" for record in records)
    assert harness.parse_harness_output(stream, "claude") == ("done", "s")
    assert harness.parse_harness_output(
        stream + b'{"type":"result","result":"last","session_id":"s2"}\n', "claude") == ("last", "s2")
    assert harness.parse_harness_output(stream + b"{}\n", "claude") == ("done", "s")
    assert harness.parse_harness_output(stream + b'{"type":"assistant"', "claude") == ("done", "s")
    assert harness.parse_harness_output(b"not json\n", "claude") == (None, None)
    without_result = b"".join(json.dumps(record).encode() + b"\n" for record in records[:-1])
    assert harness.parse_harness_output(without_result, "claude") == (None, None)


@pytest.mark.parametrize("position", ["first", "last", "alone", "without-result"])
def test_parse_harness_output_claude_nested_record_is_unparseable_not_an_exception(position) -> None:
    nested = ('{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use",'
              '"id":"tool-1","name":"Bash","input":{"v":' + "[" * 20000 + "]" * 20000 + '}}]}}\n').encode()
    init = b'{"type":"system","subtype":"init"}\n'
    result = b'{"type":"result","result":"done","session_id":"s"}\n'
    raw, expected = {
        "first": (nested + init + result, ("done", "s")),
        "last": (init + result + nested, ("done", "s")),
        "alone": (nested, (None, None)),
        "without-result": (nested + init, (None, None)),
    }[position]
    assert harness.parse_harness_output(raw, "claude") == expected


class _FakeProc:
    """A Popen stand-in that records waits. Every test using it mocks os.killpg: the 2026-10 incident
    (a real os.killpg on pid 1 from this file) took the host down."""

    def __init__(self, pid: int, *, alive: bool) -> None:
        self.pid = pid
        self.alive = alive
        self.waits: list[float] = []

    def poll(self) -> int | None:
        return None if self.alive else 0

    def wait(self, timeout: float) -> int:
        self.waits.append(timeout)
        raise subprocess.TimeoutExpired("fake", timeout)


def _record_killpg(monkeypatch: pytest.MonkeyPatch, error: OSError | None = None) -> list[tuple[int, int]]:
    sent: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        sent.append((pgid, sig))
        if error is not None:
            raise error

    monkeypatch.setattr(harness.os, "killpg", fake_killpg)
    return sent


SAFE_PGID = 424242  # never signalled for real: os.killpg is mocked wherever it appears


def test_kill_group_turns_oserror_into_a_description(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _record_killpg(monkeypatch, PermissionError(1, "Operation not permitted"))
    proc = _FakeProc(SAFE_PGID, alive=False)
    description = harness.kill_group(proc)  # type: ignore[arg-type]
    assert description is not None and "\n" not in description
    assert description.startswith(f"process group {SAFE_PGID} cleanup failed: ")
    assert "PermissionError" in description
    assert sent == [(SAFE_PGID, signal.SIGTERM), (SAFE_PGID, signal.SIGKILL)]
    # ESRCH (the group is already gone) is silent.
    sent = _record_killpg(monkeypatch, ProcessLookupError(3, "No such process"))
    assert harness.kill_group(_FakeProc(SAFE_PGID, alive=False)) is None  # type: ignore[arg-type]
    assert sent == [(SAFE_PGID, signal.SIGTERM), (SAFE_PGID, signal.SIGKILL)]


def test_kill_group_terms_waits_then_kills_a_live_child_group(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _record_killpg(monkeypatch)
    monkeypatch.setattr(harness.os, "getpgid", lambda pid: SAFE_PGID)
    proc = _FakeProc(SAFE_PGID + 1, alive=True)
    assert harness.kill_group(proc) is None  # type: ignore[arg-type]
    assert sent == [(SAFE_PGID, signal.SIGTERM), (SAFE_PGID, signal.SIGKILL)]
    assert proc.waits == [30]


@pytest.mark.parametrize("case", ["live-pgid-1", "live-pgid-0", "live-own-group", "exited-pid-1",
                                  "exited-own-group", "lookup-failure-pid-1", "lookup-failure-own-group"])
def test_kill_group_refuses_unsafe_groups_without_signal_or_wait(
        case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _record_killpg(monkeypatch)
    own = os.getpgrp()
    lookup_failure = case.startswith("lookup-failure")
    target = own if case.endswith("own-group") else (0 if case == "live-pgid-0" else 1)

    def fake_getpgid(pid: int) -> int:
        if lookup_failure:
            raise ProcessLookupError(3, "No such process")
        return target

    monkeypatch.setattr(harness.os, "getpgid", fake_getpgid)
    # A live process resolves its group through getpgid; an exited one or a failed lookup falls back to its pid.
    proc = _FakeProc(SAFE_PGID if case.startswith("live") else target, alive=not case.startswith("exited"))
    description = harness.kill_group(proc)  # type: ignore[arg-type]
    assert sent == [] and proc.waits == []
    assert description is not None and "\n" not in description
    assert description.startswith(f"process group {target} cleanup refused: ")
    assert ("own process group" if case.endswith("own-group") else "<= 1") in description


def test_kill_group_ends_a_real_child_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """Smoke against a real, separately spawned child group: the guards leave ordinary cleanup alone."""
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        pgid = os.getpgid(proc.pid)
        assert pgid == proc.pid and pgid > 1 and pgid != os.getpgrp()
        try:
            os.killpg(pgid, 0)  # signal 0 to the child's own new group: a permission probe, nothing is sent
        except PermissionError:
            pytest.skip("this sandbox refuses every process-group signal (EPERM on killpg), even to its own child")
        assert harness.kill_group(proc) is None
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    # A second call on the exited child: the mocked killpg answers ESRCH, which stays silent.
    sent = _record_killpg(monkeypatch, ProcessLookupError(3, "No such process"))
    assert harness.kill_group(proc) is None
    assert sent == [(proc.pid, signal.SIGTERM), (proc.pid, signal.SIGKILL)]


def test_acceptance_env_scrub_is_one_function() -> None:
    base = {"GIT_DIR": "/g", "GIT_INDEX_FILE": "/i", "GIT_WORK_TREE": "/w", "GIT_COMMON_DIR": "/c",
            "NODE_EXTRA_CA_CERTS": "/ca.pem", "NODE_OPTIONS": "--x", "PYTHONPATH": "/p", "PYTHONSTARTUP": "/s",
            "PYTHONHOME": "/h", "VIRTUAL_ENV": "/venv", "PATH": "/venv/bin:/usr/bin:/venv/bin/:/bin",
            "HOME": "/home/u", "AOS_RUN_ID": "AO-1-r1", "AOS_WORKTREE": "/wt", "COMPOSE_PROJECT_NAME": "aos-ao-1-r1",
            "AOS_RESOURCE_ROOT": "/r", "DATABASE_URL": "postgresql://x", "CODEX_HOME": "/host-codex"}
    env = harness.acceptance_env(base, {"CODEX_HOME": "/run/codex-home"})
    for key in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_COMMON_DIR", "NODE_EXTRA_CA_CERTS",
                "NODE_OPTIONS", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "VIRTUAL_ENV"):
        assert key not in env, key
    assert env["PATH"] == "/usr/bin:/bin"
    for key in ("HOME", "AOS_RUN_ID", "AOS_WORKTREE", "COMPOSE_PROJECT_NAME", "AOS_RESOURCE_ROOT", "DATABASE_URL"):
        assert env[key] == base[key], key
    assert env["CODEX_HOME"] == "/run/codex-home"
    assert base["PATH"].startswith("/venv/bin")  # the input is not mutated


def test_run_acceptance_results_keyword_carries_command_exit_note_and_output(tmp_path: Path) -> None:
    results: list[dict] = []
    passed, total, _text, _foreign = harness.run_acceptance(
        ["echo hi", "echo oops; exit 3", "echo never"], str(tmp_path), 5, threading.Event(), dict(os.environ),
        results=results)
    assert (passed, total) == (1, 3)
    assert [(r["command"], r["exit"], r["note"]) for r in results] == [("echo hi", 0, ""), ("echo oops; exit 3", 3, "")]
    assert results[0]["output"] == "hi\n" and results[1]["output"] == "oops\n"


def test_run_acceptance_results_keyword_records_a_timeout(tmp_path: Path) -> None:
    results: list[dict] = []
    harness.run_acceptance(["sleep 5"], str(tmp_path), 1, threading.Event(), dict(os.environ), results=results)
    assert len(results) == 1 and results[0]["command"] == "sleep 5"
    assert results[0]["exit"] is None and results[0]["note"] == "timeout after 1 s"


def test_run_acceptance_stops_when_the_run_ends_on_the_bus(tmp_path: Path) -> None:
    foreign = threading.Event()
    foreign.set()
    passed, total, text, foreign_seen = harness.run_acceptance(["true"], str(tmp_path), 5, foreign, dict(os.environ))
    assert (passed, total, foreign_seen) == (0, 1, True) and text == ""


def _heartbeat_client(responses: list[tuple[int, object]]) -> tuple[httpx.Client, list[dict]]:
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        if responses:
            status, body = responses.pop(0)
        else:  # keep the lease short so the thread keeps beating
            status, body = 200, {"status": "running",
                                 "lease_until": (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()}
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://bus"), posted


def test_heartbeat_extends_the_lease_and_stops_on_a_foreign_status() -> None:
    soon = datetime.now(timezone.utc) + timedelta(seconds=2)
    renewed = soon + timedelta(seconds=2)
    client, posted = _heartbeat_client([
        (200, {"status": "running", "lease_until": renewed.isoformat().replace("+00:00", "Z")}),
        (200, {"status": "cancelled"}),
    ])
    beat = harness.Heartbeat(client, threading.Lock(), "AO-1-r1", "claim-token", soon)
    beat.start()
    beat.join(timeout=15)
    assert not beat.is_alive()
    assert posted and posted[0] == {"claim_token": "claim-token"}
    assert beat.foreign.is_set() and beat.foreign_status == "cancelled"
    assert beat.lease_until == renewed  # the first heartbeat's lease_until was taken over


def test_heartbeat_marks_a_4xx_as_foreign_and_three_5xx_as_unreachable() -> None:
    client, _ = _heartbeat_client([(409, {"detail": "not yours"})])
    beat = harness.Heartbeat(client, threading.Lock(), "AO-1-r1", "t", datetime.now(timezone.utc) + timedelta(seconds=2))
    beat.start()
    beat.join(timeout=10)
    assert beat.foreign_status == "http 409"
    client, _ = _heartbeat_client([(500, {}), (500, {}), (500, {})])
    beat = harness.Heartbeat(client, threading.Lock(), "AO-1-r1", "t", datetime.now(timezone.utc) + timedelta(seconds=2))
    beat.start()
    beat.join(timeout=15)
    assert beat.foreign_status == "unreachable"


def test_heartbeat_recovers_after_invalid_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"{"
    soon = datetime.now(timezone.utc) + timedelta(seconds=2)
    renewed = soon + timedelta(seconds=2)
    client, posted = _heartbeat_client([
        (200, body),
        (200, body),
        (200, {"status": "running", "lease_until": renewed.isoformat()}),
        (200, body),
        (200, body),
        (200, {"status": "cancelled"}),
    ])
    beat = harness.Heartbeat(client, threading.Lock(), "AO-1-r1", "t", soon)
    monkeypatch.setattr(beat.stop_event, "wait", lambda timeout: False)
    with client:
        beat.start()
        beat.join(timeout=5)
    assert not beat.is_alive()
    assert len(posted) == 6
    assert beat.lease_until == renewed
    assert beat.foreign.is_set() and beat.foreign_status == "cancelled"


@pytest.mark.parametrize("body", [
    b"{",
    [],
    {"status": []},
    {"status": "running"},
    {"status": "running", "lease_until": "not-a-timestamp"},
    {"status": "running", "lease_until": "2026-10-22T00:00:00"},
])
def test_heartbeat_fails_closed_after_persistent_invalid_responses(
    body: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    soon = datetime.now(timezone.utc) + timedelta(seconds=2)
    client, posted = _heartbeat_client([(200, body)] * 3 + [(200, {"status": "cancelled"})])
    beat = harness.Heartbeat(client, threading.Lock(), "AO-1-r1", "t", soon)
    monkeypatch.setattr(beat.stop_event, "wait", lambda timeout: False)
    with client:
        beat.start()
        beat.join(timeout=5)
    assert not beat.is_alive()
    assert len(posted) == 3
    assert beat.lease_until == soon
    assert beat.foreign.is_set() and beat.foreign_status == "unreachable"


def test_verify_skill_pointer_needs_a_committed_file_not_a_working_tree_copy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    (repo / "README.md").write_text("x\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    skill = repo / harness.VERIFY_SKILL_PATH
    skill.parent.mkdir(parents=True)
    skill.write_text("uncommitted\n")
    assert harness.verify_skill_on_default(str(repo), policy.resolve_default(str(repo))) is False
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "skill"], check=True)
    assert harness.verify_skill_on_default(str(repo), policy.resolve_default(str(repo))) is True
    assert harness.VERIFY_SKILL_PATH in harness.VERIFY_SKILL_LINE
