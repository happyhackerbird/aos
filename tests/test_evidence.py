from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_os import evidence
from agent_os.evidence import (
    DEFAULT_MAX_BYTES, MAX_MAX_BYTES, Declaration, check_dir, collect_local, copy_local, extract_tar,
    flags, flags_of, listing_local, listing_script, load_declaration, map_strings, named_dir,
    parse_listing, prompt_line, prune, prune_quietly, remove_run, summary, within_cap,
    written_by_acceptance,
)


def _repo(tmp_path: Path, ignore: str = "evidence/\n") -> Path:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / ".gitignore").write_text(ignore)
    return source


def _declare(source: Path, text: str) -> None:
    (source / ".aos").mkdir(exist_ok=True)
    (source / ".aos" / "evidence.toml").write_text(text)


# == load_declaration =========================================================

def test_absent_declaration_is_none(tmp_path: Path) -> None:
    assert load_declaration(str(_repo(tmp_path))) is None


def test_declaration_defaults_and_stripped_trailing_slash(tmp_path: Path) -> None:
    source = _repo(tmp_path)
    _declare(source, 'dir = "evidence/"\n')
    assert load_declaration(str(source)) == Declaration("evidence", DEFAULT_MAX_BYTES)
    _declare(source, 'dir = "evidence"\nmax_bytes = 1000\n')
    assert load_declaration(str(source)) == Declaration("evidence", 1000)


@pytest.mark.parametrize("text", [
    "",                                   # dir required
    'dir = ""\n', 'dir = 3\n', 'dir = "/abs"\n', 'dir = "a/../b"\n', 'dir = "."\n', 'dir = "a//b"\n',
    'dir = "a\\\\b"\n', 'dir = ".git"\n', 'dir = ".git/x"\n', 'dir = ".aos"\n', 'dir = "./x"\n',
    'dir = "evidence//"\n', 'dir = "evidence"\nmax_bytes = 0\n',
    f'dir = "evidence"\nmax_bytes = {MAX_MAX_BYTES + 1}\n', 'dir = "evidence"\nmax_bytes = true\n',
    'dir = "evidence"\nmax_bytes = "5"\n', 'dir = "evidence"\nother = 1\n', "dir = [",
])
def test_declaration_refusals(tmp_path: Path, text: str) -> None:
    source = _repo(tmp_path, ignore="evidence/\na/\n")
    _declare(source, text)
    with pytest.raises(ValueError, match="^evidence.toml: "):
        load_declaration(str(source))


def test_declaration_refuses_a_directory_git_does_not_ignore(tmp_path: Path) -> None:
    source = _repo(tmp_path, ignore="other/\n")
    _declare(source, 'dir = "evidence"\n')
    with pytest.raises(ValueError, match=r"evidence.toml: evidence/ is not ignored by git in the source "
                                          r"repository; add it to .gitignore"):
        load_declaration(str(source))


def test_declaration_in_a_non_git_source_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "plain"
    source.mkdir()
    _declare(source, 'dir = "evidence"\n')
    with pytest.raises(ValueError, match="evidence.toml: "):
        load_declaration(str(source))


# == listing and selection ====================================================

def _tree(tmp_path: Path) -> Path:
    base = tmp_path / "wt"
    (base / "evidence" / "run1").mkdir(parents=True)
    (base / "evidence" / "run1" / "result.txt").write_text("PASS a\nFAIL b\nnote\n")
    (base / "evidence" / "run1" / "shot.png").write_bytes(b"png")
    return base


def test_listing_local_lists_regular_files_only_and_never_follows_links(tmp_path: Path) -> None:
    base = _tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    (base / "evidence" / "link").symlink_to(outside)
    (base / "evidence" / "flink").symlink_to(outside / "secret.txt")
    listing = listing_local(str(base), "evidence")
    assert sorted(listing) == ["run1/result.txt", "run1/shot.png"]
    size, mtime = listing["run1/result.txt"]
    assert size == len("PASS a\nFAIL b\nnote\n") and isinstance(mtime, int)


def test_listing_local_missing_directory_or_a_directory_linked_out_is_empty(tmp_path: Path) -> None:
    base = tmp_path / "wt"
    base.mkdir()
    assert listing_local(str(base), "evidence") == {}
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("x")
    (base / "evidence").symlink_to(outside)
    assert listing_local(str(base), "evidence") == {}


def test_parse_listing_and_script() -> None:
    text = "a/b.txt\t12\t1700000000.5\nbad line\n\tx\ty\nc.png\t3\t1700000001.25\n"
    assert parse_listing(text) == {"a/b.txt": (12, int(1700000000.5 * 1e9)),
                                   "c.png": (3, int(1700000001.25 * 1e9))}
    script = listing_script("evidence")
    assert "d=/sandbox/repo/evidence" in script and '[ ! -L "$d" ]' in script
    assert 'realpath "$d")" = "$d"' in script
    assert "find . -type f -printf '%P\\t%s\\t%T@\\n'" in script
    assert "d=/sandbox/repo/'a b'" in listing_script("a b")


def test_written_by_acceptance_within_one_tree() -> None:
    before = {"a": (1, 10), "b": (2, 20), "c": (3, 30)}
    after = {"a": (1, 10), "b": (2, 21), "c": (3, 30), "d": (4, 40)}
    assert written_by_acceptance(before, after) == ["b", "d"]
    assert written_by_acceptance({}, {}) == []


def test_within_cap_keeps_in_order_until_the_cap_and_skips_the_rest() -> None:
    sizes = {"a": 4, "b": 4, "c": 1, "d": 1}
    assert within_cap(["a", "b", "c", "d"], sizes, 8) == (["a", "b"], ["c", "d"])
    assert within_cap(["a", "b"], sizes, 100) == (["a", "b"], [])
    assert within_cap(["a"], sizes, 3) == ([], ["a"])


def test_copy_local_copies_with_modes_and_refuses_a_symlink_out(tmp_path: Path) -> None:
    base = _tree(tmp_path)
    dest = tmp_path / "dest"
    copy_local(str(base), "evidence", ["run1/result.txt", "run1/shot.png"], str(dest))
    assert (dest / "run1" / "result.txt").read_text() == "PASS a\nFAIL b\nnote\n"
    assert (dest / "run1" / "result.txt").stat().st_mode & 0o777 == 0o600
    assert (dest / "run1").stat().st_mode & 0o777 == 0o700
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (base / "evidence" / "leak").symlink_to(outside)
    with pytest.raises(OSError):
        copy_local(str(base), "evidence", ["leak"], str(tmp_path / "dest2"))
    assert not (tmp_path / "dest2" / "leak").exists()
    with pytest.raises(OSError):
        copy_local(str(base), "evidence", ["../../secret.txt"], str(tmp_path / "dest3"))


# == extract_tar ===============================================================

def _tgz(path: Path, members: list[tuple[str, Any]]) -> str:
    with tarfile.open(path, "w:gz") as tar:
        for name, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif isinstance(kind, tuple):
                info.type = kind[0]
                info.linkname = kind[1]
                tar.addfile(info)
            else:
                data = kind if isinstance(kind, bytes) else kind.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return str(path)


def test_extract_tar_writes_regular_files_and_names_what_it_skips(tmp_path: Path) -> None:
    tgz = _tgz(tmp_path / "e.tgz", [
        ("ok/a.txt", "hello"), ("d", "dir"), ("lnk", (tarfile.SYMTYPE, "/etc/passwd")),
        ("hard", (tarfile.LNKTYPE, "ok/a.txt")), ("/abs.txt", "x"), ("../esc.txt", "x"),
        ("a/../../esc2.txt", "x"), ("fifo", (tarfile.FIFOTYPE, "")),
    ])
    dest = tmp_path / "out"
    skipped = extract_tar(tgz, str(dest), 1000)
    assert (dest / "ok" / "a.txt").read_text() == "hello"
    assert (dest / "ok" / "a.txt").stat().st_mode & 0o777 == 0o600
    assert sorted(skipped) == sorted(["lnk", "hard", "/abs.txt", "../esc.txt", "a/../../esc2.txt", "fifo"])
    assert not (tmp_path / "esc.txt").exists() and not (tmp_path / "esc2.txt").exists()
    assert not (dest / "lnk").exists() and not (dest / "hard").exists()
    assert not (tmp_path / "abs.txt").exists()


def test_extract_tar_stops_at_max_bytes_and_leaves_no_partial_file(tmp_path: Path) -> None:
    tgz = _tgz(tmp_path / "e.tgz", [("a", "x" * 6), ("b", "y" * 6), ("c", "z" * 3)])
    dest = tmp_path / "out"
    skipped = extract_tar(tgz, str(dest), 10)
    assert (dest / "a").read_text() == "x" * 6
    assert not (dest / "b").exists()
    assert (dest / "c").read_text() == "z" * 3
    assert skipped == ["b"]
    total = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    assert total <= 10


def test_extract_tar_does_not_overwrite_an_existing_file(tmp_path: Path) -> None:
    tgz = _tgz(tmp_path / "e.tgz", [("a", "new")])
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "a").write_text("old")
    assert extract_tar(tgz, str(dest), 100) == ["a"]
    assert (dest / "a").read_text() == "old"


# == the evidence field ========================================================

AO3 = "Done. Evidence directory: `evidence/20261011T043423Z`."


@pytest.mark.parametrize("message,expected", [
    (AO3, "20261011T043423Z"),
    ("Evidence directory: evidence/X", "X"),
    ("Evidence directory: `evidence/X`", "X"),
    ("`Evidence directory: evidence/X`", "X"),
    ("**Evidence directory:** evidence/X", "X"),
    ("_Evidence directory:_ evidence/X", "X"),
    ("Evidence directory: **evidence/X**", "X"),
    ("Evidence directory: _evidence/X_", "X"),
    ("**Evidence directory: evidence/X**", "X"),
    ("- Evidence directory: evidence/X", "X"),
    ("* Evidence directory: evidence/X", "X"),
    ("Evidence directory: evidence/X.", "X"),
    ("Evidence directory: evidence/X,", "X"),
    ("Evidence directory: evidence/X;", "X"),
    ("Evidence directory: evidence/X/", "X"),
    ("Evidence directory: `evidence/X/`.", "X"),
    ("see (Evidence directory: evidence/X)", "X"),
    ("Evidence directory: evidence/X\r\nthanks\r\n", "X"),
    ("Evidence directory: evidence/a/b", "a/b"),
    ("Evidence directory: evidence/2026-10-12T10:10:10Z", "2026-10-12T10:10:10Z"),
    ("Evidence directory: evidence/X\nlater\nEvidence directory: evidence/Y", "Y"),
    ("Evidence directory: evidence/<that directory>", None),
    ("Evidence directory: evidence/<UTC stamp>", None),
    ("end with the line: Evidence directory: evidence/<that directory>", None),
    ("Evidence directory: evidence/..", None),
    ("Evidence directory: evidence/../x", None),
    ("Evidence directory: evidence/a/../b", None),
    ("Evidence directory: evidence/", None),
    ("Evidence directory: evidence/.", None),
    ("Evidence directory: evidence//abs", None),
    ("Evidence directory: /abs/evidence/X", None),
    ("Evidence directory: other/X", None),
    ("Evidence directory: tests/evidence/X", None),
    ("no field here, evidence/X", None),
    ("", None),
    (None, None),
])
def test_named_dir(message: str | None, expected: str | None) -> None:
    assert named_dir(message, "evidence") == expected


def test_named_dir_with_a_nested_declared_directory() -> None:
    assert named_dir("Evidence directory: out/evidence/X", "out/evidence") == "X"


def test_prompt_line_names_the_directory_and_the_field() -> None:
    line = prompt_line(Declaration("evidence", 10))
    assert "evidence/" in line and "Evidence directory: evidence/<that directory>" in line
    assert line.startswith("Evidence: write what proves your change under evidence/")


def test_check_dir_missing_stale_and_fresh() -> None:
    listing = {"X/a.txt": (1, 100), "X/b/c.png": (2, 300), "Y": (1, 50)}
    assert check_dir(listing, "X", 200) is None          # one file is newer
    assert check_dir(listing, "X", 400) == "stale"
    assert check_dir(listing, "Z", 0) == "missing"
    assert check_dir(listing, "Y", 50) is None            # a file equal to the name counts
    assert check_dir(listing, "X/b", 300) is None


def test_flags_in_order_and_only_when_true() -> None:
    assert flags("evidence", True, ["a"], "X", None, []) == []
    assert flags("evidence", True, [], "X", None, []) == ["acceptance wrote no evidence under evidence"]
    assert flags("evidence", False, [], None, None, []) == [
        'no "Evidence directory: evidence/..." line in the agent\'s final message']
    assert flags("evidence", True, [], "X", "missing", ["p"] * 3) == [
        "acceptance wrote no evidence under evidence",
        "the agent's evidence directory evidence/X does not exist",
        "evidence over the cap: 3 files skipped"]
    assert flags("evidence", True, ["a"], "X", "stale", []) == [
        "the agent's evidence directory evidence/X holds nothing written during this run"]


# == summary / map_strings / flags_of ==========================================

def _local(**over: Any) -> dict:
    data: dict[str, Any] = {
        "run_id": "AO-1-r1", "task_id": "AO-1", "dir": "/root/AO-1-r1", "declared": "evidence",
        "files": [{"path": "acceptance/x.txt", "bytes": 3}], "skipped": [], "bytes": 3,
        "acceptance": [{"command": "true", "exit": 0, "note": ""}],
        "results": [{"file": "acceptance/r/result.txt", "lines": ["PASS a", "FAIL b"]}],
        "screenshots": ["acceptance/r/shot.png"], "agent_dir": None, "flags": ["f1"],
    }
    data.update(over)
    return data


def test_summary_of_a_small_manifest_keeps_everything() -> None:
    data = summary(_local())
    assert data["run_id"] == "AO-1-r1" and data["dir"] == "/root/AO-1-r1" and data["declared"] == "evidence"
    assert data["file_count"] == 1 and data["bytes"] == 3 and data["flags"] == ["f1"]
    assert data["acceptance"] == [{"command": "true", "exit": 0, "note": ""}]
    assert data["results"] == [{"file": "acceptance/r/result.txt", "lines": ["PASS a", "FAIL b"],
                               "lines_omitted": 0}]
    assert data["screenshots"] == ["acceptance/r/shot.png"] and data["agent_dir"] is None
    assert data["acceptance_omitted"] == 0 and data["results_omitted"] == 0
    assert data["screenshots_omitted"] == 0


def test_summary_keeps_fail_lines_first_in_file_order_and_counts_the_omitted() -> None:
    lines = [f"PASS p{i}" for i in range(20)]
    lines.insert(17, "FAIL late")
    data = summary(_local(results=[{"file": "acceptance/r/result.txt", "lines": lines}]))
    entry = data["results"][0]
    assert len(entry["lines"]) == 15 and "FAIL late" in entry["lines"]
    assert entry["lines"] == [line for line in lines if line in entry["lines"]]
    assert entry["lines_omitted"] == 6


def test_summary_within_the_cap_omits_no_lines() -> None:
    data = summary(_local())
    assert data["results"][0]["lines_omitted"] == 0


def test_summary_stays_bounded_with_adversarial_content_and_keeps_every_flag() -> None:
    long = "é" * 5000
    local = _local(
        files=[{"path": f"acceptance/{i}", "bytes": 1} for i in range(3000)],
        acceptance=[{"command": "c" * 70000, "exit": 1, "note": "n"} for _ in range(60)],
        results=[{"file": long, "lines": ["FAIL " + long] * 100} for _ in range(50)],
        screenshots=[f"acceptance/{i}-{'s' * 50}.png" for i in range(500)] + ["x" * 301 + ".png"],
        flags=[f"flag {i} " + long for i in range(30)], agent_dir=long, dir=long, declared=long)
    data = summary(local)
    encoded = json.dumps(data, separators=(",", ":")).encode()
    assert len(encoded) <= 60000
    assert data["file_count"] == 3000
    assert len(data["flags"]) == 10 and all(len(f) <= 300 for f in data["flags"])
    assert [f[:7] for f in data["flags"]] == [f"flag {i} "[:7] for i in range(10)]
    assert len(data["acceptance"]) <= 20 and data["acceptance_omitted"] == 60 - len(data["acceptance"])
    assert all(len(a["command"]) <= 300 for a in data["acceptance"])
    assert len(data["results"]) <= 8 and all(len(r["lines"]) <= 15 for r in data["results"])
    assert all(len(line) <= 200 for r in data["results"] for line in r["lines"])
    assert len(data["screenshots"]) <= 20 and data["screenshots_omitted"] >= 481
    assert len(data["agent_dir"]) <= 300 and len(data["dir"]) <= 300 and len(data["declared"]) <= 300


def test_summary_over_the_byte_bound_drops_results_then_screenshots_then_acceptance_never_flags(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence, "SUMMARY_MAX_BYTES", 1200)
    local = _local(
        acceptance=[{"command": f"cmd {i} " + "c" * 280, "exit": 0, "note": ""} for i in range(20)],
        results=[{"file": "f" * 280, "lines": ["PASS " + "x" * 190] * 15} for _ in range(8)],
        screenshots=["s" * 280 + str(i) for i in range(20)],
        flags=["keep me"] * 3)
    data = summary(local)
    assert len(json.dumps(data, separators=(",", ":")).encode()) <= 1200
    assert data["flags"] == ["keep me"] * 3
    assert data["results"] == [] and data["results_omitted"] == 8
    assert data["screenshots"] == [] and data["screenshots_omitted"] == 20
    assert data["acceptance_omitted"] == 20 - len(data["acceptance"])
    assert [a["command"][:6] for a in data["acceptance"]] == [f"cmd {i} " for i in range(len(data["acceptance"]))]


def test_summary_picks_the_fail_bearing_ninth_file_and_drops_a_fail_free_one() -> None:
    results = [{"file": f"acceptance/r{i}/result.txt", "lines": [f"PASS p{i}"]} for i in range(1, 10)]
    results[8]["lines"].append("FAIL nine")
    data = summary(_local(results=results))
    assert [r["file"] for r in data["results"]] == [f"acceptance/r{i}/result.txt" for i in (1, 2, 3, 4, 5, 6, 7, 9)]
    assert data["results_omitted"] == 1
    assert data["results"][-1]["lines"] == ["PASS p9", "FAIL nine"]


def test_summary_carries_the_run_totals_over_every_result_file() -> None:
    results = [{"file": f"acceptance/r{i:02d}/result.txt", "lines": ["PASS a", "PASS b"]} for i in range(1, 10)]
    results[8]["lines"] = ["PASS a", "FAIL z"]
    results[0]["lines"] = ["PASS a"] * 20
    data = summary(_local(results=results))
    assert data["pass_total"] == 20 + 7 * 2 + 1 == 35 and data["fail_total"] == 1
    assert data["pass_total"] == sum(1 for r in results for line in r["lines"] if line.startswith("PASS "))


def test_summary_over_the_byte_bound_drops_agent_result_files_from_the_end_one_at_a_time() -> None:
    cjk = "\u6f22" * 200
    results = [{"file": "acceptance/r/result.txt", "lines": ["FAIL short"]}]
    results += [{"file": f"evidence/x{i}/result.txt", "lines": ["PASS " + cjk] * 15} for i in range(7)]
    data = summary(_local(results=results, screenshots=[], acceptance=[]))
    assert len(json.dumps(data, separators=(",", ":")).encode()) <= 60000
    kept = [r["file"] for r in data["results"]]
    assert kept[0] == "acceptance/r/result.txt" and 1 < len(kept) < 8
    assert kept == [r["file"] for r in results[:len(kept)]]
    assert data["results_omitted"] == 8 - len(kept)


def test_summary_replaces_control_characters_in_every_string() -> None:
    data = summary(_local(
        results=[{"file": "acceptance/r/result.txt", "lines": ["PASS \x1b[31mred\x1b[0m\x07\x7f\x85ok"]}],
        screenshots=["acceptance/\x1b]0;x\x07.png"]))
    assert data["results"][0]["lines"] == ["PASS ?[31mred?[0m???ok"]
    assert data["screenshots"] == ["acceptance/?]0;x?.png"]


def test_named_dir_refuses_a_name_holding_a_control_character() -> None:
    assert named_dir("Evidence directory: evidence/a\x1bb", "evidence") is None
    assert named_dir("Evidence directory: evidence/ab", "evidence") == "ab"


def test_map_strings_walks_values_never_keys() -> None:
    data = {"a": "x", "b": ["y", {"c": "z", "n": 3}], "t": ("q",), "none": None}
    assert map_strings(data, str.upper) == {"a": "X", "b": ["Y", {"c": "Z", "n": 3}], "t": ["Q"],
                                            "none": None}
    assert data["a"] == "x"


def test_flags_of_accepts_dict_or_json_text_and_the_refusal_marker() -> None:
    assert flags_of({"evidence": {"flags": ["a", "b"]}}) == ["a", "b"]
    assert flags_of({"evidence": json.dumps({"flags": ["a"]})}) == ["a"]
    assert flags_of({"evidence": {"flags": ["a"]}, "x": 1}) == ["a"]
    assert flags_of({"evidence": None}) == [] and flags_of({}) == []
    assert flags_of({"evidence": "not json"}) == [] and flags_of({"evidence": "[1]"}) == []
    marker = {"truncated": True, "error": "evidence summary refused by the bus: over 65536 bytes"}
    assert flags_of({"evidence": marker}) == ["evidence summary refused by the bus: over 65536 bytes"]
    assert flags_of({"evidence": json.dumps(marker)}) == [
        "evidence summary refused by the bus: over 65536 bytes"]
    assert flags_of(json.dumps({"flags": ["a"]})) == ["a"]
    assert flags_of({"flags": ["z"]}) == ["z"]


# == collect_local =============================================================

def _worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    (wt / "evidence").mkdir(parents=True)
    return wt


def test_collect_local_captures_acceptance_agent_and_outputs(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    decl = Declaration("evidence", DEFAULT_MAX_BYTES)
    (wt / "evidence" / "old").mkdir()
    (wt / "evidence" / "old" / "keep.txt").write_text("before")
    before = listing_local(str(wt), "evidence")
    since = 1
    (wt / "evidence" / "agent-1").mkdir()
    (wt / "evidence" / "agent-1" / "note.txt").write_text("agent wrote")
    (wt / "evidence" / "stamp").mkdir()
    (wt / "evidence" / "stamp" / "result.txt").write_text("PASS one\nFAIL two\nnoise\n")
    (wt / "evidence" / "stamp" / "s.png").write_bytes(b"png")
    root = tmp_path / "root"
    results = [{"command": "bash check.sh", "exit": 1, "note": "", "output": "out text"},
               {"command": "echo hi", "exit": 0, "note": "", "output": "hi"}]
    commands = ["bash check.sh", "echo hi", "never reached"]
    data = collect_local(str(root), "AO-1-r1", "AO-1", str(wt), decl, before, results, commands,
                         "Evidence directory: evidence/agent-1", since)
    run_dir = root / "AO-1-r1"
    assert run_dir.stat().st_mode & 0o777 == 0o700
    assert (run_dir / "acceptance" / "stamp" / "result.txt").exists()
    assert (run_dir / "acceptance" / "agent-1" / "note.txt").exists()  # acceptance listing also saw it
    assert (run_dir / "agent" / "agent-1" / "note.txt").read_text() == "agent wrote"
    assert not (run_dir / "acceptance" / "old").exists()
    assert (run_dir / "outputs" / "0.txt").read_text() == "out text"
    assert (run_dir / "outputs" / "1.txt").read_text() == "hi"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert (run_dir / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert manifest["run_id"] == "AO-1-r1" and manifest["task_id"] == "AO-1"
    assert [a["exit"] for a in manifest["acceptance"]] == [1, 0, None]
    assert manifest["acceptance"][2] == {"command": "never reached", "exit": None, "note": "not run"}
    assert all("output" not in a for a in manifest["acceptance"])
    assert data["dir"] == str(run_dir) and data["declared"] == "evidence"
    assert data["agent_dir"] == "agent-1" and data["flags"] == []
    assert {"file": "acceptance/stamp/result.txt", "lines": ["PASS one", "FAIL two"],
            "lines_omitted": 0} in data["results"]
    assert "acceptance/stamp/s.png" in data["screenshots"]
    assert data["acceptance"][2]["note"] == "not run"
    assert data["file_count"] >= 5


def test_collect_local_flags_a_missing_stale_or_unnamed_directory_and_empty_acceptance(
        tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    decl = Declaration("evidence", DEFAULT_MAX_BYTES)
    (wt / "evidence" / "old").mkdir()
    (wt / "evidence" / "old" / "f.txt").write_text("x")
    before = listing_local(str(wt), "evidence")
    results = [{"command": "true", "exit": 0, "note": "", "output": ""}]
    far_future = 2 * 10**19 // 3
    stale = collect_local(str(tmp_path / "r1"), "r-1", "t", str(wt), decl, before, results, ["true"],
                          "Evidence directory: evidence/old", far_future)
    assert stale["flags"] == ["acceptance wrote no evidence under evidence",
                              "the agent's evidence directory evidence/old holds nothing written during this run"]
    missing = collect_local(str(tmp_path / "r2"), "r-2", "t", str(wt), decl, before, results, ["true"],
                            "Evidence directory: evidence/nope", 0)
    assert "the agent's evidence directory evidence/nope does not exist" in missing["flags"]
    unnamed = collect_local(str(tmp_path / "r3"), "r-3", "t", str(wt), decl, before, results, ["true"],
                            None, 0)
    assert unnamed["flags"] == ['acceptance wrote no evidence under evidence',
                                'no "Evidence directory: evidence/..." line in the agent\'s final message']


def test_collect_local_applies_the_cap_and_lists_skipped(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    decl = Declaration("evidence", 10)
    before = listing_local(str(wt), "evidence")
    (wt / "evidence" / "a.txt").write_text("12345678")
    (wt / "evidence" / "b.txt").write_text("12345678")
    results = [{"command": "true", "exit": 0, "note": "", "output": "o"}]
    data = collect_local(str(tmp_path / "r"), "r-1", "t", str(wt), decl, before, results, ["true"], None, 0)
    kept = [p for p in (tmp_path / "r" / "r-1" / "acceptance").rglob("*") if p.is_file()]
    assert len(kept) == 1
    assert any(f.startswith("evidence over the cap: 1 files skipped") for f in data["flags"])
    manifest = json.loads((tmp_path / "r" / "r-1" / "manifest.json").read_text())
    assert len(manifest["skipped"]) == 1


def test_collect_local_never_raises_on_oserror(tmp_path: Path) -> None:
    wt = _worktree(tmp_path)
    (wt / "evidence" / "a.txt").write_text("x")
    blocker = tmp_path / "root"
    blocker.write_text("a file where the root should be")
    results = [{"command": "true", "exit": 0, "note": "", "output": ""}]
    data = collect_local(str(blocker), "r-1", "t", str(wt), Declaration("evidence", 100), {}, results,
                         ["true", "second"], None, 0)
    assert "evidence capture failed: " in " ".join(data["flags"])
    assert any(f.startswith("evidence capture failed: ") for f in data["flags"])
    assert data["run_id"] == "r-1"


def test_evidence_root_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence, "DEFAULT_ROOT", "~/.aos/evidence")
    assert evidence.evidence_root({"AOS_EVIDENCE_ROOT": str(tmp_path)}) == str(tmp_path)
    assert evidence.evidence_root({}) == os.path.expanduser("~/.aos/evidence")
    assert evidence.evidence_root({"AOS_EVIDENCE_ROOT": ""}) == os.path.expanduser("~/.aos/evidence")


def test_the_suite_never_resolves_the_operators_evidence_root(isolated_evidence_root: Path) -> None:
    assert evidence.evidence_root({}) == os.path.join(str(isolated_evidence_root), "default")
    assert evidence.evidence_root() == os.environ["AOS_EVIDENCE_ROOT"]
    assert Path(evidence.evidence_root({})).is_relative_to(isolated_evidence_root)
    assert not Path(evidence.evidence_root({})).is_relative_to(Path.home() / ".aos")


# == bus_id ======================================================================

BUS_A = "a" * 32
BUS_B = "b" * 32


class AnswerClient:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def get(self, path: str) -> Any:
        self.asked.append(path)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_bus_id_returns_the_id_of_a_valid_capabilities_answer() -> None:
    client = AnswerClient(httpx.Response(200, json={"evidence": True, "bus_id": "0123456789abcdef" * 2}))
    assert evidence.bus_id(client) == "0123456789abcdef" * 2
    assert client.asked == ["/capabilities"]


@pytest.mark.parametrize("answer", [
    httpx.Response(200, json={"evidence": True}),
    httpx.Response(200, json={"evidence": True, "bus_id": "A" * 32}),
    httpx.Response(200, json={"evidence": True, "bus_id": "a" * 31}),
    httpx.Response(200, json={"evidence": True, "bus_id": "a" * 33}),
    httpx.Response(200, json={"evidence": True, "bus_id": "../x"}),
    httpx.Response(200, json={"evidence": True, "bus_id": "0" * 32 + "/../x"}),
    httpx.Response(200, json={"evidence": True, "bus_id": 12345}),
    httpx.Response(200, json={"evidence": True, "bus_id": None}),
    httpx.Response(200, json={"evidence": False, "bus_id": BUS_A}),
    httpx.Response(200, json=["not", "a", "mapping"]),
    httpx.Response(200, content=b"not json"),
    httpx.Response(404, json={"evidence": True, "bus_id": BUS_A}),
    httpx.Response(500, json={}),
    httpx.ConnectError("down"),
    RuntimeError("boom"),
])
def test_bus_id_is_none_for_anything_else(answer: Any) -> None:
    assert evidence.bus_id(AnswerClient(answer)) is None


# == prune / remove_run ========================================================

class FakeBus:
    def __init__(self, tasks: dict[str, Any]) -> None:
        self.tasks = tasks
        self.asked: list[str] = []

    def get(self, path: str) -> Any:
        self.asked.append(path)
        task_id = path.rsplit("/", 1)[1]
        value = self.tasks.get(task_id)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return httpx.Response(404, json={})
        if isinstance(value, int):
            return httpx.Response(value, json={})
        return httpx.Response(200, json=value)


def _run_dir(root: Path, run_id: str, task_id: str) -> Path:
    directory = root / run_id
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({"run_id": run_id, "task_id": task_id}))
    (directory / "outputs").mkdir()
    (directory / "outputs" / "0.txt").write_text("x")
    return directory


def _task(status: str, **runs: str) -> dict:
    return {"status": status, "runs": [{"id": rid, "status": st} for rid, st in runs.items()]}


class CapabilitiesBus(FakeBus):
    """A FakeBus that also answers /capabilities with a bus id."""

    def __init__(self, tasks: dict[str, Any], bus: str | None = BUS_A) -> None:
        super().__init__(tasks)
        self.bus = bus

    def get(self, path: str) -> Any:
        if path == "/capabilities":
            self.asked.append(path)
            return httpx.Response(200, json={"evidence": True} | ({"bus_id": self.bus} if self.bus else {}))
        return super().get(path)


def test_prune_decisions_over_a_fake_bus(tmp_path: Path) -> None:
    root = tmp_path / "root" / BUS_A
    dirs = {name: _run_dir(root, name, tid) for name, tid in [
        ("t1-r1", "T1"), ("t2-r1", "T2"), ("t3-r1", "T3"), ("t4-r1", "T4"), ("t5-r1", "T5"),
        ("t6-r1", "T6"), ("t7-r1", "T7"), ("t8-r1", "T8"), ("t9-r1", "T9"), ("tA-r1", "TA"),
        ("tB-r1", "TB"), ("tC-r1", "TC")]}
    bus = FakeBus({
        "T1": _task("done", **{"t1-r1": "passed"}),
        "T2": _task("backlog", **{"t2-r1": "rejected"}),
        "T3": _task("cancelled", **{"t3-r1": "cancelled"}),
        "T4": _task("review", **{"t4-r1": "review"}),
        "T5": _task("backlog", **{"t5-r1": "failed"}),
        "T6": _task("blocked", **{"t6-r1": "blocked"}),
        "T7": _task("running", **{"t7-r1": "running"}),
        "T8": _task("backlog", **{"t8-r1": "failed", "t8-r2": "review"}),   # newer run alone removes nothing
        "T9": _task("done", **{"other": "passed"}),                          # unknown run kept
        "TA": httpx.ConnectError("down"),
        "TB": 500,
        "TC": _task("cancelled", **{"tC-r1": "failed"}),                    # task cancelled removes
    })
    bus = CapabilitiesBus(bus.tasks)
    err = io.StringIO()
    removed = prune(bus, str(root.parent), err)
    gone = {name for name, directory in dirs.items() if not directory.exists()}
    assert gone == {"t1-r1", "t2-r1", "t3-r1", "tC-r1"}
    assert removed == 4


def test_prune_ignores_foreign_entries_and_a_manifest_naming_another_run(tmp_path: Path) -> None:
    top = tmp_path / "root"
    root = top / BUS_A
    root.mkdir(parents=True)
    (root / "stray.txt").write_text("x")
    (root / "nomanifest").mkdir()
    bad = root / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text("{nope")
    liar = _run_dir(root, "dir-name", "T1")
    (liar / "manifest.json").write_text(json.dumps({"run_id": "someone-else", "task_id": "T1"}))
    bus = CapabilitiesBus({"T1": _task("done", **{"dir-name": "passed", "someone-else": "passed"})})
    assert prune(bus, str(top), io.StringIO()) == 0
    assert liar.exists() and bad.exists()
    assert prune(bus, str(tmp_path / "absent"), io.StringIO()) == 0


def test_prune_quietly_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    _run_dir(root / BUS_A, "t1-r1", "T1")
    err = io.StringIO()

    def boom(client: Any, root: str, err: Any) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(evidence, "prune", boom)
    prune_quietly(object(), {"AOS_EVIDENCE_ROOT": str(root)}, err)
    assert (root / BUS_A / "t1-r1").exists()
    assert "evidence prune skipped: RuntimeError" in err.getvalue()
    monkeypatch.undo()
    bus = CapabilitiesBus({"T1": _task("done", **{"t1-r1": "passed"})})
    prune_quietly(bus, {"AOS_EVIDENCE_ROOT": str(root)}, err)
    assert not (root / BUS_A / "t1-r1").exists()


def test_prune_scopes_to_this_buses_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    mine = _run_dir(root / BUS_A, "t1-r1", "T1")
    theirs = _run_dir(root / BUS_B, "t1-r1", "T1")
    direct = _run_dir(root, "t1-r1", "T1")
    bus = CapabilitiesBus({"T1": _task("done", **{"t1-r1": "passed"})}, BUS_A)
    assert prune(bus, str(root), io.StringIO()) == 1
    assert not mine.exists() and theirs.exists() and direct.exists()


def test_prune_with_no_bus_id_skips_and_says_so(tmp_path: Path) -> None:
    root = tmp_path / "root"
    kept = _run_dir(root / BUS_A, "t1-r1", "T1")
    err = io.StringIO()
    bus = CapabilitiesBus({"T1": _task("done", **{"t1-r1": "passed"})}, None)
    assert prune(bus, str(root), err) == 0
    assert kept.exists() and "evidence prune skipped: the bus gives no id" in err.getvalue()
    assert bus.asked == ["/capabilities"]


def test_prune_refuses_an_id_that_walks_out_of_the_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _run_dir(root / BUS_A, "t1-r1", "T1")
    victim = _run_dir(tmp_path / "victim-parent", "t1-r1", "T1")
    evil = "0" * 32 + "/../../victim-parent"
    bus = CapabilitiesBus({"T1": _task("done", **{"t1-r1": "passed"})}, evil)
    assert prune(bus, str(root), io.StringIO()) == 0
    assert victim.exists()


def test_prune_makes_no_bus_call_without_a_bus_directory(tmp_path: Path) -> None:
    bus = CapabilitiesBus({})
    assert prune(bus, str(tmp_path / "absent"), io.StringIO()) == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert prune(bus, str(empty), io.StringIO()) == 0
    root = tmp_path / "root"
    direct = _run_dir(root, "t1-r1", "T1")
    (root / "UPPER".ljust(32, "A")).mkdir()
    (root / ("c" * 31)).mkdir()
    assert prune(bus, str(root), io.StringIO()) == 0
    assert direct.exists() and bus.asked == []


def test_remove_run(tmp_path: Path) -> None:
    root = tmp_path / "root"
    directory = _run_dir(root / BUS_A, "t1-r1", "T1")
    other_bus = _run_dir(root / BUS_B, "t1-r1", "T1")
    direct = _run_dir(root, "t1-r1", "T1")
    assert remove_run(str(root), BUS_A, "t1-r1") is True
    assert not directory.exists() and other_bus.exists() and direct.exists()
    assert remove_run(str(root), BUS_A, "t1-r1") is False
    precious = tmp_path / "precious"
    precious.mkdir()
    (root / BUS_A / "link").symlink_to(precious)
    assert remove_run(str(root), BUS_A, "link") is False and precious.exists()
    for bad in ("", ".", "..", "a/b", "../precious"):
        assert remove_run(str(root), BUS_A, bad) is False
    for bad_bus in ("", "..", "A" * 32, "a" * 31, "0" * 32 + "/../" + BUS_B, "../root/" + BUS_B):
        assert remove_run(str(root), bad_bus, "t1-r1") is False
    assert precious.exists() and other_bus.exists() and direct.exists()
