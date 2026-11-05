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


def test_protected_and_watch_parse_as_stripped_patterns():
    fields = parse_workflow('protected = [" tests/acceptance/** ", "tasks/", "scripts/check*.sh"]\n'
                            'watch = ["uv.lock", "**/conftest.py"]\n')
    assert fields["protected"] == ("tests/acceptance/**", "tasks/", "scripts/check*.sh")
    assert fields["watch"] == ("uv.lock", "**/conftest.py")
    absent = parse_workflow('test_command = "x"\n')
    assert absent["protected"] == () and absent["watch"] == ()


@pytest.mark.parametrize("key", ["protected", "watch"])
@pytest.mark.parametrize("value, message", [
    ("'tests/**'", "{key} must be a list of strings"),
    ("['a', 3]", "{key} must be a list of strings"),
    ("[]", "{key} is empty; omit the key for none"),
    ("['ok', '  ']", "{key}[1] is blank"),
    ('["a\\tb"]', "{key}[0] has a control character"),
    ('["a\\u0007"]', "{key}[0] has a control character"),
    ("['/etc/passwd']", "{key}[0] starts with /; patterns are relative to the repository root"),
    ("[':(exclude)x']", "{key}[0] starts with :; pathspec magic is not accepted"),
    ("['../other']", "{key}[0] has an empty, . or .. path component"),
    ("['tests/../aos-policy.toml']", "{key}[0] has an empty, . or .. path component"),
    ("['./tests']", "{key}[0] has an empty, . or .. path component"),
    ("['tests//acceptance']", "{key}[0] has an empty, . or .. path component"),
])
def test_pattern_refusals(key, value, message):
    with pytest.raises(WorkflowError) as info:
        parse_workflow(f"{key} = {value}\n")
    assert str(info.value) == PREFIX + message.format(key=key)


def test_the_aos_files_are_always_locked(tmp_path):
    assert workflow.locked_patterns() == ("aos-policy.toml", "aos-workflow.toml")
    assert workflow.locked_patterns(("tests/**", "aos-policy.toml")) == ("aos-policy.toml", "aos-workflow.toml",
                                                                        "tests/**")
    repo = _repo(tmp_path)
    _commit(repo, 'protected = ["tests/acceptance/**"]\nwatch = ["uv.lock"]\n')
    result = read_workflow(str(repo), _default(repo))
    assert result.protected == ("tests/acceptance/**",) and result.watch == ("uv.lock",)
    assert workflow.locked_patterns(result.protected) == ("aos-policy.toml", "aos-workflow.toml",
                                                          "tests/acceptance/**")


def test_required_checks_parse_and_merge_with_the_task_list():
    fields = parse_workflow('required_checks = ["merge-check", "review", "review"]\n')
    assert fields["required_checks"] == ("merge-check", "review")
    default = Default("local", "main", "a" * 40, "main@aaaaaaaaaaaa")
    repo_workflow = workflow.Workflow(None, ("check",), None, "b" * 64, default.ref,
                                      required_checks=fields["required_checks"])
    snapshot = workflow.execution_snapshot({"acceptance": [], "required_checks": ["real-service", "review"]},
                                           repo_workflow, default)
    assert snapshot["required_checks"] == ["merge-check", "review", "real-service"]
    bare = workflow.execution_snapshot({"acceptance": ["x"]}, None, default)
    assert "required_checks" not in bare  # absent, so a bus without the field still takes the claim
    only_task = workflow.execution_snapshot({"acceptance": ["x"], "required_checks": ["review"]}, None, default)
    assert only_task["required_checks"] == ["review"]


@pytest.mark.parametrize("value, message", [
    ("'review'", "required_checks must be a list of strings"),
    ("[]", "required_checks is empty; omit the key for none"),
    ("['Review']", "required_checks[0] is not a check name ([a-z0-9][a-z0-9-]{0,39})"),
    ("['ok', 'two words']", "required_checks[1] is not a check name ([a-z0-9][a-z0-9-]{0,39})"),
    (str([f"c{i}" for i in range(21)]), "required_checks holds more than 20 names"),
])
def test_required_checks_refusals(value, message):
    with pytest.raises(WorkflowError) as info:
        parse_workflow(f"required_checks = {value}\n")
    assert str(info.value) == PREFIX + message


def test_tab_allowed_in_command_and_prompt():
    fields = parse_workflow('test_command = "a\\tb"\nacceptance = ["c\\td"]\nprompt = "e\\tf\\ng"\n')
    assert (fields["test_command"], fields["acceptance"], fields["prompt"]) == ("a\tb", ("c\td",), "e\tf\ng")


def test_crlf_prompt_normalised():
    assert parse_workflow('prompt = """\r\nline one\r\nline two\r\n"""\n')["prompt"] == "line one\nline two"


def test_prompt_cap_is_bytes_not_characters():
    assert parse_workflow(f'prompt = "{"é" * 2048}"\n')["prompt"] == "é" * 2048
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


def test_baseline_key_is_a_boolean():
    assert parse_workflow("baseline = true\n")["baseline"] is True
    assert parse_workflow("baseline = false\n")["baseline"] is False
    assert parse_workflow('test_command = "x"\n')["baseline"] is False
    for value in ('"true"', "1", "[]"):
        with pytest.raises(WorkflowError) as info:
            parse_workflow(f"baseline = {value}\n")
        assert str(info.value) == PREFIX + "baseline must be true or false"


def test_test_changes_is_not_a_configurable_check():
    with pytest.raises(WorkflowError) as info:
        parse_workflow('required_checks = ["review", "test-changes"]\n')
    assert str(info.value) == PREFIX + ("required_checks[1]: test-changes is added by AOS when tests are lost; "
                                        "turn on baseline instead")
