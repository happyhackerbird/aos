"""Repository policy parsing and the fail-closed git reads that feed it."""
from __future__ import annotations

import atexit
import hashlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _no_hooks_dir() -> str:
    """An empty private directory (mode 0700, this process only), handed to every runner git call as
    core.hooksPath so no repository hook runs under the runner's own git. AO-4 r2 (2026-10-12): the
    target's pre-push hook committed into the task branch and rewrote the source clone's .git/config."""
    path = tempfile.mkdtemp(prefix="aos-no-hooks-")
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


NO_HOOKS_DIR = _no_hooks_dir()
GIT = ("git", "--no-replace-objects", "-c", "core.commitGraph=false", "-c", "advice.graftFileDeprecated=false",
       "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={NO_HOOKS_DIR}")
GIT_TIMEOUT = 60
APPROVED_CACHE_ROOTS = ("~/.cache", "~/.npm", "~/Library/Caches")
BASELINE_DENY_READ = ("~/.ssh", "~/.aws", "~/.config/gh", "~/.claude", "~/.aos/claude-oauth-token", "~/.aos/claude-settings.json", "~/.codex")
POLICY_PATH = "aos-policy.toml"
GIT_LOCAL_ENV = frozenset(("GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_INTERNAL_SUPER_PREFIX", "GIT_SHALLOW_FILE", "GIT_COMMON_DIR"))
GIT_PATHSPEC_ENV = frozenset(("GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"))
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Default:
    state: str
    branch: str
    sha: str
    ref: str


@dataclass(frozen=True)
class Policy:
    write: tuple[str, ...]
    deny_read: tuple[str, ...]
    network_allow: tuple[str, ...]
    sha: str
    ref: str
    text: bytes
    local_binding: bool = False


def reason_line(stderr: bytes | str, verb: str, rc: int) -> str:
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    ignored = ("Warning:", "warning:", "hint:", "Please make sure", "** ", "remote: ")
    for line in stderr.splitlines():
        line = line.strip()
        if line and not line.startswith(ignored):
            return line[:200]
    return f"git {verb} exited {rc}"[:200]


def git_env(base: dict[str, str]) -> dict[str, str]:
    env = {key: value for key, value in base.items()
           if key not in GIT_LOCAL_ENV and key not in GIT_PATHSPEC_ENV and not key.startswith(("GIT_TRACE", "AOS_"))}
    env["GIT_GRAFT_FILE"] = os.devnull
    env["GIT_SHALLOW_FILE"] = os.devnull
    return env


def check_hooks_dir() -> None:
    """Fail closed before a git call when something planted a hook in the runner's hooks directory.
    A directory that is gone runs no hook either, so that is not an error."""
    if os.path.islink(NO_HOOKS_DIR):
        raise PolicyError(f"policy: runner hooks directory {NO_HOOKS_DIR} is a symbolic link")
    try:
        entries = os.listdir(NO_HOOKS_DIR)
    except FileNotFoundError:
        return
    if entries:
        raise PolicyError(f"policy: runner hooks directory {NO_HOOKS_DIR} is not empty ({sorted(entries)[0]})")


def git_output(repo: str, *args: str, input: bytes | None = None,
               ok: tuple[int, ...] = (0,), git_dir: str | None = None) -> tuple[int, bytes]:
    check_hooks_dir()
    argv = [*GIT, "-C", repo]
    if git_dir is not None:
        argv.extend((f"--git-dir={git_dir}", f"--work-tree={repo}"))
    proc = subprocess.Popen([*argv, *args], env=git_env(os.environ),
                            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(input=input, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise PolicyError(f"git {args[0]} timed out after {GIT_TIMEOUT} s") from None
    if proc.returncode not in ok:
        raise PolicyError(reason_line(stderr, args[0], proc.returncode))
    return proc.returncode, stdout


def _policy_error(what: str) -> PolicyError:
    return PolicyError(f"policy: aos-policy.toml: {what}")


def _dedupe(entries: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(entries))


def _path_problem(entry: str) -> str | None:
    if entry in ("~", "~/"):
        return "is a bare ~"
    if entry.endswith("/"):
        return "has a trailing slash"
    if os.path.isabs(entry):
        return "is absolute"
    if any(part in ("", ".", "..") for part in entry.split("/")):
        return "has an empty, . or .. segment"
    return None


def _cache_path(entry: str, home: str) -> str:
    if not entry.startswith("~/"):
        raise _policy_error(f"write entry {entry!r} is not a supported placeholder or cache path")
    problem = _path_problem(entry)
    if problem:
        raise _policy_error(f"write entry {entry!r} {problem}")
    resolved = os.path.realpath(os.path.join(home, entry[2:]))
    roots = [os.path.realpath(os.path.join(home, root[2:])) for root in APPROVED_CACHE_ROOTS]
    for root in roots:
        try:
            if resolved != root and os.path.commonpath((resolved, root)) == root:
                return resolved
        except ValueError:
            pass
    raise _policy_error(f"write entry {entry!r} is outside approved cache roots")


def _network_entry(entry: str) -> None:
    if "://" in entry:
        raise _policy_error(f"network.allow entry {entry!r} has a scheme")
    if "/" in entry:
        raise _policy_error(f"network.allow entry {entry!r} has a path")
    if "@" in entry:
        raise _policy_error(f"network.allow entry {entry!r} has userinfo")
    host, separator, port = entry.rpartition(":")
    if not separator:
        host = entry
    elif not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise _policy_error(f"network.allow entry {entry!r} has an invalid port")
    if host.startswith("*."):
        host = host[2:]
    if not host or not all(_LABEL.fullmatch(label) for label in host.split(".")):
        raise _policy_error(f"network.allow entry {entry!r} is not a hostname")


def _table(data: dict, name: str, required: bool) -> dict:
    value = data.get(name)
    if value is None:
        if required:
            raise _policy_error(f"missing [{name}]")
        return {}
    if type(value) is not dict:
        raise _policy_error(f"{name} must be a table")
    return value


def _strings(value: object, name: str) -> list[str]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise _policy_error(f"{name} must be a list of strings")
    return value


def _validate(data: object, home: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], bool]:
    if type(data) is not dict:
        raise _policy_error("top level must be a table")
    unknown = set(data) - {"execution", "filesystem", "network", "tools"}
    if unknown:
        raise _policy_error(f"unknown section {sorted(unknown)[0]}")
    execution = _table(data, "execution", True)
    filesystem = _table(data, "filesystem", True)
    network = _table(data, "network", False)
    tools = _table(data, "tools", False)
    if set(execution) != {"fail_closed"} or type(execution.get("fail_closed")) is not bool or execution["fail_closed"] is not True:
        raise _policy_error("execution.fail_closed must be true and its only key")
    if set(filesystem) - {"write", "deny_read"}:
        raise _policy_error(f"unknown key filesystem.{sorted(set(filesystem) - {'write', 'deny_read'})[0]}")
    if "write" not in filesystem:
        raise _policy_error("filesystem.write is required")
    write_source = _strings(filesystem["write"], "filesystem.write")
    if "$WORKTREE" not in write_source or "$RUN_TMP" not in write_source:
        raise _policy_error("filesystem.write must include $WORKTREE and $RUN_TMP")
    deny_source = _strings(filesystem.get("deny_read", []), "filesystem.deny_read")
    if set(network) - {"allow", "local_binding"}:
        raise _policy_error(f"unknown key network.{sorted(set(network) - {'allow', 'local_binding'})[0]}")
    allow_source = _strings(network.get("allow", []), "network.allow")
    local_binding = network.get("local_binding", False)
    if type(local_binding) is not bool:
        raise _policy_error("network.local_binding must be true or false")
    if set(tools) - {"web_fetch", "mcp"} or ("web_fetch" in tools and (type(tools["web_fetch"]) is not bool or tools["web_fetch"] is not False)) or ("mcp" in tools and (type(tools["mcp"]) is not list or tools["mcp"] != [])):
        raise _policy_error("tools only permits web_fetch = false and mcp = []")
    write = []
    for entry in write_source:
        if entry in ("$WORKTREE", "$RUN_TMP"):
            write.append(entry)
        else:
            write.append(_cache_path(entry, home))
    deny = list(BASELINE_DENY_READ)
    for entry in deny_source:
        if entry.startswith("$"):
            raise _policy_error(f"deny_read entry {entry!r} is a placeholder")
        problem = _path_problem(entry)
        if problem:
            raise _policy_error(f"deny_read entry {entry!r} {problem}")
        deny.append(entry)
    for entry in allow_source:
        _network_entry(entry)
    return _dedupe(write), _dedupe(deny), _dedupe(allow_source), local_binding


def _integrity_error() -> PolicyError:
    return PolicyError("policy: source git directory is redirected, bare, a linked worktree, or not a repository root; the policy source must be a main checkout's root")


def _remote_error(exc: PolicyError) -> PolicyError:
    return PolicyError("policy: default branch unavailable: " + str(exc))


def _lines(output: bytes) -> list[tuple[str, str]]:
    return [tuple(line.split("\t", 1)) for line in output.decode(errors="replace").splitlines() if "\t" in line]


def _fetch(source: str, branch: str, sha: str) -> Default:
    try:
        git_output(source, "fetch", "--no-tags", "--refmap=", "--no-write-fetch-head", "origin", f"refs/heads/{branch}")
        git_output(source, "cat-file", "-e", f"{sha}^{{commit}}")
    except PolicyError as exc:
        raise _remote_error(exc) from None
    return Default("available", branch, sha, f"origin/{branch}@{sha[:12]}")


def resolve_default(source: str) -> Default:
    try:
        _, output = git_output(source, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel")
        git_dir, common_dir, top = output.decode(errors="replace").splitlines()
        if not os.path.isdir(git_dir) or os.path.realpath(git_dir) != os.path.realpath(common_dir) or not os.path.samefile(top, source):
            raise ValueError
    except (PolicyError, ValueError, OSError):
        raise _integrity_error() from None
    try:
        rc, origin = git_output(source, "config", "--get", "remote.origin.url", ok=(0, 1))
    except PolicyError as exc:
        raise PolicyError("policy: " + str(exc)) from None
    if rc == 1 or not origin.decode(errors="replace").strip():
        try:
            branch_rc, branch_out = git_output(source, "symbolic-ref", "-q", "--short", "HEAD", ok=(0, 1))
            sha_rc, sha_out = git_output(source, "rev-parse", "-q", "--verify", "HEAD", ok=(0, 1))
        except PolicyError as exc:
            raise PolicyError("policy: " + str(exc)) from None
        branch = branch_out.decode(errors="replace").strip() if branch_rc == 0 else "HEAD"
        if sha_rc == 1:
            return Default("empty", "", "", "")
        sha = sha_out.decode(errors="replace").strip()
        return Default("local", branch, sha, f"{branch}@{sha[:12]}")
    try:
        head = _lines(git_output(source, "ls-remote", "--symref", "origin", "HEAD")[1])
    except PolicyError as exc:
        raise _remote_error(exc) from None
    symrefs = [first[5:] for first, second in head if second == "HEAD" and first.startswith("ref: ")]
    shas = [first for first, second in head if second == "HEAD" and not first.startswith("ref: ")]
    if symrefs and not symrefs[0].startswith("refs/heads/"):
        raise _remote_error(PolicyError("remote HEAD is not a branch"))
    if shas and not symrefs:
        raise _remote_error(PolicyError("remote HEAD is detached"))
    if symrefs and shas:
        branch = symrefs[0][len("refs/heads/"):]
        if not branch.startswith("ao/"):
            return _fetch(source, branch, shas[0])
    try:
        refs = _lines(git_output(source, "ls-remote", "origin")[1])
    except PolicyError as exc:
        raise _remote_error(exc) from None
    branches = [ref for _, ref in refs if ref.startswith("refs/heads/")]
    if not branches or all(ref.startswith("refs/heads/ao/") for ref in branches):
        return Default("empty", "", "", "")
    if symrefs:
        branch = symrefs[0][len("refs/heads/"):]
        raise _remote_error(PolicyError(f"remote default branch is an agent branch {branch}"))
    raise _remote_error(PolicyError("remote HEAD names no branch"))


def read_policy(source: str, default: Default, home: str) -> Policy | None:
    if default.state == "empty":
        return None
    try:
        _, listing = git_output(source, "ls-tree", "--full-tree", default.sha, "--", POLICY_PATH)
    except PolicyError as exc:
        raise PolicyError("policy: " + str(exc)) from None
    if not listing.strip():
        return None
    try:
        info, name = listing.splitlines()[0].split(b"\t", 1)
        mode, kind, blob_id = info.split()
    except ValueError:
        raise _policy_error("invalid ls-tree result") from None
    if name != POLICY_PATH.encode() or mode not in (b"100644", b"100755") or kind != b"blob":
        raise PolicyError("policy: aos-policy.toml is not a regular file")
    try:
        _, blob = git_output(source, "cat-file", "blob", blob_id.decode())
        _, hashed = git_output(source, "hash-object", "-t", "blob", "--stdin", input=blob)
    except PolicyError as exc:
        raise PolicyError("policy: " + str(exc)) from None
    if hashed.strip() != blob_id:
        raise PolicyError("policy: aos-policy.toml object mismatch")
    try:
        data = tomllib.loads(blob.decode("utf-8"))
        write, deny, allowed, local_binding = _validate(data, home)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, PolicyError):
            raise
        raise _policy_error(str(exc)) from None
    return Policy(write, deny, allowed, hashlib.sha256(blob).hexdigest(), default.ref, blob, local_binding)


def resolve_tip(worktree: str, branch: str, git_dir: str | None = None) -> str:
    return git_output(worktree, "rev-parse", "--verify", f"refs/heads/{branch}", git_dir=git_dir)[1].decode(errors="replace").strip()


def branch_changes_policy(worktree: str, default_sha: str, tip: str, git_dir: str | None = None) -> bool:
    _, branch = git_output(worktree, "diff", "--name-only", f"{default_sha}...{tip}", "--", POLICY_PATH, git_dir=git_dir)
    _, head = git_output(worktree, "diff", "--name-only", f"{default_sha}...HEAD", "--", POLICY_PATH, git_dir=git_dir)
    return bool(branch.strip() or head.strip())


def bus_host(url: str) -> str:
    parsed = urlsplit(url if "://" in url else "//" + url)
    try:
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise PolicyError(f"policy: bus URL has no hostname: {url}") from exc
    if not host:
        raise PolicyError(f"policy: bus URL has no hostname: {url}")
    if ":" in host:
        host = f"[{host}]"
    return host if port is None else f"{host}:{port}"
