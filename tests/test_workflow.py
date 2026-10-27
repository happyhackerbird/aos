from __future__ import annotations

import os
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

from agent_os import workflow
from agent_os.policy import Default
from agent_os.workflow import WorkflowError, parse_workflow, read_workflow

PREFIX = "workflow: aos-workflow.toml: "
VALID = '''test_command = "  make check  "
acceptance = ["  true  ", "echo ok"]
prompt = "  Be careful.\\n"
'''


def _env(home: Path) -> dict[str, str]:
    return {**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com"}


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                            capture_output=True, env=_env(cwd.parent / "home"))
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "home").mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "gc.auto", "0", cwd=repo)
    (repo / "README").write_text("base\n")
    _git("add", "README", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    return repo


def _commit(repo: Path, data: bytes | str, message: str = "workflow") -> bytes:
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    (repo / "aos-workflow.toml").write_bytes(raw)
    _git("add", "aos-workflow.toml", cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)
    return raw


def _default(repo: Path) -> Default:
    sha = _git("rev-parse", "HEAD", cwd=repo)
    branch = _git("branch", "--show-current", cwd=repo)
    return Default("local", branch, sha, f"{branch}@{sha[:12]}")


def _refusal(tmp_path: Path, text: str | bytes) -> str:
    repo = _repo(tmp_path)
    _commit(repo, text)
    with pytest.raises(WorkflowError) as info:
        read_workflow(str(repo), _default(repo))
    return str(info.value)


def test_absent_file_gives_none(tmp_path):
    repo = _repo(tmp_path)
    assert read_workflow(str(repo), _default(repo)) is None


def test_empty_default_gives_none(tmp_path):
    repo = _repo(tmp_path)
    assert read_workflow(str(repo), Default("empty", "", "", "")) is None


def test_full_valid_file(tmp_path):
    repo = _repo(tmp_path)
    raw = _commit(repo, VALID)
    default = _default(repo)
    result = read_workflow(str(repo), default)
    assert result is not None
    assert result.test_command == "make check"
    assert result.acceptance == ("true", "echo ok")
    assert result.prompt == "Be careful."
    assert result.sha == sha256(raw).hexdigest()
    assert result.ref == default.ref


def test_read_is_at_default_sha_not_worktree_or_other_branch(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, 'test_command = "one"\n')
    default = _default(repo)
    (repo / "aos-workflow.toml").write_text('test_command = "edited"\n')
    assert read_workflow(str(repo), default).test_command == "one"
    _git("checkout", "-q", "-b", "other", cwd=repo)
    _commit(repo, 'test_command = "other"\n', "other branch")
    assert read_workflow(str(repo), default).test_command == "one"


def test_file_only_on_other_branch_is_absent(tmp_path):
    repo = _repo(tmp_path)
    default = _default(repo)
    _git("checkout", "-q", "-b", "other", cwd=repo)
    _commit(repo, 'test_command = "other"\n')
    assert read_workflow(str(repo), default) is None


def test_invalid_toml(tmp_path):
    assert _refusal(tmp_path, "test_command = \n").startswith(PREFIX)


def test_non_utf8(tmp_path):
    assert _refusal(tmp_path, b'test_command = "\xff"\n').startswith(PREFIX)


def test_unknown_key(tmp_path):
    assert _refusal(tmp_path, 'zeta = 1\nalpha = 2\n') == PREFIX + "unknown key alpha"


def test_table_is_unknown_key(tmp_path):
    assert _refusal(tmp_path, '[x]\na = 1\n') == PREFIX + "unknown key x"


@pytest.mark.parametrize("text, message", [
    ("test_command = 3\n", "test_command must be a string"),
    ('test_command = "  "\n', "test_command is blank"),
    ('test_command = "a\\u0001b"\n', "test_command has a control character"),
    ('test_command = "a\\nb"\n', "test_command has a control character"),
    ("acceptance = 'x'\n", "acceptance must be a list of strings"),
    ("acceptance = ['x', 3]\n", "acceptance must be a list of strings"),
    ("acceptance = []\n", "acceptance is empty; omit the key for no default"),
    ("acceptance = ['ok', ' ']\n", "acceptance[1] is blank"),
    ('acceptance = ["ok", "a\\u0007"]\n', "acceptance[1] has a control character"),
    ("prompt = 5\n", "prompt must be a string"),
    ('prompt = " \\n "\n', "prompt is blank"),
    ('prompt = "a\\u0001"\n', "prompt has a control character"),
])
def test_refusal_messages(text, message):
    with pytest.raises(WorkflowError) as info:
        parse_workflow(text)
    assert str(info.value) == PREFIX + message


def test_tab_allowed_in_command_and_prompt():
    tc, acc, prompt = parse_workflow('test_command = "a\\tb"\nacceptance = ["c\\td"]\nprompt = "e\\tf\\ng"\n')
    assert (tc, acc, prompt) == ("a\tb", ("c\td",), "e\tf\ng")


def test_crlf_prompt_normalised():
    assert parse_workflow('prompt = """\r\nline one\r\nline two\r\n"""\n')[2] == "line one\nline two"


def test_prompt_cap_is_bytes_not_characters():
    assert parse_workflow(f'prompt = "{"é" * 2048}"\n')[2] == "é" * 2048
    with pytest.raises(WorkflowError) as info:
        parse_workflow(f'prompt = "{"é" * 2049}"\n')
    assert str(info.value) == PREFIX + "prompt is 4098 bytes; the cap is 4096"


def test_symlink_is_refused(tmp_path):
    repo = _repo(tmp_path)
    (repo / "aos-workflow.toml").symlink_to("README")
    _git("add", "aos-workflow.toml", cwd=repo)
    _git("commit", "-q", "-m", "link", cwd=repo)
    with pytest.raises(WorkflowError) as info:
        read_workflow(str(repo), _default(repo))
    assert str(info.value) == "workflow: aos-workflow.toml is not a regular file"


def test_directory_is_refused(tmp_path):
    repo = _repo(tmp_path)
    (repo / "aos-workflow.toml").mkdir()
    (repo / "aos-workflow.toml" / "inner").write_text("x\n")
    _git("add", "aos-workflow.toml/inner", cwd=repo)
    _git("commit", "-q", "-m", "dir", cwd=repo)
    with pytest.raises(WorkflowError) as info:
        read_workflow(str(repo), _default(repo))
    assert str(info.value) == "workflow: aos-workflow.toml is not a regular file"


def test_git_failure_is_a_workflow_error(tmp_path):
    repo = _repo(tmp_path)
    default = Default("local", "main", "0" * 40, "main@000000000000")
    with pytest.raises(WorkflowError) as info:
        read_workflow(str(repo), default)
    assert str(info.value).startswith("workflow: ")


@pytest.mark.parametrize("explicit, repo_checks, repo_test, expected, source", [
    (["task-check"], ("repo-check",), "full-suite", ["task-check"], "task"),
    ([], ("repo-check", "lint"), "full-suite", ["repo-check", "lint"], "workflow.acceptance"),
    ([], (), "full-suite", ["full-suite"], "workflow.test_command"),
])
def test_execution_check_precedence_and_frozen_history(explicit, repo_checks, repo_test, expected, source):
    default = Default("local", "main", "a" * 40, "main@aaaaaaaaaaaa")
    repo_workflow = workflow.Workflow(repo_test, repo_checks, "instructions", "b" * 64, default.ref)
    task = {"acceptance": explicit.copy()}
    snapshot = workflow.execution_snapshot(task, repo_workflow, default)
    assert snapshot == {
        "effective_acceptance": expected,
        "acceptance_source": source,
        "workflow_commit": default.sha,
        "workflow_ref": default.ref,
        "workflow_sha": "b" * 64,
    }
    task["acceptance"].append("later-task-edit")
    returned_checks = workflow.run_acceptance(snapshot)
    returned_checks.append("later-consumer-edit")
    assert workflow.run_acceptance(snapshot) == expected


def test_explicit_checks_work_without_repo_workflow():
    default = Default("local", "main", "a" * 40, "main@aaaaaaaaaaaa")
    snapshot = workflow.execution_snapshot({"acceptance": ["task-check"]}, None, default)
    assert workflow.run_acceptance(snapshot) == ["task-check"]
    assert snapshot["acceptance_source"] == "task"
    assert snapshot["workflow_commit"] is None


@pytest.mark.parametrize("commands", [None, [], [""], [" "], [1], "check"])
def test_missing_or_invalid_run_checks_cannot_be_executed(commands):
    with pytest.raises(WorkflowError):
        workflow.run_acceptance({"effective_acceptance": commands})


def test_no_repo_or_task_checks_is_a_preflight_error():
    default = Default("local", "main", "a" * 40, "main@aaaaaaaaaaaa")
    for repo_workflow in (None, workflow.Workflow(None, (), "instructions only", "b" * 64, default.ref)):
        with pytest.raises(WorkflowError):
            workflow.execution_snapshot({"acceptance": []}, repo_workflow, default)
