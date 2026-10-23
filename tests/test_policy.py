from __future__ import annotations

import hashlib
import os
import subprocess
import time
import zlib
from pathlib import Path

import pytest

import agent_os.policy as policy_module
from agent_os.policy import (
    BASELINE_DENY_READ,
    Default,
    GIT,
    PolicyError,
    branch_changes_policy,
    bus_host,
    git_output,
    read_policy,
    reason_line,
    resolve_default,
    resolve_tip,
)


VALID = '''\
[execution]
fail_closed = true
[filesystem]
write = ["$WORKTREE", "$RUN_TMP"]
'''


def _env(home: Path) -> dict[str, str]:
    return {**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com"}


def _git(*args: str, cwd: Path, home: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                            capture_output=True, env=_env(home or cwd / ".home"))
    return result.stdout.strip()


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    repo = tmp_path / name
    repo.mkdir()
    _git("init", "-q", cwd=repo, home=home)
    _git("config", "user.email", "test@example.com", cwd=repo, home=home)
    _git("config", "user.name", "Test", cwd=repo, home=home)
    _git("config", "gc.auto", "0", cwd=repo, home=home)
    (repo / "README").write_text("base\n")
    _git("add", "README", cwd=repo, home=home)
    _git("commit", "-q", "-m", "base", cwd=repo, home=home)
    return repo


def _commit(repo: Path, message: str = "policy", *, text: str = VALID) -> str:
    (repo / "aos-policy.toml").write_text(text)
    _git("add", "aos-policy.toml", cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


def _default(repo: Path) -> Default:
    sha = _git("rev-parse", "HEAD", cwd=repo)
    branch = _git("branch", "--show-current", cwd=repo)
    return Default("local", branch, sha, f"{branch}@{sha[:12]}")


def _read(repo: Path, home: Path):
    return read_policy(str(repo), _default(repo), str(home))


def test_policy_validation_floor_and_exact_types(tmp_path):
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    _commit(repo, text=VALID + '''\
deny_read = ["secrets", "~/.ssh", "secrets"]
[network]
allow = ["example.com", "example.com"]
''')
    policy = _read(repo, home)
    assert policy is not None
    assert policy.write == ("$WORKTREE", "$RUN_TMP")
    assert policy.deny_read == (*BASELINE_DENY_READ, "secrets")
    assert policy.network_allow == ("example.com",)
    for text in (
        VALID.replace("true", "1"),
        VALID.replace("[filesystem]", "[filesystem]\ncache = []"),
        VALID.replace("$RUN_TMP", "$OTHER"),
        "[filesystem]\nwrite = [\"$WORKTREE\", \"$RUN_TMP\"]\n",
    ):
        _commit(repo, "bad", text=text)
        with pytest.raises(PolicyError, match=r"policy: aos-policy.toml:"):
            _read(repo, home)


@pytest.mark.parametrize("entry", ["~/.cache", "/tmp/x", "a/../b", "a//b", "a/", "./x", "~", "$OTHER"])
def test_invalid_write_entries_refuse(tmp_path, entry):
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    _commit(repo, text=VALID.replace('"$RUN_TMP"', f'"$RUN_TMP", "{entry}"'))
    with pytest.raises(PolicyError, match=r"policy: aos-policy.toml:"):
        _read(repo, home)


@pytest.mark.parametrize("entry", ["secrets/", "a//b", "./x", "/secret", "$WORKTREE", "~"])
def test_invalid_deny_read_entries_refuse(tmp_path, entry):
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    _commit(repo, text=VALID + f'deny_read = ["{entry}"]\n')
    with pytest.raises(PolicyError, match=r"policy: aos-policy.toml:"):
        _read(repo, home)


def test_cache_write_resolves_once_and_must_stay_under_an_approved_root(tmp_path):
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    (home / ".cache").mkdir()
    (home / ".npm").mkdir()
    (home / "Library/Caches").mkdir(parents=True)
    _commit(repo, text=VALID.replace('"$RUN_TMP"', '"$RUN_TMP", "~/.cache/uv"'))
    policy = _read(repo, home)
    assert policy is not None and policy.write[-1] == os.path.realpath(home / ".cache/uv")
    (home / ".cache/link").symlink_to(home / ".npm", target_is_directory=True)
    _commit(repo, "linked", text=VALID.replace('"$RUN_TMP"', '"$RUN_TMP", "~/.cache/link/ok"'))
    assert _read(repo, home).write[-1] == os.path.realpath(home / ".npm/ok")
    (home / ".cache/escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    _commit(repo, "escape", text=VALID.replace('"$RUN_TMP"', '"$RUN_TMP", "~/.cache/escape/no"'))
    with pytest.raises(PolicyError, match="write entry"):
        _read(repo, home)


@pytest.mark.parametrize("entry", ["example.com", "*.example.com", "host-1:443", "127.0.0.1:8010"])
def test_network_allow_grammar_accepts_valid_entries(tmp_path, entry):
    repo = _repo(tmp_path)
    _commit(repo, text=VALID + f"[network]\nallow = [\"{entry}\"]\n")
    assert _read(repo, tmp_path / "home").network_allow == (entry,)


@pytest.mark.parametrize("entry", ["https://pypi.org", "host/path", "user@host", "host:0", "host:65536"])
def test_network_allow_grammar_refuses_invalid_entries(tmp_path, entry):
    repo = _repo(tmp_path)
    _commit(repo, text=VALID + f"[network]\nallow = [\"{entry}\"]\n")
    with pytest.raises(PolicyError, match="network.allow entry"):
        _read(repo, tmp_path / "home")


def test_non_utf8_and_hash_mismatch_refuse(tmp_path):
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    (repo / "aos-policy.toml").write_bytes(VALID.encode() + b"\xff")
    _git("add", "aos-policy.toml", cwd=repo)
    _git("commit", "-q", "-m", "non-utf8", cwd=repo)
    with pytest.raises(PolicyError, match="aos-policy.toml"):
        _read(repo, home)
    _commit(repo, "valid")
    blob = _git("rev-parse", "HEAD:aos-policy.toml", cwd=repo)
    object_path = repo / ".git" / "objects" / blob[:2] / blob[2:]
    object_path.chmod(0o600)
    object_path.write_bytes(zlib.compress(b"blob 6\0forged"))
    with pytest.raises(PolicyError, match="object mismatch"):
        _read(repo, home)


def test_read_policy_absent_regular_file_and_tree_integrity(tmp_path):
    repo = _repo(tmp_path)
    assert _read(repo, tmp_path / "home") is None
    (repo / "aos-policy.toml").symlink_to("README")
    _git("add", "aos-policy.toml", cwd=repo)
    _git("commit", "-q", "-m", "symlink", cwd=repo)
    with pytest.raises(PolicyError, match="regular file"):
        _read(repo, tmp_path / "home")
    (repo / "aos-policy.toml").unlink()
    _commit(repo, "policy")
    tree = _git("rev-parse", "HEAD^{tree}", cwd=repo)
    _git("rm", "-q", "aos-policy.toml", cwd=repo)
    _git("commit", "-q", "-m", "other tree", cwd=repo)
    other_tree = _git("rev-parse", "HEAD^{tree}", cwd=repo)
    target = repo / ".git/objects" / tree[:2] / tree[2:]
    source = repo / ".git/objects" / other_tree[:2] / other_tree[2:]
    target.chmod(0o600)
    target.write_bytes(source.read_bytes())
    with pytest.raises(PolicyError):
        read_policy(str(repo), Default("local", "main", _git("rev-parse", "HEAD~", cwd=repo), "x"), str(tmp_path / "home"))


def test_network_local_binding_parses_and_defaults_false(tmp_path):
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    _commit(repo, text=VALID + "[network]\nlocal_binding = true\n")
    enabled = _read(repo, home)
    assert enabled is not None and enabled.local_binding is True

    _commit(repo, "default", text=VALID)
    default = _read(repo, home)
    assert default is not None and default.local_binding is False


@pytest.mark.parametrize("value", ["1", '"true"', "[]"])
def test_network_local_binding_refuses_non_booleans(tmp_path, value):
    repo = _repo(tmp_path)
    _commit(repo, text=VALID + f"[network]\nlocal_binding = {value}\n")
    with pytest.raises(PolicyError) as excinfo:
        _read(repo, tmp_path / "home")
    assert str(excinfo.value) == "policy: aos-policy.toml: network.local_binding must be true or false"


def test_network_unknown_key_still_refuses_with_local_binding(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, text=VALID + "[network]\nlocal_binding = true\nother = false\n")
    with pytest.raises(PolicyError) as excinfo:
        _read(repo, tmp_path / "home")
    assert str(excinfo.value) == "policy: aos-policy.toml: unknown key network.other"


@pytest.mark.parametrize(("url", "expected"), [
    ("http://127.0.0.1:8010", "127.0.0.1:8010"),
    ("http://203.0.113.10:8010", "203.0.113.10:8010"),
    ("http://agent-os:8010", "agent-os:8010"),
    ("http://bus.example.com", "bus.example.com"),
    ("http://[::1]:8010", "[::1]:8010"),
    ("http://user@host:8010/path", "host:8010"),
])
def test_bus_host(url, expected):
    assert bus_host(url) == expected
    with pytest.raises(PolicyError):
        bus_host("/only-a-path")


def test_reason_line_filters_banners_and_is_bounded():
    assert reason_line(b"remote: welcome\n** banner\nssh: bad host\nfatal: later\n", "fetch", 128) == "ssh: bad host"
    assert reason_line("Warning: hello\r\nfatal: useful\r\n", "fetch", 128) == "fatal: useful"
    assert reason_line(" \n\thint: use this\n", "fetch", 128) == "git fetch exited 128"
    assert len(reason_line("x" * 300, "fetch", 1)) == 200


def _origin_repo(tmp_path: Path) -> tuple[Path, Path]:
    source = _repo(tmp_path, "source")
    bare = tmp_path / "origin.git"
    _git("init", "--bare", "-q", str(bare), cwd=tmp_path)
    _git("remote", "add", "origin", str(bare), cwd=source)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=source)
    _git("-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main", cwd=source)
    return source, bare


def test_resolve_default_remote_local_and_integrity(tmp_path, monkeypatch):
    source, bare = _origin_repo(tmp_path)
    default = resolve_default(str(source))
    assert default.state == "available" and default.ref.startswith("origin/main@")
    assert not (source / ".git/FETCH_HEAD").exists()
    assert resolve_default(str(_repo(tmp_path, "local"))).state == "local"
    empty = tmp_path / "empty"
    empty.mkdir(); _git("init", "-q", cwd=empty)
    assert resolve_default(str(empty)).state == "empty"
    for bad in (bare, source / ".git", source / "sub"):
        if bad.name == "sub": bad.mkdir()
        with pytest.raises(PolicyError, match="policy source must be a main checkout"):
            resolve_default(str(bad))
    monkeypatch.setenv("GIT_DIR", str(bare))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "remote.origin.url=/not-used")
    assert resolve_default(str(source)).state == "available"


def test_resolve_default_remote_head_states_and_local_variants(tmp_path):
    source, bare = _origin_repo(tmp_path)
    main = _git("rev-parse", "HEAD", cwd=source)
    _git("-C", str(bare), "update-ref", "--no-deref", "HEAD", main, cwd=source)
    with pytest.raises(PolicyError, match="remote HEAD is detached"):
        resolve_default(str(source))
    _git("-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main", cwd=source)
    _git("tag", "-a", "v1", "-m", "v1", cwd=source)
    _git("push", "-q", "origin", "refs/tags/v1", cwd=source)
    _git("-C", str(bare), "symbolic-ref", "HEAD", "refs/tags/v1", cwd=source)
    with pytest.raises(PolicyError, match="remote HEAD is not a branch"):
        resolve_default(str(source))
    local = _repo(tmp_path, "local")
    _git("remote", "add", "origin", "", cwd=local)
    assert resolve_default(str(local)).state == "local"
    _git("remote", "remove", "origin", cwd=local)
    _git("remote", "add", "origin", "x", cwd=local)
    _git("remote", "set-url", "--push", "origin", "x", cwd=local)
    _git("config", "--unset", "remote.origin.url", cwd=local)
    assert resolve_default(str(local)).state == "local"


def test_resolve_default_empty_and_agent_remote_states(tmp_path):
    source, bare = _origin_repo(tmp_path)
    _git("push", "-q", "origin", "HEAD:refs/heads/ao/x", cwd=source)
    _git("-C", str(bare), "update-ref", "-d", "refs/heads/main", cwd=source)
    _git("-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/ao/x", cwd=source)
    assert resolve_default(str(source)).state == "empty"
    _git("push", "-q", "origin", "HEAD:refs/heads/review", cwd=source)
    with pytest.raises(PolicyError, match="remote default branch is an agent branch ao/x"):
        resolve_default(str(source))
    _git("-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/missing", cwd=source)
    with pytest.raises(PolicyError, match="remote HEAD names no branch"):
        resolve_default(str(source))
    local = _repo(tmp_path, "detached")
    _git("checkout", "-q", "--detach", cwd=local)
    assert resolve_default(str(local)).branch == "HEAD"


def test_git_output_ok_codes_and_environment_stripping(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    rc, output = git_output(str(repo), "config", "--get", "remote.origin.url", ok=(0, 1))
    assert rc == 1 and output == b""
    with pytest.raises(PolicyError):
        git_output(str(repo), "rev-parse", "does-not-exist")
    monkeypatch.setenv("GIT_TRACE", "1")
    with pytest.raises(PolicyError) as exc:
        git_output(str(repo), "rev-parse", "does-not-exist")
    assert not str(exc.value).startswith("trace:")


def test_git_env_strips_git_trace_pathspec_and_aos_variables():
    env = policy_module.git_env({
        "GIT_DIR": "/decoy", "GIT_TRACE": "1", "GIT_LITERAL_PATHSPECS": "1",
        "AOS_TOKEN": "secret", "HOME": "/home/test", "PATH": "/bin",
    })
    assert "GIT_DIR" not in env
    assert "GIT_TRACE" not in env
    assert "GIT_LITERAL_PATHSPECS" not in env
    assert "AOS_TOKEN" not in env
    assert env["HOME"] == "/home/test"
    assert env["PATH"] == "/bin"
    assert env["GIT_GRAFT_FILE"] == os.devnull
    assert env["GIT_SHALLOW_FILE"] == os.devnull


def test_git_output_git_dir_uses_the_pinned_admin_directory_for_a_relinked_worktree(tmp_path):
    source = _repo(tmp_path, "source")
    worktree = tmp_path / "worktree"
    _git("worktree", "add", "-q", "-b", "work", str(worktree), cwd=source)
    true_tip = _commit(worktree, "worktree tip")
    admin_dir = _git("rev-parse", "--path-format=absolute", "--git-dir", cwd=worktree)
    decoy = _repo(tmp_path, "decoy")
    decoy_tip = _git("rev-parse", "HEAD", cwd=decoy)
    (worktree / ".git").write_text(f"gitdir: {decoy / '.git'}\n")

    _, pinned = git_output(str(worktree), "rev-parse", "HEAD", git_dir=admin_dir)
    _, redirected = git_output(str(worktree), "rev-parse", "HEAD")

    assert pinned.decode().strip() == true_tip
    assert redirected.decode().strip() == decoy_tip


def test_git_output_ignores_a_shallow_file(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "second")
    (repo / ".git/shallow").write_text(_git("rev-parse", "HEAD", cwd=repo) + "\n")

    _, output = git_output(str(repo), "log", "--format=%H")

    assert len(output.splitlines()) == 2


def test_git_output_disables_embedded_repository_fsmonitor(tmp_path):
    repo = _repo(tmp_path)
    sub = repo / "sub"
    sub.mkdir()
    _git("init", "-q", cwd=sub)
    _git("config", "user.email", "test@example.com", cwd=sub)
    _git("config", "user.name", "Test", cwd=sub)
    marker = tmp_path / "fsmonitor-ran"
    _git("config", "core.fsmonitor", f"sh -c 'echo ran > {marker}'", cwd=sub)
    (sub / "tracked").write_text("tracked\n")
    _git("add", "tracked", cwd=sub)
    _git("commit", "-q", "-m", "tracked", cwd=sub)
    _git("add", "sub", cwd=repo)
    _git("commit", "-q", "-m", "embedded repository", cwd=repo)
    marker.unlink(missing_ok=True)

    git_output(str(repo), "status", "--porcelain")

    assert not marker.exists()
    assert ("-c", "core.fsmonitor=false") in zip(GIT, GIT[1:])  # core.hooksPath follows it


def test_git_argv_points_hooks_path_at_an_empty_private_directory(tmp_path):
    """Directive 2026-10-13 U2: no repository hook runs under the runner's git."""
    pairs = list(zip(GIT, GIT[1:]))
    value = next(v for flag, v in pairs if flag == "-c" and v.startswith("core.hooksPath="))
    hooks_dir = value[len("core.hooksPath="):]
    assert hooks_dir == policy_module.NO_HOOKS_DIR
    assert os.path.isdir(hooks_dir) and not os.listdir(hooks_dir)
    assert os.stat(hooks_dir).st_mode & 0o777 == 0o700
    # A repository hook (pre-commit here) does not run under git_output.
    repo = _repo(tmp_path)
    marker = tmp_path / "hook-ran"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho ran > {marker}\nexit 1\n")
    hook.chmod(0o755)
    (repo / "f").write_text("f\n")
    git_output(str(repo), "add", "f")
    git_output(str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "no hook")
    assert not marker.exists()


def test_check_hooks_dir_refuses_a_planted_hook(tmp_path):
    planted = Path(policy_module.NO_HOOKS_DIR) / "pre-push"
    planted.write_text("#!/bin/sh\nexit 1\n")
    try:
        with pytest.raises(PolicyError, match="is not empty \\(pre-push\\)"):
            policy_module.check_hooks_dir()
        repo = _repo(tmp_path)
        with pytest.raises(PolicyError, match="is not empty"):
            git_output(str(repo), "status", "--porcelain")
    finally:
        planted.unlink()
    policy_module.check_hooks_dir()


def test_git_output_strips_aos_token_from_embedded_filter(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    sub = repo / "sub"
    sub.mkdir()
    _git("init", "-q", cwd=sub)
    _git("config", "user.email", "test@example.com", cwd=sub)
    _git("config", "user.name", "Test", cwd=sub)
    marker = tmp_path / "filter-ran"
    _git("config", "filter.x.clean", f"sh -c 'echo TOKEN=[$AOS_TOKEN] >> {marker}; cat'", cwd=sub)
    attributes = sub / ".git/info/attributes"
    attributes.write_text("* filter=x\n")
    tracked = sub / "tracked"
    tracked.write_text("same\n")
    _git("-c", "filter.x.clean=cat", "add", "tracked", cwd=sub)
    _git("-c", "filter.x.clean=cat", "commit", "-q", "-m", "tracked", cwd=sub)
    _git("add", "sub", cwd=repo)
    _git("commit", "-q", "-m", "embedded repository", cwd=repo)
    marker.unlink(missing_ok=True)
    future = time.time() + 5
    os.utime(tracked, (future, future))
    monkeypatch.setenv("AOS_TOKEN", "secret")

    git_output(str(repo), "status", "--porcelain")

    assert set(marker.read_text().splitlines()) == {"TOKEN=[]"}


def test_resolve_tip_and_policy_diffs_are_branch_and_history_based(tmp_path):
    repo = _repo(tmp_path)
    base = _git("rev-parse", "HEAD", cwd=repo)
    _git("checkout", "-q", "-b", "ao/T", cwd=repo)
    _commit(repo)
    tip = resolve_tip(str(repo), "ao/T")
    assert branch_changes_policy(str(repo), base, tip)
    _git("tag", "-f", "ao/T", base, cwd=repo)
    assert resolve_tip(str(repo), "ao/T") == tip
    with pytest.raises(PolicyError):
        resolve_tip(str(repo), "missing")
    _git("replace", tip, base, cwd=repo)
    assert branch_changes_policy(str(repo), base, tip)


@pytest.mark.parametrize("move", ["forced", "fast_forward"])
def test_resolve_default_handles_branch_move_races(tmp_path, monkeypatch, move):
    source = _repo(tmp_path, "source")
    bare = tmp_path / "origin.git"
    _git("init", "--bare", "-q", str(bare), cwd=tmp_path)
    if move == "forced":
        announced = _repo(tmp_path, "announced")
        (announced / "announced").write_text("old snapshot\n")
        _git("add", "announced", cwd=announced)
        _git("commit", "-q", "-m", "announced", cwd=announced)
        _git("push", "-q", str(bare), "HEAD:refs/heads/main", cwd=announced)
        successor = _repo(tmp_path, "successor")
        _git("push", "-q", str(bare), "HEAD:refs/heads/future", cwd=successor)
        announced_sha = _git("rev-parse", "HEAD", cwd=announced)
        successor_sha = _git("rev-parse", "HEAD", cwd=successor)
    else:
        announced_sha = _git("rev-parse", "HEAD", cwd=source)
        (source / "fast-forward").write_text("new\n")
        _git("add", "fast-forward", cwd=source)
        _git("commit", "-q", "-m", "fast forward", cwd=source)
        successor_sha = _git("rev-parse", "HEAD", cwd=source)
        _git("push", "-q", str(bare), f"{successor_sha}:refs/heads/future", cwd=source)
        _git("-C", str(bare), "update-ref", "refs/heads/main", announced_sha, cwd=source)
    _git("-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main", cwd=source)
    _git("remote", "add", "origin", str(bare), cwd=source)

    original = policy_module.git_output
    moved = False

    def race(repo, *args, **kwargs):
        nonlocal moved
        result = original(repo, *args, **kwargs)
        if not moved and args[0] == "ls-remote":
            moved = True
            _git("-C", str(bare), "update-ref", "refs/heads/main", successor_sha, cwd=source)
        return result

    monkeypatch.setattr(policy_module, "git_output", race)
    if move == "forced":
        with pytest.raises(PolicyError, match=r"^policy: default branch unavailable:"):
            resolve_default(str(source))
    else:
        default = resolve_default(str(source))
        assert default.state == "available" and default.sha == announced_sha


def test_resolve_default_fetch_does_not_write_refs_or_tags(tmp_path):
    source, bare = _origin_repo(tmp_path)
    branch_sha = _git("rev-parse", "HEAD", cwd=source)
    _git("update-ref", "-d", "refs/remotes/origin/main", cwd=source)
    tagger = _repo(tmp_path, "tagger")
    _git("remote", "add", "origin", str(bare), cwd=tagger)
    _git("push", "-q", "origin", "HEAD:refs/heads/tag-source", cwd=tagger)
    _git("tag", "-a", "main", "-m", "main tag", cwd=tagger)
    _git("push", "-q", "origin", "refs/tags/main", cwd=tagger)
    _git("config", "remote.origin.tagOpt", "--tags", cwd=source)

    assert not (source / ".git/refs/remotes/origin/main").exists()
    default = resolve_default(str(source))

    tags = source / ".git/refs/tags"
    assert default.state == "available" and default.sha == branch_sha
    assert not (source / ".git/refs/remotes/origin/main").exists()
    assert not (source / ".git/FETCH_HEAD").exists()
    assert not tags.exists() or not any(tags.rglob("*"))


def test_resolve_default_treats_pushurl_only_and_empty_url_as_local(tmp_path):
    pushurl_only = _repo(tmp_path, "pushurl-only")
    _git("remote", "add", "origin", "fetch", cwd=pushurl_only)
    _git("remote", "set-url", "--push", "origin", "push", cwd=pushurl_only)
    _git("config", "--unset", "remote.origin.url", cwd=pushurl_only)
    assert resolve_default(str(pushurl_only)).state == "local"

    empty_url = _repo(tmp_path, "empty-url")
    _git("remote", "add", "origin", "fetch", cwd=empty_url)
    _git("config", "remote.origin.url", "", cwd=empty_url)
    assert resolve_default(str(empty_url)).state == "local"


def test_resolve_default_refuses_redirected_bare_and_nonroot_sources(tmp_path):
    message = ("policy: source git directory is redirected, bare, a linked worktree, or not a "
               "repository root; the policy source must be a main checkout's root")

    redirected = _repo(tmp_path, "redirected")
    other = _repo(tmp_path, "other")
    (redirected / ".git/commondir").write_text(str(other / ".git") + "\n")
    with pytest.raises(PolicyError, match="^" + message + "$"):
        resolve_default(str(redirected))

    nested = _repo(tmp_path, "nested")
    (nested / "subdir").mkdir()
    with pytest.raises(PolicyError, match="^" + message + "$"):
        resolve_default(str(nested / "subdir"))

    bare = tmp_path / "bare.git"
    _git("init", "--bare", "-q", str(bare), cwd=tmp_path)
    with pytest.raises(PolicyError, match="^" + message + "$"):
        resolve_default(str(bare))

    git_dir = _repo(tmp_path, "git-dir") / ".git"
    with pytest.raises(PolicyError, match="^" + message + "$"):
        resolve_default(str(git_dir))


def test_resolve_default_accepts_differently_cased_checkout_path(tmp_path):
    upper = _repo(tmp_path, "CaseRepo")
    lower = tmp_path / "caserepo"
    try:
        lower.mkdir()
    except FileExistsError:
        pass
    if os.path.samefile(upper, lower) is False:
        pytest.skip("filesystem is case-sensitive")
    assert resolve_default(str(lower)).state == "local"


def test_resolve_default_ignores_nonbare_remote_extra_head_refs(tmp_path):
    origin = _repo(tmp_path, "origin")
    source = _repo(tmp_path, "source")
    main = _git("rev-parse", "HEAD", cwd=origin)
    _git("branch", "x/HEAD", cwd=origin)
    _git("update-ref", "refs/remotes/origin/HEAD", main, cwd=origin)
    _git("remote", "add", "origin", str(origin), cwd=source)

    default = resolve_default(str(source))

    assert default.state == "available" and default.sha == main


def test_branch_changes_policy_ignores_grafts_commit_graph_and_shallow(tmp_path):
    repo = _repo(tmp_path)
    base = _git("rev-parse", "HEAD", cwd=repo)
    _git("checkout", "-q", "-b", "ao/T", cwd=repo)
    tip = _commit(repo)
    grafts = repo / ".git/info/grafts"
    grafts.parent.mkdir(exist_ok=True)
    grafts.write_text(tip + "\n")
    assert branch_changes_policy(str(repo), base, tip)
    grafts.unlink()
    _git("commit-graph", "write", "--reachable", cwd=repo)
    assert branch_changes_policy(str(repo), base, tip)
    (repo / ".git/shallow").write_text(tip + "\n")
    assert branch_changes_policy(str(repo), base, tip)


def test_branch_changes_policy_sees_earlier_renamed_and_not_other_changes(tmp_path):
    repo = _repo(tmp_path)
    base = _commit(repo, "base policy")

    _git("checkout", "-q", "-b", "ao/earlier", cwd=repo)
    _commit(repo, "policy first", text=VALID + '[network]\nallow = ["example.com"]\n')
    (repo / "later").write_text("later\n")
    _git("add", "later", cwd=repo)
    _git("commit", "-q", "-m", "unrelated second", cwd=repo)
    assert branch_changes_policy(str(repo), base, _git("rev-parse", "HEAD", cwd=repo))

    _git("checkout", "-q", "-b", "ao/rename", base, cwd=repo)
    _git("mv", "aos-policy.toml", "renamed-policy.toml", cwd=repo)
    _git("commit", "-q", "-m", "rename policy", cwd=repo)
    assert branch_changes_policy(str(repo), base, _git("rev-parse", "HEAD", cwd=repo))

    _git("checkout", "-q", "-b", "ao/other", base, cwd=repo)
    (repo / "only-other").write_text("other\n")
    _git("add", "only-other", cwd=repo)
    _git("commit", "-q", "-m", "other only", cwd=repo)
    assert not branch_changes_policy(str(repo), base, _git("rev-parse", "HEAD", cwd=repo))


def test_git_output_strips_trace_and_literal_pathspec_environment(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("GIT_TRACE", "1")
    with pytest.raises(PolicyError) as exc:
        git_output(str(repo), "ls-remote", "/nope")
    assert str(exc.value) == "fatal: '/nope' does not appear to be a git repository"

    _git("checkout", "-q", "-b", "ao/T", cwd=repo)
    (repo / ".AOS").mkdir()
    (repo / ".AOS/f").write_text("found\n")
    _git("add", ".AOS/f", cwd=repo)
    _git("commit", "-q", "-m", "upper case aos", cwd=repo)
    tip = _git("rev-parse", "HEAD", cwd=repo)
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "1")
    _, output = git_output(str(repo), "log", "--format=%h", "--full-history", "HEAD", tip,
                           "--", ":(icase).aos")
    assert output.strip()


def test_reason_line_keeps_first_fatal_and_skips_banners():
    assert reason_line("fatal: '/nope' does not appear to be a git repository\n"
                       "fatal: Could not read from remote repository.\n", "ls-remote", 128) == (
                           "fatal: '/nope' does not appear to be a git repository")
    assert reason_line("** WARNING: connection warning\nremote: Welcome\nfatal: useful\n",
                       "ls-remote", 128) == "fatal: useful"
    assert reason_line("hint: a\nhint: b\n", "ls-remote", 128) == "git ls-remote exited 128"


def test_read_policy_refuses_a_rewritten_tree_with_git_hash_reason(tmp_path):
    repo = _repo(tmp_path)
    policy_sha = _commit(repo)
    tree = _git("rev-parse", f"{policy_sha}^{{tree}}", cwd=repo)
    (repo / "other").write_text("other\n")
    _git("add", "other", cwd=repo)
    _git("commit", "-q", "-m", "other tree", cwd=repo)
    other_tree = _git("rev-parse", "HEAD^{tree}", cwd=repo)
    target = repo / ".git/objects" / tree[:2] / tree[2:]
    replacement = repo / ".git/objects" / other_tree[:2] / other_tree[2:]
    target.chmod(0o600)
    target.write_bytes(replacement.read_bytes())

    with pytest.raises(PolicyError, match=r"^policy: error: hash mismatch"):
        read_policy(str(repo), Default("local", "main", policy_sha, "x"), str(tmp_path / "home"))


def test_resolve_tip_prefers_branch_and_refuses_a_worktree_path_file(tmp_path):
    repo = _repo(tmp_path)
    base = _git("rev-parse", "HEAD", cwd=repo)
    _git("checkout", "-q", "-b", "ao/T", cwd=repo)
    (repo / "branch-file").write_text("tip\n")
    _git("add", "branch-file", cwd=repo)
    _git("commit", "-q", "-m", "tip", cwd=repo)
    tip = _git("rev-parse", "HEAD", cwd=repo)
    _git("tag", "ao/T", base, cwd=repo)
    assert resolve_tip(str(repo), "ao/T") == tip
    _git("checkout", "-q", base, cwd=repo)
    _git("branch", "-D", "ao/T", cwd=repo)
    path_file = repo / "refs/heads/ao/T"
    path_file.parent.mkdir(parents=True)
    path_file.write_text("not a ref\n")
    with pytest.raises(PolicyError):
        resolve_tip(str(repo), "ao/T")


def test_cache_write_uses_cross_root_realpath_and_refuses_escape(tmp_path):
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    (home / ".cache").mkdir()
    (home / ".npm/uv-real").mkdir(parents=True)
    (home / "Library/Caches").mkdir(parents=True)
    (home / ".cache/uv").symlink_to(home / ".npm/uv-real", target_is_directory=True)
    _commit(repo, text=VALID.replace('"$RUN_TMP"', '"$RUN_TMP", "~/.cache/uv"'))
    policy = _read(repo, home)
    realpath = os.path.realpath(home / ".npm/uv-real")
    assert policy.write[-1] == realpath

    (home / ".cache/escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    _commit(repo, "escape", text=VALID.replace('"$RUN_TMP"', '"$RUN_TMP", "~/.cache/escape"'))
    with pytest.raises(PolicyError, match="outside approved cache roots"):
        _read(repo, home)


