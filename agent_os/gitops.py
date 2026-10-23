"""Git calls and worktree-link checks shared by the OpenShell runner, `aos integrate`, the live
sessions and the CLI (moved out of the retired host runner on 2026-10-13, MVP decision 6).

Every git call goes through ``policy.GIT``: no repository hook runs (core.hooksPath is the
runner's empty private directory) and the host's GIT_* variables are dropped.
"""
from __future__ import annotations

import os
import stat
import subprocess

from agent_os import policy

ERROR_CAP = 2000


class GitError(RuntimeError):
    def __init__(self, stderr: str) -> None:
        super().__init__(stderr)
        self.stderr = stderr


class WorktreeIntegrityError(GitError):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)
        self.stderr = "worktree integrity: " + message


def git(*args: str, cwd: str) -> subprocess.CompletedProcess:
    """A git call that must succeed; a failing one raises GitError with the capped stderr."""
    try:
        policy.check_hooks_dir()
        return subprocess.run([*policy.GIT, "-C", cwd, *args], check=True, capture_output=True, text=True,
                              env=policy.git_env(os.environ))
    except policy.PolicyError as exc:
        raise GitError(str(exc)[:ERROR_CAP]) from exc
    except subprocess.CalledProcessError as exc:
        raise GitError((exc.stderr or str(exc))[:ERROR_CAP]) from exc


def git_rc(*args: str, cwd: str) -> subprocess.CompletedProcess:
    """A git call whose exit code the caller inspects (same hooks and environment rules)."""
    policy.check_hooks_dir()
    return subprocess.run([*policy.GIT, "-C", cwd, *args], capture_output=True, text=True,
                          env=policy.git_env(os.environ))


def worktree_dirty(path: str, git_dir: str | None = None) -> bool:
    try:
        return bool(policy.git_output(path, "status", "--porcelain", "--untracked-files=all", "--", ".",
                                      ":(exclude).aos", git_dir=git_dir)[1].strip())
    except policy.PolicyError as exc:
        raise RuntimeError(str(exc)) from exc


# -- worktree link integrity -----------------------------------------------------------

def _read_worktree_link(worktree: str) -> tuple[str, str]:
    link = os.path.join(worktree, ".git")
    try:
        mode = os.lstat(link).st_mode
        if stat.S_ISLNK(mode): raise WorktreeIntegrityError(".git link is a symlink")
        if stat.S_ISDIR(mode): raise WorktreeIntegrityError(".git link is a directory")
        if not stat.S_ISREG(mode): raise WorktreeIntegrityError(".git link unreadable: not a regular file")
        lines = open(link).read().splitlines()
    except FileNotFoundError as exc:
        raise WorktreeIntegrityError(".git link missing") from exc
    except WorktreeIntegrityError:
        raise
    except OSError as exc:
        raise WorktreeIntegrityError(f".git link unreadable: {exc}") from exc
    if len(lines) != 1 or not lines[0].startswith("gitdir: "):
        raise WorktreeIntegrityError(".git link unreadable: missing gitdir: prefix")
    target = lines[0][len("gitdir: "):]
    gitdir = os.path.realpath(target if os.path.isabs(target) else os.path.join(worktree, target))
    if os.path.exists(os.path.join(gitdir, "config.worktree")):
        raise WorktreeIntegrityError("admin config.worktree present")
    try:
        commondir = os.path.realpath(os.path.join(gitdir, open(os.path.join(gitdir, "commondir")).read().strip()))
    except OSError as exc:
        raise WorktreeIntegrityError(f"admin commondir unreadable: {exc}") from exc
    return gitdir, commondir


def record_worktree_link(source: str, worktree: str) -> dict[str, str]:
    try:
        common = os.path.realpath(policy.git_output(
            source, "rev-parse", "--path-format=absolute", "--git-common-dir")[1].decode().strip())
    except policy.PolicyError as exc:
        raise WorktreeIntegrityError(f"source common dir unreadable: {exc}") from exc
    gitdir, commondir = _read_worktree_link(worktree)
    if os.path.dirname(gitdir) != os.path.join(common, "worktrees"):
        raise WorktreeIntegrityError(f".git link gitdir outside the source: {gitdir}")
    if commondir != common:
        raise WorktreeIntegrityError(f"admin commondir outside the source: {commondir}")
    return {"gitdir": gitdir, "commondir": commondir}


def check_worktree_link(worktree: str, recorded: dict) -> str | None:
    try:
        gitdir, commondir = _read_worktree_link(worktree)
    except WorktreeIntegrityError as exc:
        return exc.message
    if gitdir != recorded["gitdir"]:
        return f".git link gitdir changed: {recorded['gitdir']} -> {gitdir}"
    if commondir != recorded["commondir"]:
        return f"admin commondir changed: {recorded['commondir']} -> {commondir}"
    return None
