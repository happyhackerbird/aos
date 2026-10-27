"""Unit tests for agent_os.openshell_evidence: listing, final message, exit codes, fetch and collect."""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

from agent_os import evidence, openshell_evidence as oe, openshell_runner as adapter

DECL = evidence.Declaration("evidence", 10_000)


def done(stdout="", rc=0):
    return subprocess.CompletedProcess(["openshell"], rc, stdout, "")


def done_bytes(stdout=b"", rc=0):
    return subprocess.CompletedProcess(["openshell"], rc, stdout, b"")


class Symlink(str):
    """A tar member that is a symbolic link to this target (extract_tar skips it)."""


def make_tgz(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            if isinstance(body, Symlink):
                info.type, info.linkname = tarfile.SYMTYPE, str(body)
                tar.addfile(info)
                continue
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))


def test_listing_runs_the_guarded_script_and_parses(monkeypatch):
    seen = []

    def fake(name, script, **kw):
        seen.append((name, script))
        return done("a/b.txt\t5\t1700000000.5\nbroken line\n")

    monkeypatch.setattr(adapter, "sandbox_exec", fake)
    got = oe.listing("sb", "evidence")
    assert seen == [("sb", evidence.listing_script("evidence"))]
    assert got == {"a/b.txt": (5, 1_700_000_000_500_000_000)}


def test_listing_failure_is_empty(monkeypatch):
    monkeypatch.setattr(adapter, "sandbox_exec", lambda *a, **k: done("x\t1\t2\n", rc=1))
    assert oe.listing("sb", "evidence") == {}

    def boom(*a, **k):
        raise OSError("gone")

    monkeypatch.setattr(adapter, "sandbox_exec", boom)
    assert oe.listing("sb", "evidence") == {}


def claude_json(text: str) -> bytes:
    return json.dumps({"type": "result", "result": text, "session_id": "s"}).encode()


def test_final_message_claude(monkeypatch):
    seen = []

    def fake(name, argv, **kw):
        seen.append(argv)
        return done_bytes(claude_json("all done"))

    monkeypatch.setattr(adapter, "sandbox_exec_direct_bytes", fake)
    assert oe.final_message("sb", "claude") == "all done"
    assert seen == [["/usr/bin/tail", "-c", "5000002", "/sandbox/harness.out"]]


@pytest.mark.parametrize("boundary", ["complete", "partial", "exact-cap", "cap-plus-one"])
def test_final_message_claude_tail_boundary_keeps_a_complete_first_line(monkeypatch, boundary):
    cap = oe.FINAL_MESSAGE_CAP
    final = claude_json("last result") + b"\n"
    first = claude_json("first result") + b"\n"
    if boundary == "complete":
        # The returned cap+2 window begins with the newline before a complete first line.
        retained = first + b" " * (cap + 1 - len(first) - len(final) - 1) + b"\n" + final
        transcript = b"discarded prefix\n" + retained
        expected = "last result"
        assert transcript[-(cap + 2):].startswith(b"\n" + first)
    elif boundary == "partial":
        retained = b"x" * (cap + 1 - len(final) - 1) + b"\n" + final
        transcript = b"discarded prefix" + retained
        expected = "last result"
        assert transcript[-(cap + 2):][:1] != b"\n"
    elif boundary == "exact-cap":
        expected = "x" * (cap - len(claude_json("")))
        result_line = claude_json(expected)
        assert len(result_line) == cap
        transcript = b"discarded prefix\n" + result_line + b"\n"
        assert transcript[-(cap + 2):][:1] == b"\n"
    else:
        # Without the extra preceding byte, even the complete first line must remain intact.
        expected = "x" * (cap + 1 - len(claude_json("")) - 1)
        transcript = claude_json(expected) + b"\n"
        assert len(transcript) == cap + 1

    def fake(name, argv, *, timeout):
        assert argv == ["/usr/bin/tail", "-c", "5000002", "/sandbox/harness.out"]
        assert timeout == 120
        return done_bytes(transcript[-(cap + 2):])

    monkeypatch.setattr(adapter, "sandbox_exec_direct_bytes", fake)
    assert oe.final_message("sb", "claude") == expected


@pytest.mark.parametrize("raw", [b"not json", b"\xff\xfe\x00", b""])
def test_final_message_unparseable_is_none(monkeypatch, raw):
    monkeypatch.setattr(adapter, "sandbox_exec_direct_bytes", lambda *a, **k: done_bytes(raw))
    assert oe.final_message("sb", "claude") is None
    assert oe.final_message("sb", "codex") is None


def test_final_message_failed_exec_is_none(monkeypatch):
    monkeypatch.setattr(adapter, "sandbox_exec_direct_bytes", lambda *a, **k: done_bytes(b"", rc=1))
    assert oe.final_message("sb", "claude") is None


def codex_lines(text: str) -> bytes:
    events = [{"type": "thread.started", "thread_id": "t"},
              {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
              {"type": "turn.completed"}]
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode()


def test_final_message_codex_small_and_truncated(monkeypatch):
    seen = []

    def small(name, argv, **kw):
        seen.append(argv)
        return done_bytes(codex_lines("hello"))

    monkeypatch.setattr(adapter, "sandbox_exec_direct_bytes", small)
    assert oe.final_message("sb", "codex") == "hello"
    assert seen == [["/usr/bin/tail", "-c", "5000001", "/sandbox/harness.out"]]
    # a file larger than the cap: the tail starts mid-line, so the first partial line is dropped
    body = codex_lines("final words")
    padded = b'{"type":"item.completed","item":{"type":"agent_message","text":"' + b"y" * (5_000_001 - len(body) - 20)
    raw = padded + b"\n" + body
    assert len(raw) > 5_000_000
    monkeypatch.setattr(adapter, "sandbox_exec_direct_bytes", lambda *a, **k: done_bytes(raw))
    assert oe.final_message("sb", "codex") == "final words"


def test_evidence_readers_use_direct_argv(monkeypatch):
    # F137 (D2): the evidence reads run absolute root-owned binaries through the direct form, never a shell.
    shells = []

    def shell(*a, **k):
        shells.append(a)
        raise AssertionError(f"a shell exec: {a}")

    seen = []
    texts = {("/bin/cat", "/sandbox/harness.started"): "1000\n",
             ("/bin/cat", "/sandbox/accept-before.txt"): "a.txt\t1\t2.0\n",
             ("/bin/cat", "/sandbox/accept-0.rc"): "0\n", ("/bin/cat", "/sandbox/accept-1.rc"): "3\n",
             ("/usr/bin/tail", "-c", str(evidence.OUTPUT_TAIL_BYTES), "/sandbox/accept-0.out"): "collected\n"}

    def direct(name, argv, **kw):
        seen.append((name, tuple(argv), kw))
        return done(texts[tuple(argv)])

    def direct_bytes(name, argv, **kw):
        seen.append((name, tuple(argv), kw))
        return done_bytes(claude_json("all done") if argv[2] == str(oe.FINAL_MESSAGE_CAP + 2) else codex_lines("hello"))

    monkeypatch.setattr(adapter, "sandbox_exec", shell)
    monkeypatch.setattr(adapter, "sandbox_exec_bytes", shell)
    monkeypatch.setattr(adapter, "sandbox_exec_direct", direct)
    monkeypatch.setattr(adapter, "sandbox_exec_direct_bytes", direct_bytes, raising=False)
    assert oe.read_started("sb") == 1000
    assert oe.read_before("sb") == {"a.txt": (1, 2_000_000_000)}
    assert oe.exit_codes("sb", 2) == [0, 3]
    assert oe.outputs("sb", 1) == ["collected\n"]
    assert oe.final_message("sb", "claude") == "all done"
    assert oe.final_message("sb", "codex") == "hello"
    assert seen == [
        ("sb", ("/bin/cat", "/sandbox/harness.started"), {"timeout": 60}),
        ("sb", ("/bin/cat", "/sandbox/accept-before.txt"), {"timeout": 60}),
        ("sb", ("/bin/cat", "/sandbox/accept-0.rc"), {"timeout": 60}),
        ("sb", ("/bin/cat", "/sandbox/accept-1.rc"), {"timeout": 60}),
        ("sb", ("/usr/bin/tail", "-c", "1048576", "/sandbox/accept-0.out"), {"timeout": 120}),
        ("sb", ("/usr/bin/tail", "-c", "5000002", "/sandbox/harness.out"), {"timeout": 120}),
        ("sb", ("/usr/bin/tail", "-c", "5000001", "/sandbox/harness.out"), {"timeout": 120}),
    ]
    assert shells == []


def test_exit_codes_read_each_rc_file(monkeypatch):
    answers = {"/sandbox/accept-0.rc": done("0\n"), "/sandbox/accept-1.rc": done("3\n"),
               "/sandbox/accept-2.rc": done("", rc=1)}

    def direct(name, argv, **k):
        assert argv[0] == "/bin/cat" and len(argv) == 2
        return answers[argv[1]]

    monkeypatch.setattr(adapter, "sandbox_exec_direct", direct)
    assert oe.exit_codes("sb", 3) == [0, 3, None]


def test_fetch_tars_behind_the_guard_and_extracts(monkeypatch, tmp_path):
    calls = []
    state = tmp_path / "state"
    state.mkdir()

    def fake_openshell(*args, check=True, timeout=600):
        calls.append(args)
        if args[:2] == ("sandbox", "download"):
            make_tgz(Path(args[4]), {"a.txt": b"hello", "sub/b.txt": b"bb"})
        return done()

    def fake_exec(name, script, **kw):
        calls.append(("exec", name, script))
        return done()

    monkeypatch.setattr(adapter, "openshell", fake_openshell)
    monkeypatch.setattr(adapter, "sandbox_exec", fake_exec)
    dest = tmp_path / "out" / "acceptance"
    skipped = oe.fetch("sb", "evidence", ["a.txt", "sub/b.txt"], state, str(dest), 1000)
    assert skipped == []
    assert (dest / "a.txt").read_bytes() == b"hello" and (dest / "sub" / "b.txt").read_bytes() == b"bb"
    tar = next(c for c in calls if c[0] == "exec")[2]
    assert "--no-recursion --verbatim-files-from -T" in tar and "realpath" in tar and "[ ! -L" in tar
    assert tar.index("realpath") < tar.index("tar ")
    assert not (state / "evidence.tgz").exists()


def test_fetch_nothing_does_not_call_the_sandbox(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("no call expected")

    monkeypatch.setattr(adapter, "openshell", boom)
    monkeypatch.setattr(adapter, "sandbox_exec", boom)
    assert oe.fetch("sb", "evidence", [], tmp_path, str(tmp_path / "d"), 10) == []


class Tree:
    """A scripted sandbox tree for collect: listings by sandbox name, files by download."""

    def __init__(self, tmp_path, *, job_listing, acc_listing, before, tgz_files, rcs=("0",), outs=("ok",)):
        self.job_listing, self.acc_listing, self.before = job_listing, acc_listing, before
        self.tgz_files, self.rcs, self.outs = tgz_files, rcs, outs
        self.listed_paths: list[list[str]] = []
        self.events: list[tuple] = []

    def lines(self, listing):
        return "".join(f"{p}\t{s}\t{m / 1e9}\n" for p, (s, m) in listing.items())

    def sandbox_exec(self, name, script, **kw):
        if script == evidence.listing_script("evidence"):
            return done(self.lines(self.job_listing if name == "job" else self.acc_listing))
        if script.startswith(("cat /sandbox/", "tail -c ")):
            raise AssertionError(f"an evidence read through a shell: {script}")  # F137: the four reads are direct
        self.events.append(("exec", name, script))
        return done()

    def sandbox_exec_direct(self, name, argv, **kw):
        argv = tuple(argv)
        if argv == ("/bin/cat", "/sandbox/harness.started"):
            return done("1000\n")
        if argv == ("/bin/cat", "/sandbox/accept-before.txt"):
            return done(self.lines(self.before))
        rc = re.fullmatch(r"/sandbox/accept-(\d+)\.rc", argv[-1])
        if len(argv) == 2 and argv[0] == "/bin/cat" and rc:
            i = int(rc.group(1))
            return done(self.rcs[i] + "\n") if i < len(self.rcs) else done("", rc=1)
        out = re.fullmatch(r"/sandbox/accept-(\d+)\.out", argv[-1])
        if argv[:3] == ("/usr/bin/tail", "-c", str(evidence.OUTPUT_TAIL_BYTES)) and len(argv) == 4 and out:
            i = int(out.group(1))
            return done(self.outs[i] if i < len(self.outs) else "")
        raise AssertionError(f"unexpected direct exec: {argv}")

    def openshell(self, *args, check=True, timeout=600):
        if args[:2] == ("sandbox", "upload"):
            self.listed_paths.append(Path(args[3]).read_text().splitlines())
        if args[:2] == ("sandbox", "download"):
            self.events.append(("download", args[2]))
            make_tgz(Path(args[4]), self.tgz_files[args[2]])
        return done()


def collect_args(tmp_path, _tree, **over):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    args = dict(root=str(tmp_path / "root"), run_id="AO-1-r1", task_id="AO-1", decl=DECL, job="job",
                tree="acc", fresh=True, state=state, commands=["pytest -q"],
                message="done\nEvidence directory: evidence/run1\n", agent_listing={"run1/p.txt": (2, 5000)})
    args.update(over)
    return args


def test_collect_fresh_attributes_only_paths_absent_from_before(monkeypatch, tmp_path):
    tree = Tree(tmp_path, job_listing={"run1/p.txt": (2, 5000)},
                acc_listing={".gitkeep": (0, 10), "result.txt": (30, 9000)}, before={".gitkeep": (0, 10)},
                tgz_files={"acc": {"result.txt": b"PASS one\nFAIL two\n"}, "job": {"run1/p.txt": b"hi"}})
    monkeypatch.setattr(adapter, "sandbox_exec", tree.sandbox_exec)
    monkeypatch.setattr(adapter, "sandbox_exec_direct", tree.sandbox_exec_direct)
    monkeypatch.setattr(adapter, "openshell", tree.openshell)
    summary = oe.collect(**collect_args(tmp_path, tree))
    assert tree.listed_paths[0] == ["result.txt"]            # .gitkeep unchanged: not acceptance's
    assert tree.listed_paths[1] == ["run1/p.txt"]
    assert summary["flags"] == [] and summary["agent_dir"] == "run1"
    assert summary["acceptance"] == [{"command": "pytest -q", "exit": 0, "note": ""}]
    host = tmp_path / "root" / "AO-1-r1"
    assert (host / "acceptance" / "result.txt").read_bytes() == b"PASS one\nFAIL two\n"
    assert (host / "agent" / "run1" / "p.txt").read_bytes() == b"hi"
    assert (host / "outputs" / "0.txt").read_text() == "ok"
    assert json.loads((host / "manifest.json").read_text())["run_id"] == "AO-1-r1"


def test_collect_flags_nothing_written_and_stale(monkeypatch, tmp_path):
    tree = Tree(tmp_path, job_listing={"run1/p.txt": (2, 500)}, acc_listing={".gitkeep": (0, 10)},
                before={".gitkeep": (0, 10)},
                tgz_files={"job": {"run1/p.txt": b"hi"}})
    monkeypatch.setattr(adapter, "sandbox_exec", tree.sandbox_exec)
    monkeypatch.setattr(adapter, "sandbox_exec_direct", tree.sandbox_exec_direct)
    monkeypatch.setattr(adapter, "openshell", tree.openshell)
    summary = oe.collect(**collect_args(tmp_path, tree, agent_listing={"run1/p.txt": (2, 500)}))
    assert summary["flags"] == ["acceptance wrote no evidence under evidence",
                                "the agent's evidence directory evidence/run1 holds nothing written during this run"]


def test_collect_timeout_marks_commands_without_a_result(monkeypatch, tmp_path):
    tree = Tree(tmp_path, job_listing={}, acc_listing={}, before={}, tgz_files={}, rcs=("0",), outs=("first", "partial"))
    monkeypatch.setattr(adapter, "sandbox_exec", tree.sandbox_exec)
    monkeypatch.setattr(adapter, "sandbox_exec_direct", tree.sandbox_exec_direct)
    monkeypatch.setattr(adapter, "openshell", tree.openshell)
    summary = oe.collect(**collect_args(tmp_path, tree, commands=["a", "b", "c"], timed_out=True, message=None,
                                        agent_listing={}))
    assert summary["acceptance"] == [{"command": "a", "exit": 0, "note": ""},
                                     {"command": "b", "exit": None, "note": "timeout or not reached"},
                                     {"command": "c", "exit": None, "note": "timeout or not reached"}]


def test_collect_in_job_uses_the_given_results_and_before(monkeypatch, tmp_path):
    tree = Tree(tmp_path, job_listing={"r/x.txt": (4, 7000), "new.txt": (3, 8000)}, acc_listing={}, before={},
                tgz_files={"job": {"new.txt": b"abc", "r/x.txt": b"1234"}})
    monkeypatch.setattr(adapter, "sandbox_exec", tree.sandbox_exec)
    monkeypatch.setattr(adapter, "sandbox_exec_direct", tree.sandbox_exec_direct)
    monkeypatch.setattr(adapter, "openshell", tree.openshell)
    results = [{"command": "pytest -q", "exit": 1, "note": "", "output": "boom"}]
    summary = oe.collect(**collect_args(
        tmp_path, tree, tree="job", fresh=False, results=results,
        before={"r/x.txt": (4, 7000)}, agent_listing={"r/x.txt": (4, 7000)}, message="Evidence directory: evidence/r"))
    assert tree.listed_paths[0] == ["new.txt"]
    assert summary["acceptance"][0]["exit"] == 1
    assert (tmp_path / "root" / "AO-1-r1" / "outputs" / "0.txt").read_text() == "boom"


def every_file(top: Path) -> dict[str, bytes]:
    return {str(path.relative_to(top)): path.read_bytes() for path in sorted(top.rglob("*")) if path.is_file()}


def test_collect_redacts_everything_it_writes(monkeypatch, tmp_path):
    # F111 (D5, D10 a): the outputs, the fetched files and the manifest's result lines are redacted before the disk;
    # the manifest's sizes are the disk's, and the summary's bytes their sum.
    result_txt = b"PASS connected to postgresql://aos:pw-secret@h/aos\n"
    proof = b"the agent printed pw-secret twice: pw-secret\n"
    tree = Tree(tmp_path, job_listing={"run1/p.txt": (len(proof), 5000)},
                acc_listing={".gitkeep": (0, 10), "result.txt": (len(result_txt), 9000)}, before={".gitkeep": (0, 10)},
                tgz_files={"acc": {"result.txt": result_txt}, "job": {"run1/p.txt": proof}},
                outs=("url postgresql://aos:pw-secret@h/aos\n",))
    monkeypatch.setattr(adapter, "sandbox_exec", tree.sandbox_exec)
    monkeypatch.setattr(adapter, "sandbox_exec_direct", tree.sandbox_exec_direct)
    monkeypatch.setattr(adapter, "openshell", tree.openshell)
    summary = oe.collect(**collect_args(tmp_path, tree, agent_listing={"run1/p.txt": (len(proof), 5000)}),
                         redact=lambda t: t.replace("pw-secret", "***"),
                         redact_bytes=lambda b: b.replace(b"pw-secret", b"***"))
    host = tmp_path / "root" / "AO-1-r1"
    written = every_file(host)
    assert sorted(written) == ["acceptance/result.txt", "agent/run1/p.txt", "manifest.json", "outputs/0.txt"]
    assert [name for name, body in written.items() if b"pw-secret" in body] == []   # grep -r pw-secret <run dir>
    for name in ("outputs/0.txt", "acceptance/result.txt", "manifest.json"):
        assert b"***" in written[name], name
    assert written["acceptance/result.txt"] == b"PASS connected to postgresql://aos:***@h/aos\n"
    manifest = json.loads(written["manifest.json"])
    assert manifest["results"] == [{"file": "acceptance/result.txt", "lines": ["PASS connected to postgresql://aos:***@h/aos"]}]
    assert [f["path"] for f in manifest["files"]] == ["outputs/0.txt", "acceptance/result.txt", "agent/run1/p.txt"]
    for entry in manifest["files"]:
        assert entry["bytes"] == os.path.getsize(host / entry["path"]), entry
    on_disk = sum(len(body) for name, body in written.items() if name != "manifest.json")
    assert manifest["bytes"] == summary["bytes"] == on_disk
    assert manifest["skipped"] == [] and summary["flags"] == []


def test_collect_skips_a_kept_name_that_never_reached_the_disk(monkeypatch, tmp_path):
    # F111 (D5 ii, the parent's F79): fetch ignores tar's exit and extract_tar skips links, so a kept name may never be
    # written; it moves to `skipped`, never into `files` and never into a capture failure.
    tree = Tree(tmp_path, job_listing={"run1/p.txt": (2, 5000)},
                acc_listing={"result.txt": (9, 9000), "gone.txt": (4, 9000), "link.txt": (10, 9000)}, before={},
                tgz_files={"acc": {"result.txt": b"PASS one\n", "link.txt": Symlink("result.txt")},
                           "job": {"run1/p.txt": b"hi"}})
    monkeypatch.setattr(adapter, "sandbox_exec", tree.sandbox_exec)
    monkeypatch.setattr(adapter, "sandbox_exec_direct", tree.sandbox_exec_direct)
    monkeypatch.setattr(adapter, "openshell", tree.openshell)
    summary = oe.collect(**collect_args(tmp_path, tree))
    host = tmp_path / "root" / "AO-1-r1"
    assert tree.listed_paths[0] == ["gone.txt", "link.txt", "result.txt"]
    manifest = json.loads((host / "manifest.json").read_text())
    assert sorted(manifest["skipped"]) == ["acceptance/gone.txt", "acceptance/link.txt"]
    assert [f["path"] for f in manifest["files"]] == ["outputs/0.txt", "acceptance/result.txt", "agent/run1/p.txt"]
    assert manifest["bytes"] == summary["bytes"] == 2 + 9 + 2
    assert not any(flag.startswith("evidence capture failed") for flag in summary["flags"])
    assert summary["flags"] == ["evidence over the cap: 2 files skipped"]
    assert not (host / "acceptance" / "gone.txt").exists() and not os.path.lexists(host / "acceptance" / "link.txt")


def test_collect_never_raises_and_flags_the_failure(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("sandbox vanished")

    monkeypatch.setattr(adapter, "sandbox_exec", boom)
    monkeypatch.setattr(adapter, "openshell", boom)
    summary = oe.collect(**collect_args(tmp_path, None))
    assert len(summary["flags"]) == 1 and summary["flags"][0].startswith("evidence capture failed: ")
