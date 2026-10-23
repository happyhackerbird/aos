"""Evidence declaration, capture, bus summary and retention for verification-connected acceptance.

A repository declares a repo-relative evidence directory in `<source>/.aos/evidence.toml`. After
acceptance the runner copies what acceptance wrote there (and what the agent named) into
`<root>/<bus id>/<run id>/` on the runner host, outside every worktree and sandbox, and posts a bounded summary
to the bus. This module holds the pure pieces; the runners call it.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Callable, Mapping

DEFAULT_ROOT = "~/.aos/evidence"
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
MAX_MAX_BYTES = 1024 * 1024 * 1024
SUMMARY_MAX_BYTES = 60000
OUTPUT_TAIL_BYTES = 1048576
FILES_PER_SIDE = 500
TEXT_CUT = 300
LINE_CUT = 200
REFUSAL_PREFIX = "evidence summary refused by the bus: "
_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


@dataclass(frozen=True)
class Declaration:
    dir: str
    max_bytes: int


def evidence_root(environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    return environ.get("AOS_EVIDENCE_ROOT") or os.path.expanduser(DEFAULT_ROOT)


NO_BUS_ID_REFUSAL = "the bus does not accept evidence summaries or gives no bus id; update the bus first"
_BUS_ID = re.compile(r"[0-9a-f]{32}")


def bus_id(client: Any) -> str | None:
    """The bus's own id from GET /capabilities: only for a 200 answer whose `evidence` is true and whose
    `bus_id` is 32 lowercase hex characters (a full match). Anything else, a raising client included, is None."""
    try:
        response = client.get("/capabilities")
        if response.status_code != 200:
            return None
        data = response.json()
        value = data.get("evidence") is True and data.get("bus_id")
        return value if isinstance(value, str) and _BUS_ID.fullmatch(value) else None
    except Exception:  # noqa: BLE001 -- an unreachable or odd bus gives no id; never raises
        return None


# -- declaration ---------------------------------------------------------------------------------

def _valid_dir(value: str) -> bool:
    if not value or "\\" in value or "\x00" in value or value.startswith("/"):
        return False
    segments = value.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return False
    return segments[0] not in (".git", ".aos")


def _git_ignores(source: str, rel_dir: str) -> bool:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV}
    proc = subprocess.run(["git", "-C", source, "check-ignore", "-q", "--", f"{rel_dir}/probe"],
                          env=env, capture_output=True, timeout=30)
    return proc.returncode == 0


def load_declaration(source: str) -> Declaration | None:
    path = Path(source) / ".aos" / "evidence.toml"
    try:
        if not path.exists():
            return None
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"evidence.toml: {exc}") from exc
    for key in sorted(data):
        if key not in ("dir", "max_bytes"):
            raise ValueError(f"evidence.toml: unknown key {key}")
    raw = data.get("dir")
    if not isinstance(raw, str) or not raw:
        raise ValueError("evidence.toml: dir is required and must be a non-empty string")
    rel = raw[:-1] if raw.endswith("/") else raw
    if not _valid_dir(rel):
        raise ValueError(f"evidence.toml: dir {raw!r} must be a repo-relative POSIX path with no "
                         "'.', '..' or empty segment and not under .git or .aos")
    max_bytes = data.get("max_bytes", DEFAULT_MAX_BYTES)
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_MAX_BYTES:
        raise ValueError(f"evidence.toml: max_bytes must be an integer from 1 to {MAX_MAX_BYTES}")
    try:
        ignored = _git_ignores(source, rel)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"evidence.toml: cannot check git ignore rules: {exc}") from exc
    if not ignored:
        raise ValueError(f"evidence.toml: {rel}/ is not ignored by git in the source repository; "
                         "add it to .gitignore")
    return Declaration(rel, max_bytes)


# -- listing and selection -----------------------------------------------------------------------

Listing = dict[str, tuple[int, int]]


def listing_local(base: str, rel_dir: str) -> Listing:
    """Regular files under <base>/<rel_dir>: path relative to the directory -> (size, mtime_ns).
    Links are never followed; a declared directory whose realpath leaves <base> lists nothing."""
    top = os.path.join(base, rel_dir)
    real_top, real_base = os.path.realpath(top), os.path.realpath(base)
    if real_top != real_base and not real_top.startswith(real_base + os.sep):
        return {}
    if not os.path.isdir(real_top):
        return {}
    found: Listing = {}
    for current, _dirs, names in os.walk(real_top, followlinks=False):
        for name in names:
            full = os.path.join(current, name)
            try:
                info = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                found[Path(os.path.relpath(full, real_top)).as_posix()] = (info.st_size, info.st_mtime_ns)
    return found


def listing_script(rel_dir: str) -> str:
    """Shell that lists the same mapping inside a sandbox; prints nothing when the directory or any
    parent is a link."""
    return (f"d=/sandbox/repo/{shlex.quote(rel_dir)}; "
            '[ -d "$d" ] && [ ! -L "$d" ] && [ "$(realpath "$d")" = "$d" ] && cd "$d" && '
            "find . -type f -printf '%P\\t%s\\t%T@\\n'")


def parse_listing(text: str) -> Listing:
    found: Listing = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0]:
            continue
        try:
            found[parts[0]] = (int(parts[1]), int(float(parts[2]) * 1e9))
        except ValueError:
            continue
    return found


def written_by_acceptance(before: Listing, after: Listing) -> list[str]:
    return sorted(path for path, meta in after.items() if before.get(path) != meta)


def within_cap(paths: list[str], sizes: Mapping[str, int], max_bytes: int) -> tuple[list[str], list[str]]:
    """Keep paths in order while the running total stays at or under max_bytes; the rest are skipped."""
    kept: list[str] = []
    total = 0
    for index, path in enumerate(paths):
        total += sizes[path]
        if total > max_bytes:
            return kept, list(paths[index:])
        kept.append(path)
    return kept, []


def _make_dirs(path: str) -> None:
    missing: list[str] = []
    current = path
    while current and not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            continue
        os.chmod(directory, 0o700)


def _write_new(path: str, chunks: Callable[[], bytes]) -> int:
    _make_dirs(os.path.dirname(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    total = 0
    with os.fdopen(fd, "wb") as out:
        while True:
            block = chunks()
            if not block:
                break
            out.write(block)
            total += len(block)
    return total


def copy_local(base: str, rel_dir: str, paths: list[str], dest: str) -> None:
    """Copy <base>/<rel_dir>/<p> to <dest>/<p>. The source is re-checked to stay under the declared
    directory and opened without following a final link; a violation raises OSError."""
    declared = os.path.realpath(os.path.join(base, rel_dir))
    for path in paths:
        lexical = os.path.join(base, rel_dir, path)
        parent = os.path.realpath(os.path.dirname(lexical))
        if parent != declared and not parent.startswith(declared + os.sep):
            raise PermissionError(f"evidence path leaves the declared directory: {path}")
        real = os.path.join(parent, os.path.basename(lexical))
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise PermissionError(f"evidence path is not a regular file: {path}")
            _write_new(os.path.join(dest, path), lambda: source.read(1 << 20))


def extract_tar(tgz: str, dest: str, max_bytes: int) -> list[str]:
    """Write the regular files of a gzip tar under dest without extractall. Links, devices, absolute
    or escaping names are skipped and returned; a member over the remaining budget is skipped and a
    stream that outgrows it is deleted, so dest never holds more than max_bytes."""
    skipped: list[str] = []
    remaining = max_bytes
    _make_dirs(dest)
    root = os.path.normpath(dest)
    with tarfile.open(tgz, mode="r:gz") as tar:
        for member in tar:
            if member.isdir():
                continue
            name = member.name
            parts = name.split("/")
            target = os.path.normpath(os.path.join(root, name))
            if (not member.isreg() or not name or name.startswith("/") or ".." in parts
                    or not target.startswith(root + os.sep)):
                skipped.append(name)
                continue
            if member.size > remaining:
                skipped.append(name)
                continue
            source = tar.extractfile(member)
            if source is None:
                skipped.append(name)
                continue
            try:
                written = _write_new(target, lambda: source.read(min(1 << 20, remaining + 1)))
            except (OSError, ValueError):
                skipped.append(name)
                continue
            if written > remaining:
                os.unlink(target)
                skipped.append(name)
                continue
            remaining -= written
    return skipped


# -- the agent's evidence field -----------------------------------------------------------------

def prompt_line(decl: Declaration) -> str:
    return (f"Evidence: write what proves your change under {decl.dir}/ in a new directory for this run "
            f"(for example {decl.dir}/<UTC stamp>), and end your final message with the line: "
            f"Evidence directory: {decl.dir}/<that directory>")


_STRIP = ".,;:)*_`"


def named_dir(message: str | None, rel_dir: str) -> str | None:
    """The directory name (below rel_dir) in the LAST `Evidence directory:` field of the message."""
    if not message:
        return None
    pattern = re.compile(r"[*_]*Evidence directory:[*_]*[ \t]*[*_`]*" + re.escape(rel_dir) + r"/([^\s`]+)")
    matches = list(pattern.finditer(message))
    if not matches:
        return None
    path = matches[-1].group(1).rstrip(_STRIP)
    if path.endswith("/"):
        path = path[:-1]
    if not path or "<" in path or ">" in path or _CONTROL.search(path):
        return None
    if ".." in path.split("/"):
        return None
    path = posixpath.normpath(path)
    if path in ("", ".", "..") or path.startswith("/") or path.startswith("../"):
        return None
    return path


def check_dir(listing: Listing, name: str, since_ns: int) -> str | None:
    """None when a file at or below `name` was written at or after since_ns; else `missing` (no file
    there) or `stale` (files there, none newer)."""
    below = [meta for path, meta in listing.items() if path == name or path.startswith(name + "/")]
    if not below:
        return "missing"
    if any(mtime >= since_ns for _size, mtime in below):
        return None
    return "stale"


def flags(declared_dir: str, acceptance_ran: bool, acceptance_paths: list[str], named: str | None,
          dir_state: str | None, skipped: list[str]) -> list[str]:
    """Flags in the record's order, each only when true. dir_state is check_dir's answer for `named`."""
    found: list[str] = []
    if acceptance_ran and not acceptance_paths:
        found.append(f"acceptance wrote no evidence under {declared_dir}")
    if named is None:
        found.append(f"no \"Evidence directory: {declared_dir}/...\" line in the agent's final message")
    elif dir_state == "missing":
        found.append(f"the agent's evidence directory {declared_dir}/{named} does not exist")
    elif dir_state == "stale":
        found.append(f"the agent's evidence directory {declared_dir}/{named} holds nothing written "
                     "during this run")
    if skipped:
        found.append(f"evidence over the cap: {len(skipped)} files skipped")
    return found


# -- bus summary ---------------------------------------------------------------------------------

def map_strings(data: Any, fn: Callable[[str], str]) -> Any:
    """A copy with every string value (never a key) passed through fn."""
    if isinstance(data, str):
        return fn(data)
    if isinstance(data, dict):
        return {key: map_strings(value, fn) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [map_strings(value, fn) for value in data]
    return data


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _cut(text: str, limit: int = TEXT_CUT) -> str:
    text = _CONTROL.sub("?", text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _encoded_len(data: dict) -> int:
    return len(json.dumps(data, separators=(",", ":")).encode())


def _kept_lines(lines: list[str], cap: int = 15) -> list[str]:
    """At most cap lines: every FAIL line first, then PASS lines, kept in file order."""
    wanted = [i for i, line in enumerate(lines) if line.startswith("FAIL ")][:cap]
    wanted += [i for i, line in enumerate(lines) if not line.startswith("FAIL ")][:cap - len(wanted)]
    return [lines[i] for i in sorted(wanted)]


def summary(local: dict) -> dict:
    """The bounded dict posted to the bus, built from the local manifest."""
    acceptance_all = list(local.get("acceptance") or [])
    results_all = list(local.get("results") or [])
    shots_all = list(local.get("screenshots") or [])
    shots_ok = [path for path in shots_all if len(path) <= TEXT_CUT]
    failing = [r for r in results_all if any(str(line).startswith("FAIL ") for line in r["lines"])]
    failing_ids = {id(r) for r in failing}
    clean = [r for r in results_all if id(r) not in failing_ids]
    chosen = (failing + clean)[:8]
    chosen_ids = {id(r) for r in chosen}
    totals = [str(line)[:5] for r in results_all for line in r["lines"]]
    results = []
    for result in (r for r in results_all if id(r) in chosen_ids):
        kept = _kept_lines(list(result["lines"]))
        results.append({"file": result["file"], "lines": [_cut(line, LINE_CUT) for line in kept],
                        "lines_omitted": len(result["lines"]) - len(kept)})
    data: dict[str, Any] = {
        "run_id": local.get("run_id"), "task_id": local.get("task_id"), "dir": local.get("dir"),
        "declared": local.get("declared"),
        "file_count": len(local.get("files") or []),
        "bytes": local.get("bytes", sum(f.get("bytes", 0) for f in local.get("files") or [])),
        "flags": list(local.get("flags") or [])[:10],
        "acceptance": [{"command": entry["command"], "exit": entry.get("exit"), "note": entry.get("note", "")}
                       for entry in acceptance_all[:20]],
        "acceptance_omitted": max(0, len(acceptance_all) - 20),
        "results": results, "results_omitted": len(results_all) - len(results),
        "pass_total": totals.count("PASS "), "fail_total": totals.count("FAIL "),
        "screenshots": shots_ok[:20], "screenshots_omitted": len(shots_all) - len(shots_ok[:20]),
        "agent_dir": local.get("agent_dir"),
    }
    data = map_strings(data, _cut)
    while _encoded_len(data) > SUMMARY_MAX_BYTES:
        if data["results"]:
            data["results"].pop()
            data["results_omitted"] += 1
        elif data["screenshots"]:
            data["screenshots_omitted"] += len(data["screenshots"])
            data["screenshots"] = []
        elif data["acceptance"]:
            data["acceptance"].pop()
            data["acceptance_omitted"] += 1
        else:
            break
    return data


def flags_of(run: Any) -> list[str]:
    """The evidence flags of a run row, an evidence dict or evidence JSON text; [] when absent or
    unparseable. The bus's refusal marker reads as one flag."""
    value = run
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, dict) and "evidence" in value:
        value = value["evidence"]
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return []
    if not isinstance(value, dict):
        return []
    error = value.get("error")
    if isinstance(error, str) and not value.get("flags"):
        return [error if error.startswith(REFUSAL_PREFIX) else REFUSAL_PREFIX + error]
    found = value.get("flags")
    return [str(flag) for flag in found] if isinstance(found, list) else []


# -- local capture -------------------------------------------------------------------------------

def _result_lines(path: str) -> list[str]:
    lines: list[str] = []
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.rstrip("\r\n")
            if line.startswith(("PASS ", "FAIL ")):
                lines.append(line)
    return lines


def _acceptance_entries(results: list[dict], commands: list[str]) -> list[dict]:
    entries = [{"command": str(r.get("command", "")), "exit": r.get("exit"), "note": r.get("note", "")}
               for r in results]
    for command in commands[len(results):]:
        entries.append({"command": command, "exit": None, "note": "not run"})
    return entries


def collect_local(root: str, run_id: str, task_id: str, worktree: str, decl: Declaration, before: Listing,
                  results: list[dict], commands: list[str], message: str | None, since_ns: int) -> dict:
    """Copy this run's evidence to <root>/<run_id>/ and return the bus summary. Never raises: a failure
    returns a summary whose flags carry `evidence capture failed: <exception type>`."""
    host_dir = os.path.join(root, run_id)
    acceptance = _acceptance_entries(results, commands)
    try:
        after = listing_local(worktree, decl.dir)
        acceptance_paths = written_by_acceptance(before, after)
        named = named_dir(message, decl.dir)
        state = check_dir(after, named, since_ns) if named is not None else None
        agent_paths = sorted(p for p in after if named is not None and (p == named or p.startswith(named + "/")))
        skipped = [f"acceptance/{p}" for p in acceptance_paths[FILES_PER_SIDE:]]
        skipped += [f"agent/{p}" for p in agent_paths[FILES_PER_SIDE:]]
        acceptance_paths, agent_paths = acceptance_paths[:FILES_PER_SIDE], agent_paths[:FILES_PER_SIDE]
        outputs = [(f"outputs/{i}.txt", str(r.get("output") or "").encode("utf-8", "replace")[-OUTPUT_TAIL_BYTES:])
                   for i, r in enumerate(results)]
        sizes: dict[str, int] = {name: len(body) for name, body in outputs}
        sizes.update({f"acceptance/{p}": after[p][0] for p in acceptance_paths})
        sizes.update({f"agent/{p}": after[p][0] for p in agent_paths})
        ordered = [name for name, _ in outputs] + [f"acceptance/{p}" for p in acceptance_paths] \
            + [f"agent/{p}" for p in agent_paths]
        kept, over = within_cap(ordered, sizes, decl.max_bytes)
        skipped += over
        kept_set = set(kept)

        _make_dirs(root)
        _make_dirs(host_dir)
        for name, body in outputs:
            if name in kept_set:
                pending = [body]
                _write_new(os.path.join(host_dir, name), lambda: pending.pop() if pending else b"")
        copy_local(worktree, decl.dir, [p for p in acceptance_paths if f"acceptance/{p}" in kept_set],
                   os.path.join(host_dir, "acceptance"))
        copy_local(worktree, decl.dir, [p for p in agent_paths if f"agent/{p}" in kept_set],
                   os.path.join(host_dir, "agent"))

        files = [{"path": name, "bytes": sizes[name]} for name in kept]
        result_files, screenshots = [], []
        for name in kept:
            base = posixpath.basename(name)
            if base == "result.txt" and not name.startswith("outputs/"):
                result_files.append({"file": name, "lines": _result_lines(os.path.join(host_dir, name))})
            if not name.startswith("outputs/") and base.lower().endswith((".png", ".jpg", ".jpeg")):
                screenshots.append(name)
        local = {
            "run_id": run_id, "task_id": task_id, "dir": host_dir, "declared": decl.dir, "files": files,
            "bytes": sum(f["bytes"] for f in files), "skipped": skipped, "acceptance": acceptance,
            "results": result_files, "screenshots": screenshots, "agent_dir": named,
            "flags": flags(decl.dir, bool(results), acceptance_paths, named, state, skipped),
        }
        manifest_path = os.path.join(host_dir, "manifest.json")
        fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(local, out, indent=1)
        return summary(local)
    except Exception as exc:  # noqa: BLE001 -- capture never raises; the flag carries the type
        return summary({
            "run_id": run_id, "task_id": task_id, "dir": host_dir, "declared": decl.dir, "files": [],
            "bytes": 0, "acceptance": acceptance, "results": [], "screenshots": [], "agent_dir": None,
            "flags": [f"evidence capture failed: {type(exc).__name__}"]})


# -- retention -----------------------------------------------------------------------------------

REMOVABLE_RUN = ("passed", "rejected", "cancelled")
REMOVABLE_TASK = ("done", "cancelled")


def remove_run(root: str, bus: str, run_id: str) -> bool:
    """Remove <root>/<bus>/<run_id> when it is a real directory directly under <root>/<bus>."""
    if not _BUS_ID.fullmatch(bus):
        return False
    if not run_id or run_id in (".", "..") or os.sep in run_id or "/" in run_id or "\x00" in run_id:
        return False
    path = os.path.join(root, bus, run_id)
    if os.path.islink(path) or not os.path.isdir(path):
        return False
    shutil.rmtree(path)
    return True


def prune(client: Any, root: str, err: IO[str]) -> int:
    """Remove run directories whose run is passed, rejected or cancelled, or whose task is done or
    cancelled, scanning only <root>/<bus id>/ for the bus that answers `client`. Unknown runs, runs in
    review, failed, blocked or live, and any bus trouble keep the directory. Returns the number removed.
    Without a bus-id directory directly under root there is no bus call; without an id from the bus
    nothing is removed."""
    if not os.path.isdir(root):
        return 0
    if not any(_BUS_ID.fullmatch(entry) for entry in os.listdir(root)):
        return 0
    bus = bus_id(client)
    if bus is None:
        print("evidence prune skipped: the bus gives no id", file=err)
        return 0
    root = os.path.join(root, bus)
    if os.path.islink(root) or not os.path.isdir(root):
        return 0
    removed = 0
    tasks: dict[str, Any] = {}
    for name in sorted(os.listdir(root)):
        directory = os.path.join(root, name)
        manifest = os.path.join(directory, "manifest.json")
        if os.path.islink(directory) or not os.path.isfile(manifest):
            continue
        try:
            with open(manifest, encoding="utf-8") as stream:
                data = json.load(stream)
            run_id, task_id = data["run_id"], data["task_id"]
        except (OSError, ValueError, KeyError, TypeError):
            print(f"evidence prune: unreadable manifest {manifest}", file=err)
            continue
        if run_id != name or not isinstance(task_id, str):
            continue
        if task_id not in tasks:
            try:
                response = client.get(f"/tasks/{task_id}")
            except Exception as exc:  # noqa: BLE001 -- HTTP trouble keeps the directory
                print(f"evidence prune: bus unreachable ({type(exc).__name__}); {name} kept", file=err)
                tasks[task_id] = None
                continue
            tasks[task_id] = response.json() if response.status_code == 200 else None
        task = tasks[task_id]
        if not isinstance(task, dict):
            continue
        run = next((r for r in task.get("runs") or [] if r.get("id") == run_id), None)
        if run is None:
            continue
        if run.get("status") in REMOVABLE_RUN or task.get("status") in REMOVABLE_TASK:
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                print(f"evidence prune: cannot remove {directory}: {exc}", file=err)
                continue
            removed += 1
    return removed


def prune_quietly(client: Any, environ: Mapping[str, str], err: IO[str]) -> None:
    try:
        prune(client, evidence_root(environ), err)
    except Exception as exc:  # noqa: BLE001 -- best effort, never blocks a run
        print(f"evidence prune skipped: {type(exc).__name__}", file=err)
