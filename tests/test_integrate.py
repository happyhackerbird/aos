from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_os import gitops, integrate, openshell_runner
from agent_os.api import create_app
from agent_os.config import Settings
from agent_os.store import Store
from agent_os.telegram import Telegram

from runner_fixtures import origin_repo, _set_origin  # noqa: F401 (fixture + helper, reused per directive)
from runner_fixtures import _write_services, DOCKER_UNAVAILABLE
from test_openshell_runner import BASE_ENV, FakeShell, POLICY


# -- git helpers, in the style of tests/test_runner.py -----------------------
#
# GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE are stripped from the spawn
# environment: an ambient git invocation surrounding the test run (a hook, a
# wrapping `git` command) must never leak into the scratch repositories these
# tests build.

def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
          if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)


@pytest.fixture
def repos_root(tmp_path: Path) -> Path:
    root = tmp_path / "repos"
    root.mkdir()
    return root


@pytest.fixture
def source_repo(repos_root: Path) -> Path:
    repo = repos_root / "repo-a"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


@pytest.fixture
def worktree_root(tmp_path: Path) -> Path:
    return tmp_path / "worktrees"


@pytest.fixture(autouse=True)
def _base_env(monkeypatch: pytest.MonkeyPatch, repos_root: Path, worktree_root: Path) -> None:
    monkeypatch.setenv("AOS_REPO_ROOT", str(repos_root))
    monkeypatch.setenv("AOS_WORKTREE_ROOT", str(worktree_root))
    monkeypatch.setenv("AOS_HOST", "mac")
    monkeypatch.setenv("AOS_ACCEPT_TIMEOUT_SECONDS", "10")


@pytest.fixture(autouse=True)
def sandbox_transport(monkeypatch, tmp_path, source_repo, _base_env):
    """Exercise the OpenShell integration path without an external gateway."""
    for key, value in BASE_ENV.items():
        if key != "AOS_REPO_ROOT":
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("AOS_OPENSHELL_STATE_DIR", str(tmp_path / "sandbox-state"))
    (source_repo / "aos-policy.toml").write_text(POLICY)
    _git("add", "aos-policy.toml", cwd=source_repo)
    _git("commit", "-q", "-m", "sandbox policy", cwd=source_repo)
    shell = FakeShell()
    monkeypatch.setattr(openshell_runner, "openshell", shell)
    monkeypatch.setattr(openshell_runner, "pause", lambda seconds: None)
    ticks = iter(range(0, 10**6, 50))
    monkeypatch.setattr(openshell_runner, "clock", lambda: next(ticks))
    return shell


@pytest.fixture
def client(settings: Settings, store: Store, telegram: Telegram) -> TestClient:
    test_client = TestClient(create_app(settings, store=store, telegram=telegram, background=False))
    test_client.headers["Authorization"] = "Bearer test-bus-token"
    return test_client


def _push_to_origin(repo: Path, origin: Path) -> None:
    _set_origin(repo, str(origin))
    _git("push", "origin", "main", cwd=repo)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=origin)


def _make_branch(repo: Path, branch: str, filename: str, content: str) -> str:
    """Branch off the currently checked-out ref, write one file, commit, and
    return to the original ref (the repo's own checkout is never left moved)."""
    current = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout.strip()
    _git("checkout", "-q", "-b", branch, cwd=repo)
    (repo / filename).write_text(content)
    _git("add", filename, cwd=repo)
    _git("commit", "-q", "-m", f"{branch}: touch {filename}", cwd=repo)
    _git("checkout", "-q", current, cwd=repo)
    return branch


def _task_to_review(store: Store, *, repo: str, head_sha: str,
                    acceptance: list[str] | None = None,
                    inherited: list[str] | None = None) -> dict[str, Any]:
    task = store.create_task(repo=repo, title="t", brief="b",
                             acceptance=acceptance if acceptance is not None else ["true"])
    tid = task["id"]
    store.ready_task(tid)
    snapshot = ({"effective_acceptance": inherited, "acceptance_source": "workflow.acceptance",
                 "workflow_commit": "default-commit", "workflow_ref": "main", "workflow_sha": "workflow-sha"}
                if inherited is not None else
                {"effective_acceptance": task["acceptance"], "acceptance_source": "task"})
    run = store.claim_task(tid, worker="w", worker_host="mac", lease_seconds=600,
                           expected_spec_hash=task["spec_hash"], **snapshot)
    store.start_run(run["id"], run["claim_token"], base_commit="a", branch="b", worktree="/x")
    store.finish_run(run["id"], run["claim_token"], status="review", report="ok",
                     result_commit=head_sha)
    return {"task_id": tid, "run_id": run["id"]}


def _integrate(client: TestClient, task_id: str, *, keep: bool = False) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = integrate.integrate_task(client, task_id=task_id, keep=keep, environ=dict(os.environ),
                                    out=out, err=err, backend="openshell")
    return code, out.getvalue(), err.getvalue()


def _kept_scratch(output: str) -> Path:
    return Path(next(line.removeprefix("scratch worktree kept at ")
                     for line in output.splitlines() if line.startswith("scratch worktree kept at ")))


# == U2 test 1: second of two branches passes with the new base_sha =========

def test_second_of_two_branches_passes_with_new_base_sha(
    store: Store, client: TestClient, source_repo: Path, origin_repo: Path,
) -> None:
    _push_to_origin(source_repo, origin_repo)
    branch_a = _make_branch(source_repo, "task-a", "a.txt", "from a\n")
    branch_b = _make_branch(source_repo, "task-b", "b.txt", "from b\n")
    sha_b = _git("rev-parse", branch_b, cwd=source_repo).stdout.strip()

    _git("merge", "--no-ff", "-q", "-m", "merge task-a", branch_a, cwd=source_repo)
    new_main_sha = _git("rev-parse", "main", cwd=source_repo).stdout.strip()
    _git("push", "origin", "main", cwd=source_repo)

    made = _task_to_review(store, repo="repo-a", head_sha=sha_b)
    code, out, err = _integrate(client, made["task_id"])
    assert code == 0, err
    row = store.get_run(made["run_id"])["integration"]
    assert row["status"] == "passed"
    assert row["base_sha"] == new_main_sha
    assert row["base_branch"] == "main"
    assert f"integration passed {made['task_id']}" in out


# == U2 test 2: conflicting branches report conflict and name the file ======

def test_conflicting_branches_report_conflict_and_name_the_file(
    store: Store, client: TestClient, source_repo: Path, origin_repo: Path,
) -> None:
    _push_to_origin(source_repo, origin_repo)
    _make_branch(source_repo, "task-x", "README.md", "hello from x\n")
    branch_y = _make_branch(source_repo, "task-y", "README.md", "hello from y\n")
    sha_y = _git("rev-parse", branch_y, cwd=source_repo).stdout.strip()

    _git("merge", "--no-ff", "-q", "-m", "merge task-x", "task-x", cwd=source_repo)
    _git("push", "origin", "main", cwd=source_repo)

    made = _task_to_review(store, repo="repo-a", head_sha=sha_y)
    code, out, err = _integrate(client, made["task_id"])
    assert code == 1
    row = store.get_run(made["run_id"])["integration"]
    assert row["status"] == "conflict"
    assert "README.md" in row["output"]


# == U2 test 3: two branches merge cleanly and break together ===============

def test_failed_sandbox_verdict_is_for_the_combined_merge(
    store: Store, client: TestClient, source_repo: Path, origin_repo: Path, sandbox_transport,
) -> None:
    (source_repo / "lib.py").write_text("def foo():\n    return 1\n")
    (source_repo / "caller.py").write_text("print('ok')\n")
    _git("add", "lib.py", "caller.py", cwd=source_repo)
    _git("commit", "-q", "-m", "add lib and caller", cwd=source_repo)
    _push_to_origin(source_repo, origin_repo)

    _make_branch(source_repo, "task-c1", "lib.py", "def bar():\n    return 1\n")
    _make_branch(source_repo, "task-c2", "caller.py", "import lib\nlib.foo()\n")

    test_command = "python3 caller.py"
    _commit_workflow_on_main(source_repo, origin_repo, f'test_command = "{test_command}"\n')

    sandbox_transport.accept_rc = "1"
    sandbox_transport.accept_tail = b"AttributeError: module lib has no attribute foo"

    _git("merge", "--no-ff", "-q", "-m", "merge task-c1", "task-c1", cwd=source_repo)
    _git("push", "origin", "main", cwd=source_repo)

    sha_c2 = _git("rev-parse", "task-c2", cwd=source_repo).stdout.strip()
    made = _task_to_review(store, repo="repo-a", head_sha=sha_c2)
    code, out, err = _integrate(client, made["task_id"])
    assert code == 1
    row = store.get_run(made["run_id"])["integration"]
    assert row["status"] == "failed"
    assert "AttributeError" in row["output"] or "foo" in row["output"]
    with tarfile.open(fileobj=io.BytesIO(sandbox_transport.uploads["/sandbox/incoming/job.tgz"])) as job:
        assert b"def bar()" in job.extractfile("lib.py").read()
        assert b"lib.foo()" in job.extractfile("caller.py").read()


# == U2 test 4: no origin, base is the source's current branch ==============

def test_no_origin_base_is_the_sources_current_branch(
    store: Store, client: TestClient, source_repo: Path,
) -> None:
    branch = _make_branch(source_repo, "task-solo", "solo.txt", "solo\n")
    sha = _git("rev-parse", branch, cwd=source_repo).stdout.strip()
    base_sha = _git("rev-parse", "main", cwd=source_repo).stdout.strip()

    made = _task_to_review(store, repo="repo-a", head_sha=sha)
    code, out, err = _integrate(client, made["task_id"])
    assert code == 0, err
    row = store.get_run(made["run_id"])["integration"]
    assert row["status"] == "passed"
    assert row["base_branch"] == "main"
    assert row["base_sha"] == base_sha


# == A1: resolve_base(want=...) and integrate_task uses the run's pr_base ===

def test_resolve_base_with_want_and_origin_uses_origin_want_after_fetch(
    source_repo: Path, origin_repo: Path,
) -> None:
    _push_to_origin(source_repo, origin_repo)
    _git("checkout", "-q", "-b", "release", cwd=source_repo)
    (source_repo / "release.txt").write_text("release\n")
    _git("add", "release.txt", cwd=source_repo)
    _git("commit", "-q", "-m", "release init", cwd=source_repo)
    _git("push", "origin", "release", cwd=source_repo)
    release_sha = _git("rev-parse", "release", cwd=source_repo).stdout.strip()
    _git("checkout", "-q", "main", cwd=source_repo)

    base_branch, base_sha = integrate.resolve_base(str(source_repo), want="release")
    assert base_branch == "release"
    assert base_sha == release_sha


def test_resolve_base_with_want_no_origin_uses_local_branch(source_repo: Path) -> None:
    _make_branch(source_repo, "release", "release.txt", "release\n")
    release_sha = _git("rev-parse", "release", cwd=source_repo).stdout.strip()
    base_branch, base_sha = integrate.resolve_base(str(source_repo), want="release")
    assert base_branch == "release"
    assert base_sha == release_sha


def test_resolve_base_want_missing_ref_raises_giterror(source_repo: Path) -> None:
    with pytest.raises(gitops.GitError):
        integrate.resolve_base(str(source_repo), want="does-not-exist")


def test_integrate_uses_pr_base_as_want_when_set(
    store: Store, client: TestClient, source_repo: Path, origin_repo: Path,
) -> None:
    _push_to_origin(source_repo, origin_repo)
    _git("checkout", "-q", "-b", "release", cwd=source_repo)
    (source_repo / "release.txt").write_text("release\n")
    _git("add", "release.txt", cwd=source_repo)
    _git("commit", "-q", "-m", "release init", cwd=source_repo)
    _git("push", "origin", "release", cwd=source_repo)
    release_sha = _git("rev-parse", "release", cwd=source_repo).stdout.strip()
    _git("checkout", "-q", "main", cwd=source_repo)

    branch = _make_branch(source_repo, "task-rel", "task.txt", "x\n")
    sha = _git("rev-parse", branch, cwd=source_repo).stdout.strip()

    made = _task_to_review(store, repo="repo-a", head_sha=sha)
    store.annotate_run(made["run_id"], pr_base="release")

    code, out, err = _integrate(client, made["task_id"])
    assert code == 0, err
    row = store.get_run(made["run_id"])["integration"]
    assert row["base_branch"] == "release"
    assert row["base_sha"] == release_sha


def test_existing_scratch_is_untouched_and_kept_attempts_are_independent(
    store: Store, client: TestClient, source_repo: Path, worktree_root: Path,
) -> None:
    made = _review_task(store, source_repo, "task-kept", ["touch ran.marker"])
    scratch = worktree_root / "repo-a" / f".integrate-{made['task_id']}"
    scratch.parent.mkdir(parents=True)
    base_sha = _git("rev-parse", "main", cwd=source_repo).stdout.strip()
    _git("worktree", "add", "--detach", str(scratch), base_sha, cwd=source_repo)
    (scratch / "uncommitted.txt").write_text("owned by another attempt\n")

    code, out, err = _integrate(client, made["task_id"])
    assert code == 0, err
    assert list(scratch.parent.iterdir()) == [scratch]
    assert (scratch / "uncommitted.txt").read_text() == "owned by another attempt\n"
    assert _git("rev-parse", "HEAD", cwd=scratch).stdout.strip() == base_sha

    kept = []
    for _ in range(2):
        code, out, err = _integrate(client, made["task_id"], keep=True)
        assert code == 0, err
        kept.append(_kept_scratch(out))
    assert kept[0] != kept[1] and all(path.is_dir() for path in kept)
    assert not any((path / "ran.marker").exists() for path in kept)  # the checks ran in the sandbox, not here
    assert (scratch / "uncommitted.txt").exists()


# == U2 test 6: nothing about the source or the task's own worktree moves ===

def test_source_and_task_worktree_untouched_git_config_hash_unchanged(
    store: Store, client: TestClient, source_repo: Path, origin_repo: Path, tmp_path: Path,
) -> None:
    _push_to_origin(source_repo, origin_repo)
    branch = _make_branch(source_repo, "task-safe", "safe.txt", "x\n")
    sha = _git("rev-parse", branch, cwd=source_repo).stdout.strip()

    task_worktree = tmp_path / "task-owned-worktree"
    _git("worktree", "add", str(task_worktree), branch, cwd=source_repo)

    branch_tip_before = _git("rev-parse", branch, cwd=source_repo).stdout.strip()
    checked_out_before = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=source_repo).stdout.strip()
    task_worktree_head_before = _git("rev-parse", "HEAD", cwd=task_worktree).stdout.strip()
    config_path = source_repo / ".git" / "config"
    hash_before = hashlib.sha256(config_path.read_bytes()).hexdigest()

    made = _task_to_review(store, repo="repo-a", head_sha=sha)
    code, out, err = _integrate(client, made["task_id"])
    assert code == 0, err

    hash_after = hashlib.sha256(config_path.read_bytes()).hexdigest()
    print(f"U2 test 6 .git/config sha256 before={hash_before} after={hash_after}")
    assert hash_before == hash_after
    assert _git("rev-parse", branch, cwd=source_repo).stdout.strip() == branch_tip_before
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=source_repo).stdout.strip() == checked_out_before
    assert _git("rev-parse", "HEAD", cwd=task_worktree).stdout.strip() == task_worktree_head_before


# == U2 test 7: a task that is not review: exit 1, nothing posted ===========

def test_task_not_review_exits_1_and_posts_nothing(store: Store, client: TestClient) -> None:
    task = store.create_task(repo="repo-a", title="t", brief="b", acceptance=["true"])
    store.ready_task(task["id"])
    code, out, err = _integrate(client, task["id"])
    assert code == 1
    assert err.strip()
    assert store.get_task(task["id"])["runs"] == []
    assert store.get_task(task["id"])["status"] == "ready"


# == Integration does not depend on the repo registry ======================

class _RepoRegistryFails:
    """Fail registry reads while forwarding all other requests."""

    def __init__(self, inner: TestClient, *, response: httpx.Response | None = None,
                raises: Exception | None = None) -> None:
        self.inner, self.response, self.raises = inner, response, raises

    def get(self, path: str, *args: Any, **kwargs: Any) -> httpx.Response:
        if path == "/repos":
            if self.raises is not None:
                raise self.raises
            assert self.response is not None
            return self.response
        return self.inner.get(path, *args, **kwargs)

    def post(self, path: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return self.inner.post(path, *args, **kwargs)


def test_registry_unavailable_does_not_hide_saved_checks(
    store: Store, client: TestClient, source_repo: Path, origin_repo: Path, worktree_root: Path,
) -> None:
    _push_to_origin(source_repo, origin_repo)
    branch = _make_branch(source_repo, "task-reg", "reg.txt", "x\n")
    sha = _git("rev-parse", branch, cwd=source_repo).stdout.strip()
    made = _task_to_review(store, repo="repo-a", head_sha=sha, acceptance=["touch ran.marker"])

    wrapped = _RepoRegistryFails(client, response=httpx.Response(503, json={"message": "down"}))
    out, err = io.StringIO(), io.StringIO()
    code = integrate.integrate_task(wrapped, task_id=made["task_id"], keep=True,
                                    environ=dict(os.environ), out=out, err=err, backend="openshell")
    assert code == 0, (out.getvalue(), err.getvalue())
    row = store.get_run(made["run_id"])["integration"]
    assert row["status"] == "passed"
    scratch = _kept_scratch(out.getvalue())
    assert not (scratch / "ran.marker").exists()  # no acceptance command runs on the host
    assert "$ touch ran.marker" in row["output"]


def test_registry_connect_error_is_irrelevant(
    store: Store, client: TestClient, source_repo: Path, origin_repo: Path,
) -> None:
    _push_to_origin(source_repo, origin_repo)
    branch = _make_branch(source_repo, "task-reg2", "reg2.txt", "x\n")
    sha = _git("rev-parse", branch, cwd=source_repo).stdout.strip()
    made = _task_to_review(store, repo="repo-a", head_sha=sha)

    wrapped = _RepoRegistryFails(client, raises=httpx.ConnectError("registry unreachable"))
    out, err = io.StringIO(), io.StringIO()
    code = integrate.integrate_task(wrapped, task_id=made["task_id"], keep=False,
                                    environ=dict(os.environ), out=out, err=err, backend="openshell")
    assert code == 0, (out.getvalue(), err.getvalue())
    assert store.get_run(made["run_id"])["integration"]["status"] == "passed"


# == the workflow file's test_command (design D4) ===========================

def _commit_workflow_on_main(source: Path, origin: Path, text: str) -> None:
    (source / "aos-workflow.toml").write_text(text)
    _git("add", "aos-workflow.toml", cwd=source)
    _git("commit", "-q", "-m", "workflow", cwd=source)
    _git("push", "origin", "main", cwd=source)


def test_workflow_file_test_command_is_appended(
    store: Store, client: TestClient, source_repo: Path, origin_repo: Path,
) -> None:
    _push_to_origin(source_repo, origin_repo)
    _commit_workflow_on_main(source_repo, origin_repo, 'test_command = "test -f README.md"\n')
    branch = _make_branch(source_repo, "task-wf", "wf.txt", "x\n")
    sha = _git("rev-parse", branch, cwd=source_repo).stdout.strip()
    made = _task_to_review(store, repo="repo-a", head_sha=sha, acceptance=["true"])
    code, out, err = _integrate(client, made["task_id"])
    assert code == 0, (out, err)
    assert store.get_run(made["run_id"])["integration"]["status"] == "passed"
    output = store.get_run(made["run_id"])["integration"]["output"]
    assert "$ true" in output and "$ test -f README.md" in output


def test_workflow_file_without_test_command_uses_saved_checks(
    store: Store, client: TestClient, source_repo: Path, origin_repo: Path,
) -> None:
    _push_to_origin(source_repo, origin_repo)
    _commit_workflow_on_main(source_repo, origin_repo, 'prompt = "hello"\n')
    branch = _make_branch(source_repo, "task-wf2", "wf.txt", "x\n")
    sha = _git("rev-parse", branch, cwd=source_repo).stdout.strip()
    made = _task_to_review(store, repo="repo-a", head_sha=sha, acceptance=["true"])
    code, out, err = _integrate(client, made["task_id"])
    assert code == 0, (out, err)
    assert store.get_run(made["run_id"])["integration"]["status"] == "passed"


def test_invalid_workflow_file_is_error_and_runs_no_command(
    store: Store, client: TestClient, source_repo: Path, origin_repo: Path, tmp_path: Path,
) -> None:
    _push_to_origin(source_repo, origin_repo)
    _commit_workflow_on_main(source_repo, origin_repo, 'bogus = 1\n')
    branch = _make_branch(source_repo, "task-wf3", "wf.txt", "x\n")
    sha = _git("rev-parse", branch, cwd=source_repo).stdout.strip()
    marker = tmp_path / "must-not-run"
    made = _task_to_review(store, repo="repo-a", head_sha=sha, acceptance=[f"touch {marker}"])
    code, out, err = _integrate(client, made["task_id"])
    assert code == 1
    row = store.get_run(made["run_id"])["integration"]
    assert row["status"] == "error"
    assert row["output"].startswith("workflow: aos-workflow.toml:")
    assert not marker.exists()


def test_workflow_file_only_on_task_branch_does_not_replace_saved_checks(
    store: Store, client: TestClient, source_repo: Path, origin_repo: Path, sandbox_transport,
) -> None:
    _push_to_origin(source_repo, origin_repo)
    branch = _make_branch(source_repo, "task-wf4", "aos-workflow.toml", 'test_command = "true"\n')
    sha = _git("rev-parse", branch, cwd=source_repo).stdout.strip()
    made = _task_to_review(store, repo="repo-a", head_sha=sha, acceptance=["false"])
    sandbox_transport.accept_rc = "1"
    code, out, err = _integrate(client, made["task_id"])
    assert code == 1
    assert store.get_run(made["run_id"])["integration"]["status"] == "failed"
    assert "$ false" in store.get_run(made["run_id"])["integration"]["output"]


# == A3: the fallback is a read, nothing is written to the source ===========

def _symref_present(repo: Path) -> bool:
    result = subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo,
                            capture_output=True, text=True)
    return result.returncode == 0


def test_ls_remote_fallback_resolves_base_and_leaves_no_local_symref(
    source_repo: Path, origin_repo: Path,
) -> None:
    _push_to_origin(source_repo, origin_repo)
    assert not _symref_present(source_repo)
    base_branch, base_sha = integrate.resolve_base(str(source_repo))
    assert base_branch == "main"
    assert base_sha == _git("rev-parse", "main", cwd=source_repo).stdout.strip()
    assert not _symref_present(source_repo)


def test_remove_scratch_removes_a_plain_directory(source_repo: Path, worktree_root: Path) -> None:
    scratch = worktree_root / "repo-a" / ".integrate-unused"
    scratch.mkdir(parents=True)
    (scratch / "junk.txt").write_text("not a worktree\n")
    integrate._remove_scratch(str(source_repo), str(scratch), str(worktree_root), "repo-a")
    assert not scratch.exists()


def test_remove_scratch_refuses_a_path_outside_the_integrate_pattern(tmp_path: Path) -> None:
    outside = tmp_path / "not-integrate-dir"
    outside.mkdir()
    with pytest.raises(RuntimeError):
        integrate._remove_scratch(str(tmp_path), str(outside), str(tmp_path), "repo-a")
    assert outside.exists()


# == C3: a symlink at the scratch path is unlinked, never followed =========

def test_symlink_at_scratch_path_is_unlinked_not_followed(
        source_repo: Path, worktree_root: Path, tmp_path: Path,
) -> None:
    sha = _git("rev-parse", "HEAD", cwd=source_repo).stdout.strip()

    target_worktree = tmp_path / "task-owned-worktree"
    _git("worktree", "add", "--detach", str(target_worktree), sha, cwd=source_repo)
    (target_worktree / "uncommitted.txt").write_text("not committed\n")

    scratch = worktree_root / "repo-a" / ".integrate-symlink"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.symlink_to(target_worktree, target_is_directory=True)
    assert scratch.is_symlink()

    integrate._remove_scratch(str(source_repo), str(scratch), str(worktree_root), "repo-a")
    assert not scratch.is_symlink()

    assert target_worktree.exists()
    assert (target_worktree / "uncommitted.txt").exists()
    assert (target_worktree / "uncommitted.txt").read_text() == "not committed\n"
    listing = _git("worktree", "list", "--porcelain", cwd=source_repo).stdout
    assert str(target_worktree) in listing


# == A5 (C6): a plain directory inside another repository is not a source ===

def test_directory_nested_inside_another_repo_is_not_a_git_repository(
        store: Store, client: TestClient, repos_root: Path, source_repo: Path,
) -> None:
    """`git rev-parse --git-dir` walks up to find a parent repo's .git, so a
    plain directory nested inside another repository passed the old check.
    The fix compares real paths: the source is a repository only when `git
    rev-parse --show-toplevel` names the source itself."""
    nested_repo_name = "repo-a/nested-dir"
    nested = repos_root / nested_repo_name
    nested.mkdir()
    (nested / "f.txt").write_text("x\n")
    made = _task_to_review(store, repo=nested_repo_name, head_sha="a" * 40)
    code, out, err = _integrate(client, made["task_id"])
    assert code == 1
    row = store.get_run(made["run_id"])["integration"]
    assert row["status"] == "error"
    assert row["output"] == f"{nested} is not a git repository"


# == A5: a missing source repository says so =================================

def test_missing_source_repo_dir_is_error(
    store: Store, client: TestClient, repos_root: Path,
) -> None:
    made = _task_to_review(store, repo="repo-missing", head_sha="a" * 40)
    code, out, err = _integrate(client, made["task_id"])
    assert code == 1
    row = store.get_run(made["run_id"])["integration"]
    assert row["status"] == "error"
    expected_path = str(repos_root / "repo-missing")
    assert row["output"] == f"source repository {expected_path} does not exist"
    assert out.strip() == f"integration error {made['task_id']} {'a'*8}: {row['output']}"


def test_source_not_a_git_repo_is_error(
    store: Store, client: TestClient, repos_root: Path,
) -> None:
    not_git = repos_root / "repo-notgit"
    not_git.mkdir()
    (not_git / "f.txt").write_text("x\n")
    made = _task_to_review(store, repo="repo-notgit", head_sha="a" * 40)
    code, out, err = _integrate(client, made["task_id"])
    assert code == 1
    row = store.get_run(made["run_id"])["integration"]
    assert row["status"] == "error"
    assert row["output"] == f"{not_git} is not a git repository"
    assert out.strip() == f"integration error {made['task_id']} {'a'*8}: {row['output']}"


def _review_task(store: Store, source_repo: Path, name: str, acceptance: list[str]) -> dict[str, Any]:
    branch = _make_branch(source_repo, name, f"{name}.txt", "x\n")
    sha = _git("rev-parse", branch, cwd=source_repo).stdout.strip()
    return _task_to_review(store, repo="repo-a", head_sha=sha, acceptance=acceptance)


@pytest.mark.parametrize("text, message", [
    ("[mysql]\n", "services.toml: unknown table or key mysql"),
    ('[postgres]\nimage = "postgres:15"\n',
     "services.toml: postgres.image must be pinned by an @sha256: digest"),
])
def test_invalid_services_file_posts_error_before_any_scratch_worktree(
        store, client, source_repo, docker_stub, worktree_root, text, message):
    _write_services(source_repo, text)
    made = _review_task(store, source_repo, "task-bad-services", ["true"])
    code, out, err = _integrate(client, made["task_id"])
    assert code == 1
    row = store.get_run(made["run_id"])["integration"]
    assert row["status"] == "error"
    assert row["output"] == message
    assert not (worktree_root / "repo-a").exists()
    assert str(worktree_root) not in _git("worktree", "list", "--porcelain", cwd=source_repo).stdout
    assert docker_stub.calls() == []


def test_postgres_without_docker_posts_error_before_any_scratch_worktree(
        store, client, source_repo, worktree_root):
    _write_services(source_repo)
    made = _review_task(store, source_repo, "task-no-docker", ["true"])
    code, out, err = _integrate(client, made["task_id"])
    assert code == 1
    row = store.get_run(made["run_id"])["integration"]
    assert row["status"] == "error"
    assert row["output"] == DOCKER_UNAVAILABLE
    assert not (worktree_root / "repo-a").exists()
    assert str(worktree_root) not in _git("worktree", "list", "--porcelain", cwd=source_repo).stdout


def test_unreadable_aos_directory_posts_error_before_any_scratch_worktree(
        store, client, source_repo, docker_stub, worktree_root):
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 directory")
    _write_services(source_repo)
    made = _review_task(store, source_repo, "task-unreadable-aos", ["true"])
    directory = source_repo / ".aos"
    directory.chmod(0o000)
    try:
        code, out, err = _integrate(client, made["task_id"])
    finally:
        directory.chmod(0o755)
    assert code == 1
    row = store.get_run(made["run_id"])["integration"]
    assert row["status"] == "error"
    assert row["output"].startswith("services.toml: ")
    assert "Permission denied" in row["output"]
    assert not (worktree_root / "repo-a").exists()
    assert str(worktree_root) not in _git("worktree", "list", "--porcelain", cwd=source_repo).stdout
    assert docker_stub.calls() == []


def test_saved_inherited_checks_survive_workflow_changes(
    store: Store, client: TestClient, source_repo: Path, origin_repo: Path, sandbox_transport,
) -> None:
    _push_to_origin(source_repo, origin_repo)
    branch = _make_branch(source_repo, "task-inherited", "inherited.txt", "x\n")
    sha = _git("rev-parse", branch, cwd=source_repo).stdout.strip()
    made = _task_to_review(store, repo="repo-a", head_sha=sha, acceptance=[], inherited=["false"])
    _commit_workflow_on_main(source_repo, origin_repo, 'acceptance = ["true"]\ntest_command = "true"\n')
    sandbox_transport.accept_rc = "1"
    code, out, err = _integrate(client, made["task_id"])
    assert code == 1, (out, err)
    assert store.get_run(made["run_id"])["integration"]["status"] == "failed"
    assert store.get_run(made["run_id"])["effective_acceptance"] == ["false"]


def test_current_workflow_test_command_is_exactly_deduplicated(
    store: Store, client: TestClient, source_repo: Path, origin_repo: Path, tmp_path: Path,
) -> None:
    _push_to_origin(source_repo, origin_repo)
    marker = tmp_path / "checks"
    command = f"printf x >> {marker}"
    _commit_workflow_on_main(source_repo, origin_repo, f"test_command = {command!r}\n")
    branch = _make_branch(source_repo, "task-dedup", "dedup.txt", "x\n")
    sha = _git("rev-parse", branch, cwd=source_repo).stdout.strip()
    made = _task_to_review(store, repo="repo-a", head_sha=sha, acceptance=[command, command])
    code, out, err = _integrate(client, made["task_id"])
    assert code == 0, (out, err)
    output = store.get_run(made["run_id"])["integration"]["output"]
    assert output.count(f"$ {command}\n") == 1
    assert not marker.exists()


@pytest.mark.parametrize("snapshot", [None, "[]", '[""]', '["true", 1]'])
def test_invalid_run_snapshot_refuses_commands(
    store: Store, client: TestClient, source_repo: Path, tmp_path: Path, snapshot: str | None,
) -> None:
    branch = _make_branch(source_repo, "task-invalid-snapshot", "snapshot.txt", "x\n")
    sha = _git("rev-parse", branch, cwd=source_repo).stdout.strip()
    marker = tmp_path / "must-not-run"
    made = _task_to_review(store, repo="repo-a", head_sha=sha, acceptance=[f"touch {marker}"])
    with store.connection() as db:
        db.execute("UPDATE task_runs SET effective_acceptance=? WHERE id=?", (snapshot, made["run_id"]))
    code, out, err = _integrate(client, made["task_id"])
    assert code == 1, (out, err)
    assert store.get_run(made["run_id"])["integration"]["status"] == "error"
    assert not marker.exists()


@pytest.mark.parametrize("backend", ["host", "unknown", ""])
def test_unsupported_backend_refuses_before_any_side_effect(backend, sandbox_transport, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("unsupported backend performed work")

    monkeypatch.setattr(integrate.harness, "env_from", unexpected)
    with httpx.Client(transport=httpx.MockTransport(unexpected), base_url="http://bus") as bus:
        out, err = io.StringIO(), io.StringIO()
        assert integrate.integrate_task(bus, task_id="AO-1", keep=False, environ={},
                                       out=out, err=err, backend=backend) == 2
    assert "integration requires openshell" in err.getvalue()
    assert out.getvalue() == "" and sandbox_transport.calls == []
