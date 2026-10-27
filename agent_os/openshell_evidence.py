"""Evidence capture for the OpenShell backend: the sandbox-side pieces of agent_os.evidence.

The runner calls these from run_task: guarded directory listings inside a sandbox, the claude and codex
final messages, acceptance exit codes and outputs from a fresh acceptance sandbox, and `collect`, which
attributes files by before/after listings inside the tree that ran acceptance, fetches the selected
files with `tar --no-recursion --verbatim-files-from -T` behind the listing guard, and writes the same
per-run directory and manifest as the native runner. Everything that touches a sandbox goes through
`agent_os.openshell_runner` (imported inside the functions, which imports this module).
"""
from __future__ import annotations

import json
import os
import posixpath
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_os import evidence

FINAL_MESSAGE_CAP = 5_000_000
STARTED_FILE = "/sandbox/harness.started"
BEFORE_FILE = "/sandbox/accept-before.txt"
TIMEOUT_NOTE = "timeout or not reached"


def _adapter():
    from agent_os import openshell_runner
    return openshell_runner


# -- reading a sandbox --------------------------------------------------------------------------

def listing(name: str, rel_dir: str) -> evidence.Listing:
    """The guarded listing of <rel_dir> in a sandbox; {} when the directory is absent, a link, or the exec fails."""
    try:
        got = _adapter().sandbox_exec(name, evidence.listing_script(rel_dir), timeout=60)
    except Exception:  # noqa: BLE001 -- a listing is best effort; an empty one reads as `nothing there`
        return {}
    return evidence.parse_listing(got.stdout) if got.returncode == 0 else {}


def read_started(name: str) -> int:
    """The harness start clock (ns) the job sandbox wrote; ValueError when it cannot be read."""
    got = _adapter().sandbox_exec_direct(name, ["/bin/cat", STARTED_FILE], timeout=60)
    text = got.stdout.strip()
    if got.returncode != 0 or not text.isdecimal():
        raise ValueError("harness.started is unreadable")
    return int(text)


def read_before(name: str) -> evidence.Listing:
    """The listing the acceptance sandbox took after extraction and before its first command."""
    got = _adapter().sandbox_exec_direct(name, ["/bin/cat", BEFORE_FILE], timeout=60)
    if got.returncode != 0:
        raise ValueError("accept-before.txt is unreadable")
    return evidence.parse_listing(got.stdout)


def exit_codes(name: str, count: int) -> list[int | None]:
    """The exit code of each fresh-acceptance command from accept-<i>.rc; None when the file is absent."""
    adapter = _adapter()
    codes: list[int | None] = []
    for i in range(count):
        got = adapter.sandbox_exec_direct(name, ["/bin/cat", f"/sandbox/accept-{i}.rc"], timeout=60)
        text = got.stdout.strip()
        codes.append(int(text) if got.returncode == 0 and text.lstrip("-").isdecimal() else None)
    return codes


def outputs(name: str, count: int) -> list[str]:
    """The last MiB of each fresh-acceptance command's output."""
    adapter = _adapter()
    found: list[str] = []
    for i in range(count):
        got = adapter.sandbox_exec_direct(name, ["/usr/bin/tail", "-c", str(evidence.OUTPUT_TAIL_BYTES),
                                                 f"/sandbox/accept-{i}.out"], timeout=120)
        found.append(got.stdout if got.returncode == 0 else "")
    return found


def final_message(name: str, harness: str) -> str | None:
    """The agent's final message from the TAIL of /sandbox/harness.out for claude and codex.

    A Claude transcript over the cap is read from its last FINAL_MESSAGE_CAP + 1 bytes, with the byte
    before them deciding whether the first retained line is complete (a newline) or partial (dropped).
    None when the exec fails or nothing parses.
    """
    adapter = _adapter()
    try:
        if harness == "claude":
            got = adapter.sandbox_exec_direct_bytes(name, ["/usr/bin/tail", "-c", str(FINAL_MESSAGE_CAP + 2),
                                                           "/sandbox/harness.out"], timeout=120)
            if got.returncode != 0:
                return None
            raw = got.stdout
            if len(raw) > FINAL_MESSAGE_CAP + 1:
                first, raw = raw[:1], raw[1:]
                if first != b"\n":
                    idx = raw.find(b"\n")
                    raw = raw[idx + 1:] if idx >= 0 else b""
        else:
            got = adapter.sandbox_exec_direct_bytes(name, ["/usr/bin/tail", "-c", str(FINAL_MESSAGE_CAP + 1),
                                                           "/sandbox/harness.out"], timeout=120)
            if got.returncode != 0:
                return None
            raw = got.stdout
            if len(raw) > FINAL_MESSAGE_CAP:
                raw = raw[-FINAL_MESSAGE_CAP:]
                newline = raw.find(b"\n")
                raw = raw[newline + 1:] if newline >= 0 else b""
        from agent_os.harness import parse_harness_output
        message, _session = parse_harness_output(raw, harness)
        return message
    except (ValueError, UnicodeError):
        return None


# -- fetching files -----------------------------------------------------------------------------

def _guard(rel_dir: str) -> str:
    """The listing script's directory guard (a real directory whose realpath is itself), then `cd` into it."""
    script = evidence.listing_script(rel_dir)
    head, sep, _find = script.rpartition(" && find ")
    if not sep:
        raise ValueError("listing script has no find step")
    return head


def _tree_bytes(path: str) -> int:
    total = 0
    for current, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                continue
    return total


def fetch(name: str, rel_dir: str, paths: list[str], state: Path, dest: str, budget: int) -> list[str]:
    """Copy <rel_dir>/<p> of a sandbox to <dest>/<p> for the given relative paths; returns the skipped names."""
    if not paths:
        return []
    adapter = _adapter()
    listfile, tgz = state / "evidence.list", state / "evidence.tgz"
    try:
        adapter._write_private(listfile, "".join(f"{p}\n" for p in paths if "\n" not in p).encode())
        adapter.openshell("sandbox", "upload", name, str(listfile), "/sandbox/evidence.list")
        adapter.sandbox_exec(name, f"{_guard(rel_dir)} && tar czf /sandbox/evidence.tgz --no-recursion "
                                   "--verbatim-files-from -T /sandbox/evidence.list", timeout=300)
        tgz.unlink(missing_ok=True)
        adapter.openshell("sandbox", "download", name, "/sandbox/evidence.tgz", str(tgz), timeout=300)
        return evidence.extract_tar(str(tgz), dest, budget)
    finally:
        listfile.unlink(missing_ok=True)
        tgz.unlink(missing_ok=True)


# -- collect ------------------------------------------------------------------------------------

def _redact_files(top: str, redact_bytes: Callable[[bytes], bytes]) -> None:
    """Rewrite in place, same mode, every regular file under top whose bytes change under redact_bytes; links are skipped."""
    for current, _dirs, names in os.walk(top):
        for name in names:
            path = os.path.join(current, name)
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, "r+b") as stream:
                data = stream.read()
                scrubbed = redact_bytes(data)
                if scrubbed != data:
                    stream.seek(0)
                    stream.write(scrubbed)
                    stream.truncate()


def _disk_size(path: str) -> int | None:
    """The size of the regular file written at path; None when nothing was written there."""
    try:
        meta = os.lstat(path)
    except OSError:
        return None
    return meta.st_size if stat.S_ISREG(meta.st_mode) else None


def _fresh_results(tree: str, commands: list[str], timed_out: bool) -> list[dict[str, Any]]:
    codes = exit_codes(tree, len(commands))
    tails = outputs(tree, len(commands))
    results: list[dict[str, Any]] = []
    for command, code, tail in zip(commands, codes, tails):
        if code is None:
            if not timed_out:
                break                  # never reached: the padding reads `not run`
            results.append({"command": command, "exit": None, "note": TIMEOUT_NOTE, "output": tail})
        else:
            results.append({"command": command, "exit": code, "note": "", "output": tail})
    return results


def collect(*, root: str, run_id: str, task_id: str, decl: evidence.Declaration, job: str, tree: str, fresh: bool,
            state: Path, commands: list[str], message: str | None, agent_listing: evidence.Listing,
            before: evidence.Listing | None = None, results: list[dict[str, Any]] | None = None,
            timed_out: bool = False, redact: Callable[[str], str] | None = None,
            redact_bytes: Callable[[bytes], bytes] | None = None) -> dict:
    """Copy this run's evidence to <root>/<run_id>/ and return the bus summary (the runner passes
    <evidence root>/<bus id> as `root`). `tree` is the sandbox that
    ran acceptance (the -acc sandbox when `fresh`, else `job`); `agent_listing` is the job sandbox listing
    taken after the harness. `redact` passes every result's output before it is written and `redact_bytes`
    every fetched file, rewritten in place, before the manifest reads it (F111); the manifest's sizes are the
    disk's, and a kept name that never reached the disk is skipped. Never raises: a failure returns a summary
    whose flags carry `evidence capture failed: <exception type>`."""
    host_dir = os.path.join(root, run_id)
    entries = evidence._acceptance_entries(list(results or []), commands)
    try:
        since_ns = read_started(job)
        if fresh:
            before = read_before(tree)
            results = _fresh_results(tree, commands, timed_out)
        before = before or {}
        results = list(results or [])
        if timed_out:
            for command in commands[len(results):]:
                results.append({"command": command, "exit": None, "note": TIMEOUT_NOTE, "output": ""})
        if redact is not None:
            results = [{**r, "output": redact(str(r.get("output") or ""))} for r in results]
        entries = evidence._acceptance_entries(results, commands)
        after = listing(tree, decl.dir)
        acceptance_paths = evidence.written_by_acceptance(before, after)
        named = evidence.named_dir(message, decl.dir)
        dir_state = evidence.check_dir(agent_listing, named, since_ns) if named is not None else None
        agent_paths = sorted(p for p in agent_listing
                             if named is not None and (p == named or p.startswith(named + "/")))
        per_side = evidence.FILES_PER_SIDE
        skipped = [f"acceptance/{p}" for p in acceptance_paths[per_side:]]
        skipped += [f"agent/{p}" for p in agent_paths[per_side:]]
        acceptance_paths, agent_paths = acceptance_paths[:per_side], agent_paths[:per_side]
        out_files = [(f"outputs/{i}.txt", str(r.get("output") or "").encode("utf-8", "replace")[-evidence.OUTPUT_TAIL_BYTES:])
                     for i, r in enumerate(results)]
        sizes: dict[str, int] = {name: len(body) for name, body in out_files}
        sizes.update({f"acceptance/{p}": after[p][0] for p in acceptance_paths})
        sizes.update({f"agent/{p}": agent_listing[p][0] for p in agent_paths})
        ordered = [name for name, _ in out_files] + [f"acceptance/{p}" for p in acceptance_paths] \
            + [f"agent/{p}" for p in agent_paths]
        kept, over = evidence.within_cap(ordered, sizes, decl.max_bytes)
        skipped += over
        kept_set = set(kept)

        evidence._make_dirs(root)
        evidence._make_dirs(host_dir)
        for name, body in out_files:
            if name in kept_set:
                pending = [body]
                evidence._write_new(os.path.join(host_dir, name), lambda: pending.pop() if pending else b"")
        for side, sandbox, paths in (("acceptance", tree, acceptance_paths), ("agent", job, agent_paths)):
            chosen = [p for p in paths if f"{side}/{p}" in kept_set]
            dest = os.path.join(host_dir, side)
            budget = decl.max_bytes - _tree_bytes(host_dir)
            skipped += [f"{side}/{p}" for p in fetch(sandbox, decl.dir, chosen, state, dest, max(0, budget))]
        if redact_bytes is not None:
            for side in ("acceptance", "agent"):
                _redact_files(os.path.join(host_dir, side), redact_bytes)

        # The sizes are the disk's (a rewrite shrinks a file); a kept name tar never delivered, or a link
        # extract_tar refused, was never written and moves to `skipped` (F111, the parent's F79).
        files = []
        for name in kept:
            size = _disk_size(os.path.join(host_dir, name))
            if size is None:
                if name not in skipped:
                    skipped.append(name)
                continue
            files.append({"path": name, "bytes": size})
        kept = [f["path"] for f in files]
        result_files, screenshots = [], []
        for name in kept:
            base = posixpath.basename(name)
            if base == "result.txt" and not name.startswith("outputs/") and os.path.isfile(os.path.join(host_dir, name)):
                result_files.append({"file": name, "lines": evidence._result_lines(os.path.join(host_dir, name))})
            if not name.startswith("outputs/") and base.lower().endswith((".png", ".jpg", ".jpeg")):
                screenshots.append(name)
        local = {
            "run_id": run_id, "task_id": task_id, "dir": host_dir, "declared": decl.dir, "files": files,
            "bytes": sum(f["bytes"] for f in files), "skipped": skipped, "acceptance": entries,
            "results": result_files, "screenshots": screenshots, "agent_dir": named,
            "flags": evidence.flags(decl.dir, bool(results), acceptance_paths, named, dir_state, skipped),
        }
        fd = os.open(os.path.join(host_dir, "manifest.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(local, out, indent=1)
        return evidence.summary(local)
    except Exception as exc:  # noqa: BLE001 -- capture never raises; the flag carries the type
        return evidence.summary({
            "run_id": run_id, "task_id": task_id, "dir": host_dir, "declared": decl.dir, "files": [],
            "bytes": 0, "acceptance": entries, "results": [], "screenshots": [], "agent_dir": None,
            "flags": [f"evidence capture failed: {type(exc).__name__}"]})
