"""agent_os.gitops: the git calls and worktree-link checks the OpenShell runner, `aos integrate`, the
live sessions and the CLI share (moved out of the host runner on 2026-10-13)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_os import gitops


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _worktree_admin(worktree: Path) -> str:
    link = (worktree / ".git").read_text().strip()
    assert link.startswith("gitdir: ")
    return os.path.realpath(link[len("gitdir: "):])


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repos" / "repo-a"
    repo.mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def test_git_raises_giterror_with_capped_stderr_and_git_rc_returns_the_code(source_repo: Path) -> None:
    assert gitops.git("rev-parse", "--abbrev-ref", "HEAD", cwd=str(source_repo)).stdout.strip() in ("main", "master")
    with pytest.raises(gitops.GitError) as exc_info:
        gitops.git("rev-parse", "--verify", "refs/heads/no-such-branch", cwd=str(source_repo))
    assert exc_info.value.stderr and len(exc_info.value.stderr) <= gitops.ERROR_CAP
    assert gitops.git_rc("rev-parse", "--verify", "refs/heads/no-such-branch", cwd=str(source_repo)).returncode != 0


def test_worktree_dirty_sees_untracked_files_but_not_the_aos_directory(source_repo: Path) -> None:
    assert gitops.worktree_dirty(str(source_repo)) is False
    (source_repo / ".aos").mkdir()
    (source_repo / ".aos" / "run.json").write_text("{}")
    assert gitops.worktree_dirty(str(source_repo)) is False
    (source_repo / "new.txt").write_text("x\n")
    assert gitops.worktree_dirty(str(source_repo)) is True


def test_worktree_link_reader_messages(source_repo: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    _git("worktree", "add", str(worktree), cwd=source_repo)
    link = worktree / ".git"
    original_link = link.read_text()

    target = tmp_path / "link-target"
    target.write_text("not a git link\n")
    link.unlink()
    link.symlink_to(target)
    with pytest.raises(gitops.WorktreeIntegrityError) as exc_info:
        gitops._read_worktree_link(str(worktree))
    assert exc_info.value.message == ".git link is a symlink"
    assert exc_info.value.stderr == "worktree integrity: " + exc_info.value.message
    link.unlink()
    link.write_text(original_link)

    not_worktree = tmp_path / "not-worktree"
    not_worktree.write_text("regular file\n")
    with pytest.raises(gitops.WorktreeIntegrityError) as exc_info:
        gitops._read_worktree_link(str(not_worktree))
    assert exc_info.value.message.startswith(".git link unreadable: ")
    assert exc_info.value.stderr == "worktree integrity: " + exc_info.value.message

    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git("init", "-q", cwd=decoy)
    decoy_admin = os.path.realpath(decoy / ".git")
    (Path(decoy_admin) / "commondir").write_text(
        _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=source_repo).stdout.strip() + "\n")
    link.write_text(f"gitdir: {decoy_admin}\n")
    with pytest.raises(gitops.WorktreeIntegrityError) as exc_info:
        gitops.record_worktree_link(str(source_repo), str(worktree))
    assert exc_info.value.message == f".git link gitdir outside the source: {decoy_admin}"
    assert exc_info.value.stderr == "worktree integrity: " + exc_info.value.message
    link.write_text(original_link)

    admin = Path(_worktree_admin(worktree))
    commondir = admin / "commondir"
    original_commondir = commondir.read_text()
    commondir.unlink()
    with pytest.raises(gitops.WorktreeIntegrityError) as exc_info:
        gitops._read_worktree_link(str(worktree))
    assert exc_info.value.message.startswith("admin commondir unreadable: ")
    assert exc_info.value.stderr == "worktree integrity: " + exc_info.value.message
    commondir.write_text(original_commondir)

    other_common = tmp_path / "other-common"
    other_common.mkdir()
    commondir.write_text(str(other_common) + "\n")
    with pytest.raises(gitops.WorktreeIntegrityError) as exc_info:
        gitops.record_worktree_link(str(source_repo), str(worktree))
    assert exc_info.value.message == (
        f"admin commondir outside the source: {os.path.realpath(other_common)}")
    assert exc_info.value.stderr == "worktree integrity: " + exc_info.value.message
    commondir.write_text(original_commondir)

    not_source = tmp_path / "not-source"
    not_source.mkdir()
    with pytest.raises(gitops.WorktreeIntegrityError) as exc_info:
        gitops.record_worktree_link(str(not_source), str(worktree))
    assert exc_info.value.message.startswith("source common dir unreadable: ")
    assert exc_info.value.stderr == "worktree integrity: " + exc_info.value.message


def test_record_and_check_worktree_link_round_trip(source_repo: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    _git("worktree", "add", str(worktree), cwd=source_repo)
    recorded = gitops.record_worktree_link(str(source_repo), str(worktree))
    assert gitops.check_worktree_link(str(worktree), recorded) is None
    (worktree / ".git").write_text(f"gitdir: {tmp_path / 'elsewhere'}\n")
    assert gitops.check_worktree_link(str(worktree), recorded) == (
        f"admin commondir unreadable: [Errno 2] No such file or directory: '{tmp_path / 'elsewhere' / 'commondir'}'")
    (worktree / ".git").write_text(f"gitdir: {recorded['gitdir']}\n")
    assert gitops.check_worktree_link(str(worktree), recorded) is None
