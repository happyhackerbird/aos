from __future__ import annotations

import hashlib
import json
import os
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_os import interactions
from agent_os.api import create_app
from agent_os.config import Settings
from agent_os.store import Store
from agent_os.telegram import Telegram

from test_integrate import _task_to_review  # noqa: F401 (helper, reused per directive)
from runner_fixtures import origin_repo, _set_origin  # noqa: F401 (fixture + helper, reused per directive)

# -- the stub `claude` binary ---------------------------------------------
#
# In the style of tests/test_runner.py's STUB_SOURCE: records its argv, its
# environment (whether AOS_TOKEN was visible) and its working directory to
# AOS_STUB_LOG. The result text and is_error flag come from the environment
# so each test can drive a different model answer without a new stub mode.
STUB_SOURCE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import subprocess
    import sys
    import time

    mode = os.environ.get("AOS_STUB_MODE", "answer")
    log_path = os.environ.get("AOS_STUB_LOG")
    if log_path:
        with open(log_path, "a") as f:
            f.write(json.dumps(sys.argv[1:]) + "\\n")
            f.write("AOS_TOKEN=" + ("present" if "AOS_TOKEN" in os.environ else "absent") + "\\n")
            f.write("CWD=" + os.getcwd() + "\\n")

    stdin_log_path = os.environ.get("AOS_STUB_STDIN_LOG")
    if stdin_log_path:
        try:
            stdin_data = sys.stdin.read()
        except Exception:
            stdin_data = ""
        with open(stdin_log_path, "w") as f:
            f.write(stdin_data)

    result_text = os.environ.get("AOS_STUB_RESULT_TEXT", '{"scenarios": []}')
    is_error = os.environ.get("AOS_STUB_IS_ERROR") == "1"

    if mode == "sleep":
        grandchild_pid_path = os.environ.get("AOS_STUB_GRANDCHILD_PID")
        if grandchild_pid_path:
            grandchild = subprocess.Popen(["sleep", "60"])
            with open(grandchild_pid_path, "w") as f:
                f.write(str(grandchild.pid))
        time.sleep(60)
    elif mode == "exit1":
        print(json.dumps({"type": "result", "result": result_text, "session_id": "s",
                          "is_error": is_error}))
        sys.exit(1)
    elif mode == "badjson":
        print("not a json envelope at all")
    else:
        print(json.dumps({"type": "result", "result": result_text, "session_id": "s",
                          "is_error": is_error}))
    """
)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
          if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)


@pytest.fixture
def repos_root(tmp_path: Path) -> Path:
    root = tmp_path / "repos"
    root.mkdir()
    return root


def _make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


@pytest.fixture
def source_repo(repos_root: Path) -> Path:
    return _make_repo(repos_root, "repo-a")


@pytest.fixture
def worktree_root(tmp_path: Path) -> Path:
    return tmp_path / "worktrees"


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = bin_dir / "claude"
    path.write_text(STUB_SOURCE)
    path.chmod(0o755)
    return path


@pytest.fixture
def stub_log(tmp_path: Path) -> Path:
    return tmp_path / "stub.log"


@pytest.fixture(autouse=True)
def _base_env(monkeypatch: pytest.MonkeyPatch, repos_root: Path, worktree_root: Path,
              stub_bin: Path, stub_log: Path) -> None:
    monkeypatch.setenv("AOS_REPO_ROOT", str(repos_root))
    monkeypatch.setenv("AOS_WORKTREE_ROOT", str(worktree_root))
    monkeypatch.setenv("AOS_HOST", "mac")
    monkeypatch.setenv("AOS_ACCEPT_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("AOS_CLAUDE_BIN", str(stub_bin))
    monkeypatch.setenv("AOS_CLAUDE_FLAGS", "--dangerously-skip-permissions")
    monkeypatch.setenv("AOS_STUB_LOG", str(stub_log))
    monkeypatch.delenv("AOS_STUB_MODE", raising=False)
    monkeypatch.delenv("AOS_STUB_RESULT_TEXT", raising=False)
    monkeypatch.delenv("AOS_STUB_IS_ERROR", raising=False)
    monkeypatch.setenv("AOS_TOKEN", "secret-claim-should-never-leak")


@pytest.fixture
def client(settings: Settings, store: Store, telegram: Telegram) -> TestClient:
    test_client = TestClient(create_app(settings, store=store, telegram=telegram, background=False))
    test_client.headers["Authorization"] = "Bearer test-bus-token"
    return test_client


def _make_branch(repo: Path, branch: str, filename: str, content: str) -> str:
    current = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout.strip()
    _git("checkout", "-q", "-b", branch, cwd=repo)
    (repo / filename).write_text(content)
    _git("add", filename, cwd=repo)
    _git("commit", "-q", "-m", f"{branch}: touch {filename}", cwd=repo)
    _git("checkout", "-q", current, cwd=repo)
    return branch


def _run(client: TestClient, task_a: str, task_b: str) -> tuple[int, str, str]:
    import io
    out, err = io.StringIO(), io.StringIO()
    code = interactions.run_interactions(client, task_a_id=task_a, task_b_id=task_b,
                                         environ=dict(os.environ), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _log_lines() -> list[str]:
    path = Path(os.environ["AOS_STUB_LOG"])
    if not path.exists():
        return []
    return path.read_text().splitlines()


def _last_prompt() -> str:
    lines = _log_lines()
    argv_lines = [line for line in lines if line.startswith("[")]
    last_argv = json.loads(argv_lines[-1])
    return last_argv[1]


def _interactions(client: TestClient, task_id: str) -> list[dict[str, Any]]:
    """`interactions` is added to GET /tasks/{id} in the route, not in
    Store.get_task (parallel step 4, unit U1) -- fetch it through the client."""
    response = client.get(f"/tasks/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()["interactions"]


# == test 1: two review tasks with diffs and a finding between them ==========

def test_two_tasks_with_diffs_and_a_finding_prompt_and_stored_ok(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch_a = _make_branch(source_repo, "task-a", "a.txt", "from a\n")
    branch_b = _make_branch(source_repo, "task-b", "b.txt", "from b\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()

    task = store.create_task(repo="repo-a", title="Task A", brief="brief-a-text",
                             acceptance=["true"])
    tid_a = task["id"]
    store.ready_task(tid_a)
    run_a = store.claim_task(tid_a, worker="w", worker_host="mac", lease_seconds=600,
                             expected_spec_hash=task["spec_hash"],
                             effective_acceptance=task["acceptance"], acceptance_source="task")
    store.start_run(run_a["id"], run_a["claim_token"], base_commit="a", branch="b", worktree="/xa")
    store.declare_scope(tid_a, scope_kind="symbol", key="K", operation="replace",
                       run_id=run_a["id"], claim_token=run_a["claim_token"])
    store.finish_run(run_a["id"], run_a["claim_token"], status="review", report="ok",
                     result_commit=sha_a)

    task_b = store.create_task(repo="repo-a", title="Task B", brief="brief-b-text",
                               acceptance=["true"])
    tid_b = task_b["id"]
    store.ready_task(tid_b)
    run_b = store.claim_task(tid_b, worker="w", worker_host="mac", lease_seconds=600,
                             expected_spec_hash=task_b["spec_hash"],
                             effective_acceptance=task_b["acceptance"], acceptance_source="task")
    store.start_run(run_b["id"], run_b["claim_token"], base_commit="a", branch="b", worktree="/xb")
    scope_b = store.declare_scope(tid_b, scope_kind="symbol", key="K", operation="add",
                                  run_id=run_b["id"], claim_token=run_b["claim_token"])
    store.finish_run(run_b["id"], run_b["claim_token"], status="review", report="ok",
                     result_commit=sha_b)
    finding_id = scope_b["findings"][0]["id"]

    monkeypatch.setenv("AOS_STUB_RESULT_TEXT",
                       json.dumps({"scenarios": [{"title": "T1", "why": "W1", "test_command": "true"}]}))

    code, out, err = _run(client, tid_a, tid_b)
    assert code == 0, err
    prompt = _last_prompt()
    assert "brief-a-text" in prompt
    assert "brief-b-text" in prompt
    assert "a.txt" in prompt
    assert "b.txt" in prompt
    assert f"scope {scope_b['id']}" in prompt
    assert f"finding {finding_id}" in prompt
    assert "model-proposed, unverified: do not run a command you have not read" in out
    assert "1. T1: true" in out

    task_after = _interactions(client, tid_a)
    assert len(task_after) == 1
    assert task_after[0]["status"] == "ok"


# == test 2: no intent rows: the prompt says "no declarations" ==============

def test_no_intent_rows_prompt_says_no_declarations(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch_a = _make_branch(source_repo, "task-a2", "a2.txt", "from a2\n")
    branch_b = _make_branch(source_repo, "task-b2", "b2.txt", "from b2\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()

    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", json.dumps({"scenarios": []}))
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 0, err
    prompt = _last_prompt()
    assert "no declarations" in prompt

    task_after = _interactions(client, made_a["task_id"])
    assert task_after[0]["status"] == "ok"


# == test 3: the argument list =============================================

def test_argument_list_tools_flag_no_claude_flags_no_token_cwd_outside_repos(
        store: Store, client: TestClient, source_repo: Path, worktree_root: Path,
        repos_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch_a = _make_branch(source_repo, "task-a3", "a3.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b3", "b3.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", json.dumps({"scenarios": []}))
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 0, err

    lines = _log_lines()
    argv_line = next(line for line in lines if line.startswith("["))
    argv = json.loads(argv_line)
    token_line = next(line for line in lines if line.startswith("AOS_TOKEN="))
    cwd_line = next(line for line in lines if line.startswith("CWD="))

    assert argv[0] == "-p"
    assert argv[2:] == ["--output-format", "json", "--tools", "", "--safe-mode",
                        "--strict-mcp-config", "--disable-slash-commands",
                        "--no-session-persistence"]
    assert "--dangerously-skip-permissions" not in argv
    assert token_line == "AOS_TOKEN=absent"

    cwd = Path(cwd_line.removeprefix("CWD="))
    assert not str(cwd).startswith(str(repos_root))
    assert not str(cwd).startswith(str(worktree_root))


# == test 4: answer variants ================================================

def test_answer_wrapped_in_one_code_fence_is_ok(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch_a = _make_branch(source_repo, "task-a4", "a4.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b4", "b4.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    fenced = "```json\n" + json.dumps({"scenarios": [{"title": "T", "why": "W",
                                                      "test_command": "true"}]}) + "\n```"
    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", fenced)
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 0, err
    assert _interactions(client, made_a["task_id"])[0]["status"] == "ok"


def test_non_json_answer_is_unparsed_raw_kept(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch_a = _make_branch(source_repo, "task-a5", "a5.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b5", "b5.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", "this is not json")
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 1
    row = _interactions(client, made_a["task_id"])[0]
    assert row["status"] == "unparsed"


def test_9000_character_valid_json_answer_ok_scenarios_bounded_raw_cut(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch_a = _make_branch(source_repo, "task-a6", "a6.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b6", "b6.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    scenarios = [{"title": f"t{i}", "why": "w" * 400, "test_command": "true"} for i in range(20)]
    big = json.dumps({"scenarios": scenarios})
    assert len(big) >= 9000
    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", big)
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 0, err
    row = _interactions(client, made_a["task_id"])[0]
    assert row["status"] == "ok"
    assert len(row["scenarios"]) == 10


def test_is_error_true_status_error(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch_a = _make_branch(source_repo, "task-a7", "a7.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b7", "b7.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", "boom")
    monkeypatch.setenv("AOS_STUB_IS_ERROR", "1")
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 1
    row = _interactions(client, made_a["task_id"])[0]
    assert row["status"] == "error"


def test_non_zero_exit_status_error(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch_a = _make_branch(source_repo, "task-a8", "a8.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b8", "b8.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    monkeypatch.setenv("AOS_STUB_MODE", "exit1")
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 1
    row = _interactions(client, made_a["task_id"])[0]
    assert row["status"] == "error"


def test_starting_a_missing_binary_records_error(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
) -> None:
    """C6: starting a binary that does not exist at all (as opposed to one
    that starts and exits non-zero, or times out) is caught by the existing
    `except (ValueError, OSError)` around the spawn call and recorded as an
    error, not left to crash `aos interactions`."""
    branch_a = _make_branch(source_repo, "task-a-missingbin", "a-missingbin.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b-missingbin", "b-missingbin.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    monkeypatch.setenv("AOS_CLAUDE_BIN", str(tmp_path / "does-not-exist-claude"))
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 1
    row = _interactions(client, made_a["task_id"])[0]
    assert row["status"] == "error"
    assert _log_lines() == []


def test_timeout_status_error(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch_a = _make_branch(source_repo, "task-a9", "a9.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b9", "b9.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    monkeypatch.setenv("AOS_STUB_MODE", "sleep")
    monkeypatch.setattr(interactions, "CALL_TIMEOUT_SECONDS", 1)
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 1
    row = _interactions(client, made_a["task_id"])[0]
    assert row["status"] == "error"


# == test 5: an escape sequence in a scenario title is stripped =============

def test_scenario_title_escape_sequence_stripped_in_row_and_printed_line(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch_a = _make_branch(source_repo, "task-a10", "a10.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b10", "b10.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    answer = json.dumps({"scenarios": [{"title": "x\x1b]0;x\x07", "why": "w", "test_command": "true"}]})
    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", answer)
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 0, err
    row = _interactions(client, made_a["task_id"])[0]
    assert "\x1b" not in row["scenarios"][0]["title"]
    assert "\x1b" not in out
    assert "\x07" not in out


# == test 6: refused before the call ========================================

def test_different_repos_refused_stub_not_started(
        store: Store, client: TestClient, source_repo: Path, repos_root: Path,
) -> None:
    """B4: real commits as heads, in two real repos, so only the "must share a
    repo" refusal can fire -- a fake sha would also be caught by the later
    "commit is not in source" check, masking a mutation that removed this one."""
    other_repo = _make_repo(repos_root, "repo-other")
    branch_a = _make_branch(source_repo, "task-a-diffrepo", "a-diffrepo.txt", "x\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    branch_b = _make_branch(other_repo, "task-b-diffrepo", "b-diffrepo.txt", "y\n")
    sha_b = _git("rev-parse", branch_b, cwd=other_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-other", head_sha=sha_b)
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 1
    assert "are not in the same repo" in err
    assert _log_lines() == []
    # C6: this refusal must post nothing -- no interaction review recorded.
    assert store.interactions_for_task(made_a["task_id"]) == []


def test_same_task_twice_refused_stub_not_started(
        store: Store, client: TestClient, source_repo: Path,
) -> None:
    """B4: a real commit as the head, so only the "must differ" refusal --
    checked before any task is even fetched -- can fire."""
    branch = _make_branch(source_repo, "task-a-sametwice", "a-sametwice.txt", "x\n")
    sha = _git("rev-parse", branch, cwd=source_repo).stdout.strip()
    made = _task_to_review(store, repo="repo-a", head_sha=sha)
    code, out, err = _run(client, made["task_id"], made["task_id"])
    assert code == 1
    assert f"{made['task_id']} and {made['task_id']} must differ" in err
    assert _log_lines() == []
    # C6: this refusal must post nothing -- no interaction review recorded.
    assert store.interactions_for_task(made["task_id"]) == []


def test_differing_pr_bases_refused_stub_not_started(
        store: Store, client: TestClient, source_repo: Path,
) -> None:
    made_a = _task_to_review(store, repo="repo-a", head_sha="a" * 40)
    made_b = _task_to_review(store, repo="repo-a", head_sha="b" * 40)
    store.annotate_run(made_a["run_id"], pr_base="main")
    store.annotate_run(made_b["run_id"], pr_base="dev")

    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 1
    assert "the two PRs merge into different branches (main, dev)" in err
    assert _log_lines() == []
    # C6: this refusal must post nothing -- no interaction review recorded.
    assert store.interactions_for_task(made_a["task_id"]) == []


def test_task_not_review_refused_stub_not_started(
        store: Store, client: TestClient, source_repo: Path,
) -> None:
    """B4: a real commit as the other task's head, so only the "is not review"
    refusal can fire."""
    branch_b = _make_branch(source_repo, "task-b-notreview", "b-notreview.txt", "y\n")
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    task = store.create_task(repo="repo-a", title="t", brief="b", acceptance=["true"])
    store.ready_task(task["id"])
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)
    code, out, err = _run(client, task["id"], made_b["task_id"])
    assert code == 1
    assert f"task {task['id']} is not review (ready)" in err
    assert _log_lines() == []
    # C6: this refusal must post nothing -- no interaction review recorded.
    assert store.interactions_for_task(made_b["task_id"]) == []


def test_head_not_in_source_refused_stub_not_started(
        store: Store, client: TestClient, source_repo: Path,
) -> None:
    """B4: one task's head is a real commit, the other's is a well-formed but
    nonexistent sha, so only the "commit is not in source" refusal can fire --
    the earlier checks (same task, same repo, both review) all pass for real."""
    branch_a = _make_branch(source_repo, "task-a-headmissing", "a-headmissing.txt", "x\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    fake_head = "f" * 40
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=fake_head)
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 1
    row = _interactions(client, made_a["task_id"])[0]
    assert row["status"] == "error"
    with store.connection() as db:
        stored = db.execute(
            "SELECT raw FROM interaction_reviews WHERE task_a=? ORDER BY rowid DESC LIMIT 1",
            (made_a["task_id"],)).fetchone()
    assert f"commit {fake_head} is not in" in stored["raw"]
    assert _log_lines() == []


# == test 7: a diff above its bound is cut ==================================

def test_diff_above_bound_cut_with_marker_prompt_under_60000(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    big_content = "\n".join(f"line {i}" for i in range(3000)) + "\n"
    branch_a = _make_branch(source_repo, "task-a11", "big.txt", big_content)
    branch_b = _make_branch(source_repo, "task-b11", "b11.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", json.dumps({"scenarios": []}))
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 0, err
    prompt = _last_prompt()
    assert "[cut: " in prompt
    assert "characters omitted]" in prompt
    assert len(prompt) < 60000


# == test 8: both diffs empty: refused, stub not started ====================

def test_both_diffs_empty_refused_stub_not_started(
        store: Store, client: TestClient, source_repo: Path,
) -> None:
    main_sha = _git("rev-parse", "main", cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=main_sha)
    made_b = _task_to_review(store, repo="repo-a", head_sha=main_sha)
    # head must exist as the "not review" task's mismatched repo is already covered;
    # here both tasks legitimately share main's own tip as their "head".
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 1
    assert "nothing to review" in err
    assert _log_lines() == []


# == test 9: nothing about the source or worktree root moves ================

def test_source_untouched_git_config_hash_unchanged(
        store: Store, client: TestClient, source_repo: Path, worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch_a = _make_branch(source_repo, "task-a12", "a12.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b12", "b12.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    branch_tip_before = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    checked_out_before = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=source_repo).stdout.strip()
    config_path = source_repo / ".git" / "config"
    hash_before = hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert not worktree_root.exists()

    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", json.dumps({"scenarios": []}))
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 0, err

    hash_after = hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert hash_before == hash_after
    assert _git("rev-parse", branch_a, cwd=source_repo).stdout.strip() == branch_tip_before
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=source_repo).stdout.strip() == checked_out_before
    assert not worktree_root.exists()
    assert store.get_task(made_a["task_id"])["status"] == "review"
    assert store.get_task(made_b["task_id"])["status"] == "review"


# == parallel repairs, B3: control chars stripped, a failing diff is an error,
#    the prompt is bounded in bytes ==========================================

def test_control_char_in_scope_key_written_directly_stripped_from_prompt(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scope key containing NUL, written straight into the database: the new
    key check (parallel repairs B1) would refuse this through `declare_scope`,
    but this module never imports agent_os.store, so it keeps its own copy of
    the control-character stripping and must not crash on data that predates,
    or bypasses, that check."""
    branch_a = _make_branch(source_repo, "task-a13", "a13.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b13", "b13.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    with store.connection() as db:
        db.execute(
            "INSERT INTO task_intents (id, task_id, run_id, kind, scope_kind, key, operation, "
            "body, created_at, delivery_attempts) VALUES (?, ?, '', 'scope', 'symbol', ?, 'add', "
            "'', ?, 0)",
            (f"{made_a['task_id']}-i1", made_a["task_id"], "bad\x00key", "2020-01-01T00:00:00Z"))

    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", json.dumps({"scenarios": []}))
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 0, err
    prompt = _last_prompt()
    assert "\x00" not in prompt
    assert "badkey" in prompt


def test_git_diff_failing_is_error_not_empty_diff(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch_a = _make_branch(source_repo, "task-a14", "a14.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b14", "b14.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "--stat" in cmd:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: bad revision\n")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(interactions.subprocess, "run", fake_run)
    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", json.dumps({"scenarios": []}))
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 1
    row = _interactions(client, made_a["task_id"])[0]
    assert row["status"] == "error"
    with store.connection() as db:
        stored = db.execute(
            "SELECT raw FROM interaction_reviews WHERE task_a=? ORDER BY rowid DESC LIMIT 1",
            (made_a["task_id"],)).fetchone()
    assert "fatal: bad revision" in stored["raw"]
    assert _log_lines() == []


def test_multibyte_diff_content_bounded_by_bytes_not_just_characters(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each of these characters is 4 bytes in UTF-8: two diffs cut to the
    15 000-character-per-diff bound still total far more than 100 000 bytes, so
    the byte bound must cut further."""
    big_content = "\U0001F600" * 20000 + "\n"
    branch_a = _make_branch(source_repo, "task-a15", "big15.txt", big_content)
    branch_b = _make_branch(source_repo, "task-b15", "big15b.txt", big_content)
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", json.dumps({"scenarios": []}))
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 0, err
    prompt = _last_prompt()
    assert len(prompt.encode("utf-8")) < 100_000
    assert "[cut: " in prompt


# == parallel repairs, B4: checks that could not fail ===========================

def _refs_outside_remotes(repo: Path) -> dict[str, str]:
    """Every ref (name -> object sha) except `refs/remotes/*`, which a fetch
    may legitimately move (parallel repairs C6: the "changes no state" test
    is strengthened to compare every such ref, not just the ones the test
    happens to name)."""
    output = _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=repo).stdout
    refs: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        name, sha = line.rsplit(" ", 1)
        if not name.startswith("refs/remotes/"):
            refs[name] = sha
    return refs


def _hash_worktree_files(repo: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(repo.rglob("*")):
        if ".git" in path.relative_to(repo).parts:
            continue
        if path.is_file():
            hashes[str(path.relative_to(repo))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def test_source_untouched_strengthened_moved_origin_and_task_worktree(
        store: Store, client: TestClient, source_repo: Path, worktree_root: Path,
        origin_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strengthens test 9: a source with an origin that has moved (allowed:
    refs/remotes/* may follow it), a real task worktree elsewhere (untouched),
    and hashes of every work-tree file, before and after -- not just
    .git/config."""
    _set_origin(source_repo, str(origin_repo))
    _git("push", "origin", "main", cwd=source_repo)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=origin_repo)
    _git("fetch", "origin", cwd=source_repo)
    origin_tracking_before = _git("rev-parse", "refs/remotes/origin/main",
                                  cwd=source_repo).stdout.strip()

    branch_a = _make_branch(source_repo, "task-a-nostate", "a-nostate.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b-nostate", "b-nostate.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    # The origin moves on its own, independently of the source: a new commit
    # built with plumbing, since the bare origin has no work tree to commit in.
    _git("config", "user.email", "test@example.com", cwd=origin_repo)
    _git("config", "user.name", "Test", cwd=origin_repo)
    origin_main_before = _git("rev-parse", "main", cwd=origin_repo).stdout.strip()
    tree = _git("rev-parse", f"{origin_main_before}^{{tree}}", cwd=origin_repo).stdout.strip()
    origin_main_after_move = _git("commit-tree", tree, "-p", origin_main_before, "-m",
                                  "origin moves on", cwd=origin_repo).stdout.strip()
    _git("update-ref", "refs/heads/main", origin_main_after_move, cwd=origin_repo)
    assert origin_main_after_move != origin_tracking_before

    # A real task worktree, as a live task would have -- untouched by this run.
    task_worktree = worktree_root / "repo-a" / "AO-99"
    task_worktree.parent.mkdir(parents=True)
    _git("worktree", "add", "-q", "--detach", str(task_worktree), sha_a, cwd=source_repo)
    worktree_hashes_before = _hash_worktree_files(task_worktree)

    branch_tip_before = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    checked_out_before = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=source_repo).stdout.strip()
    config_path = source_repo / ".git" / "config"
    hash_before = hashlib.sha256(config_path.read_bytes()).hexdigest()
    source_hashes_before = _hash_worktree_files(source_repo)
    refs_before = _refs_outside_remotes(source_repo)

    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", json.dumps({"scenarios": []}))
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    with store.connection() as db:
        stored = db.execute(
            "SELECT raw FROM interaction_reviews WHERE task_a=? ORDER BY rowid DESC LIMIT 1",
            (made_a["task_id"],)).fetchone()
    assert code == 0, (out, err, dict(stored) if stored else None)

    # the origin's move is reflected: resolve_base's fetch tracked it.
    origin_tracking_after = _git("rev-parse", "refs/remotes/origin/main",
                                 cwd=source_repo).stdout.strip()
    assert origin_tracking_after == origin_main_after_move

    hash_after = hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert hash_before == hash_after
    assert _git("rev-parse", branch_a, cwd=source_repo).stdout.strip() == branch_tip_before
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=source_repo).stdout.strip() == checked_out_before
    assert _hash_worktree_files(source_repo) == source_hashes_before
    assert _hash_worktree_files(task_worktree) == worktree_hashes_before
    # C6: every ref outside refs/remotes/ compared, not only the ones named
    # above -- a stray ref created anywhere else would be caught here.
    assert _refs_outside_remotes(source_repo) == refs_before
    assert store.get_task(made_a["task_id"])["status"] == "review"
    assert store.get_task(made_b["task_id"])["status"] == "review"


def test_model_process_gets_no_stdin(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
) -> None:
    """The stub records what it reads from stdin: empty. Also asserted
    directly against the spawn call itself (`stdin=subprocess.DEVNULL`): the
    real stub's own fd 0 is DEVNULL regardless of this test process's own
    stdin, which a mutation swapping DEVNULL for an inherited stdin would not
    reliably disturb in every environment, but the direct assertion always
    would."""
    branch_a = _make_branch(source_repo, "task-a-nostdin", "a-nostdin.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b-nostdin", "b-nostdin.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    real_spawn = interactions.harness.spawn
    captured: dict[str, Any] = {}

    def spy_spawn(*args, **kwargs):
        captured["stdin"] = kwargs.get("stdin")
        return real_spawn(*args, **kwargs)

    monkeypatch.setattr(interactions.harness, "spawn", spy_spawn)

    stdin_log = tmp_path / "stdin.log"
    monkeypatch.setenv("AOS_STUB_STDIN_LOG", str(stdin_log))
    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", json.dumps({"scenarios": []}))
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 0, err
    assert captured["stdin"] is subprocess.DEVNULL
    assert stdin_log.read_text() == ""


# == parallel repairs, C5: the byte bound holds, cut no more than needed ====

def test_diffs_cut_by_bytes_no_more_than_the_bound_needs(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4-byte text in the diffs: the old character-based cutting loop shrank
    `diff_limit` (a CHARACTER count) by half the BYTE overage each pass, which
    for 4-byte content cuts roughly four times deeper than the bound needs.
    The fix cuts by bytes directly and stops as soon as the prompt fits."""
    big_content = "\U0001F389" * 20000 + "\n"  # 4 bytes each in UTF-8
    branch_a = _make_branch(source_repo, "task-a16", "big16.txt", big_content)
    branch_b = _make_branch(source_repo, "task-b16", "big16b.txt", big_content)
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", json.dumps({"scenarios": []}))
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 0, err
    prompt = _last_prompt()
    byte_length = len(prompt.encode("utf-8"))
    assert byte_length < interactions.PROMPT_BYTE_MAX
    # Not cut far past what the bound needs: comfortably above half the bound,
    # which the old per-character heuristic (cutting four times too deep for
    # 4-byte content) would have landed well below.
    assert byte_length > interactions.PROMPT_BYTE_MAX // 2


def test_material_cut_at_tail_when_diffs_exhausted_result_at_or_under_bound(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4-byte text in the briefs, with small diffs: even once both diffs are
    cut to nothing, the fixed material (task blocks, intents, integration,
    invariants -- each bounded in CHARACTERS, not bytes) can alone push the
    assembled prompt over the byte bound. The material after
    '--- material ---' is then cut at its tail; the result is never over."""
    branch_a = _make_branch(source_repo, "task-a17", "a17.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b17", "b17.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    emoji_block = "\U0001F389" * 4000  # 4 bytes each; already at each part's own character cap

    monkeypatch.setattr(interactions, "_task_block", lambda task: emoji_block)
    monkeypatch.setattr(interactions, "_integration_block", lambda task: emoji_block)
    monkeypatch.setattr(interactions, "_invariants_block", lambda source, base_sha: emoji_block)
    monkeypatch.setattr(interactions, "_open_intent_lines",
                        lambda client, task_id, other_id: ["\U0001F389" * 6000])

    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", json.dumps({"scenarios": []}))
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 0, err
    prompt = _last_prompt()
    assert len(prompt.encode("utf-8")) < interactions.PROMPT_BYTE_MAX


def test_timeout_kills_the_process_group_grandchild_gone(
        store: Store, client: TestClient, source_repo: Path, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
) -> None:
    branch_a = _make_branch(source_repo, "task-a-grandchild", "a-grandchild.txt", "x\n")
    branch_b = _make_branch(source_repo, "task-b-grandchild", "b-grandchild.txt", "y\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a)
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)

    grandchild_pid_path = tmp_path / "grandchild.pid"
    monkeypatch.setenv("AOS_STUB_GRANDCHILD_PID", str(grandchild_pid_path))
    # A sh stub spawns the grandchild within milliseconds: the Python stub's
    # interpreter start-up could lose the race to the timeout under load and
    # never write the pid file (H1 F197).
    sleep_stub = tmp_path / "sleep-stub"
    sleep_stub.write_text('#!/bin/sh\nsleep 60 & echo $! > "$AOS_STUB_GRANDCHILD_PID"\nwait\n')
    sleep_stub.chmod(0o755)
    monkeypatch.setenv("AOS_CLAUDE_BIN", str(sleep_stub))
    monkeypatch.setattr(interactions, "CALL_TIMEOUT_SECONDS", 3)
    started = time.monotonic()
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    elapsed = time.monotonic() - started
    assert code == 1
    # The whole run must return promptly at the ~1s timeout, not after the
    # grandchild's own 60s sleep completes naturally -- a weaker kill (of the
    # harness process alone, say) would leave the grandchild holding the
    # stdout pipe open and the run would only return once it exits on its own,
    # which would also happen to leave no live grandchild to find, masking
    # the very defect this test exists to catch.
    assert elapsed < 30, f"took {elapsed:.1f}s -- the process group was not killed promptly"
    row = _interactions(client, made_a["task_id"])[0]
    assert row["status"] == "error"

    # The stub wrote the pid before the kill or never: a short bounded wait.
    for _ in range(20):
        if grandchild_pid_path.exists() and grandchild_pid_path.read_text().strip():
            break
        time.sleep(0.05)
    assert grandchild_pid_path.exists() and grandchild_pid_path.read_text().strip(), (
        "the stub was killed before it spawned the grandchild: the start-up race of H1 F197")
    pid = int(grandchild_pid_path.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_interaction_prompt_preserves_inherited_run_checks(
    store: Store, client: TestClient, source_repo: Path,
) -> None:
    branch_a = _make_branch(source_repo, "task-inherited-a", "ia.txt", "a\n")
    branch_b = _make_branch(source_repo, "task-inherited-b", "ib.txt", "b\n")
    sha_a = _git("rev-parse", branch_a, cwd=source_repo).stdout.strip()
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()
    made_a = _task_to_review(store, repo="repo-a", head_sha=sha_a, acceptance=[],
                             inherited=["python -m pytest inherited"])
    made_b = _task_to_review(store, repo="repo-a", head_sha=sha_b)
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 0, err
    assert "Acceptance:\n  - python -m pytest inherited" in _last_prompt()


def test_interactions_refuse_missing_run_snapshot(
    store: Store, client: TestClient, source_repo: Path,
) -> None:
    branch_a = _make_branch(source_repo, "task-missing-a", "ma.txt", "a\n")
    branch_b = _make_branch(source_repo, "task-missing-b", "mb.txt", "b\n")
    made_a = _task_to_review(store, repo="repo-a",
                             head_sha=_git("rev-parse", branch_a, cwd=source_repo).stdout.strip())
    made_b = _task_to_review(store, repo="repo-a",
                             head_sha=_git("rev-parse", branch_b, cwd=source_repo).stdout.strip())
    with store.connection() as db:
        db.execute("UPDATE task_runs SET effective_acceptance=NULL WHERE id=?", (made_a["run_id"],))
    code, out, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 1 and err.strip()
    assert _log_lines() == []
    assert _interactions(client, made_a["task_id"]) == []


def test_interaction_reads_ignore_inherited_git_directory(
        store: Store, client: TestClient, source_repo: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    (source_repo / "docs").mkdir()
    (source_repo / "docs" / "invariants.md").write_text("Every invoice balances.\n")
    _git("add", "docs/invariants.md", cwd=source_repo)
    _git("commit", "-qm", "invariants", cwd=source_repo)
    branch_a = _make_branch(source_repo, "task-env-a", "a.txt", "invoice A\n")
    branch_b = _make_branch(source_repo, "task-env-b", "b.txt", "invoice B\n")
    made_a = _task_to_review(store, repo="repo-a",
                             head_sha=_git("rev-parse", branch_a, cwd=source_repo).stdout.strip())
    made_b = _task_to_review(store, repo="repo-a",
                             head_sha=_git("rev-parse", branch_b, cwd=source_repo).stdout.strip())
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "unrelated.git"))
    monkeypatch.setenv("AOS_STUB_RESULT_TEXT", json.dumps({"scenarios": []}))
    code, _, err = _run(client, made_a["task_id"], made_b["task_id"])
    assert code == 0, err
    prompt = _last_prompt()
    assert "Every invoice balances." in prompt
    assert "+invoice A" in prompt and "+invoice B" in prompt
    assert _interactions(client, made_a["task_id"])[0]["status"] == "ok"
