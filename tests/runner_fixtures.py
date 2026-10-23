"""Repository and service fixtures shared by integration and interactions tests."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repos_root = tmp_path / "repos"
    repos_root.mkdir()
    repo = repos_root / "repo-a"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)],
                   check=True, capture_output=True, text=True)
    return origin


def _set_origin(repo: Path, url: str) -> None:
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", url],
                   check=True, capture_output=True, text=True)


POLICY_TOML = """\
[execution]
fail_closed = true

[filesystem]
write = ["$WORKTREE", "$RUN_TMP"]
"""


DOCKER_UNAVAILABLE = "services: postgres requested but Docker is unavailable"


def _write_services(source_repo: Path, text: str = "[postgres]\n") -> None:
    directory = source_repo / ".aos"
    directory.mkdir(exist_ok=True)
    (directory / "services.toml").write_text(text)
