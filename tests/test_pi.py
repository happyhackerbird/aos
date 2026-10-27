"""Pi-family adapter: settled event contract, flavours and capabilities, worker auth, redaction, bundle checks.

No real Pi, omp, network or credential is used; tokens are synthetic.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from agent_os import pi

SID = "0b1c2d3e-4f50-4612-8a7b-9c0d1e2f3a4b"
OTHER = "6f1d2c3b-4a59-4876-8a9b-0c1d2e3f4a5b"


def line(record: dict) -> bytes:
    return json.dumps(record).encode() + b"\n"


def assistant(stop: str | None = "stop", *texts: str) -> dict:
    message = {"role": "assistant", "content": [{"type": "thinking", "thinking": "plan"}]
               + [{"type": "text", "text": t} for t in (texts or ("done",))]}
    if stop is not None:
        message["stopReason"] = stop
    return message


def stream(*records: dict, session: str | None = SID) -> bytes:
    head = [line({"type": "session", "version": 3, "id": session, "cwd": "/sandbox/repo"})] if session else []
    return b"".join(head + [line(r) for r in records])


def ok_run(message: dict | None = None, *after: dict) -> list[dict]:
    message = message or assistant()
    return [{"type": "agent_start"}, {"type": "turn_start"}, {"type": "message_start", "message": message},
            {"type": "message_update"}, {"type": "message_end", "message": message},
            {"type": "turn_end", "message": message},
            {"type": "agent_end", "messages": [{"role": "user", "content": []}, message]}, *after,
            {"type": "agent_settled", "aborted": False}]


# -- parser -----------------------------------------------------------------------------------

def test_success_multiline_text_and_thinking():
    result = pi.parse_output(stream(*ok_run(assistant("stop", "line one\n", "line two"))), 0, SID)
    assert result == pi.PiResult("line one\nline two", SID, None, "stop", True)


def test_success_without_expected_session_uses_observed():
    result = pi.parse_output(stream(*ok_run(), session=OTHER), 0)
    assert result.error is None and result.session_id == OTHER


def test_text_tail_is_bounded():
    result = pi.parse_output(stream(*ok_run(assistant("stop", "x" * 30000 + "END"))), 0, SID)
    assert len(result.text) == 30003  # the parser keeps the full text; the caller redacts, then cuts
    tail = pi.safe_tail(result.text, pi.TEXT_TAIL, None)
    assert len(tail) == pi.TEXT_TAIL and tail.endswith("END")


@pytest.mark.parametrize("stop, phrase", [
    ("error", "assistant stopped with error"), ("aborted", "assistant stopped with aborted"),
    ("length", "assistant stopped with length"), ("toolUse", "assistant stopped with toolUse"),
    ("weird", "stop reason is missing or unknown"), (None, "stop reason is missing or unknown"),
])
def test_model_failures_with_exit_zero_fail(stop, phrase):
    result = pi.parse_output(stream(*ok_run(assistant(stop))), 0, SID)
    assert result.text is None and phrase in result.error and result.session_id == SID


def test_blank_text_fails():
    result = pi.parse_output(stream(*ok_run(assistant("stop", "   \n"))), 0, SID)
    assert result.error == "pi: assistant final message has no text"


def test_agent_end_without_settlement_fails():
    records = ok_run()[:-1]
    result = pi.parse_output(stream(*records), 0, SID)
    assert "not settled" in result.error and result.session_id == SID and result.settled is False


def test_aborted_settlement_fails():
    records = ok_run()
    records[-1] = {"type": "agent_settled", "aborted": True}
    assert pi.parse_output(stream(*records), 0, SID).error == "pi: agent run was aborted"


def test_last_assistant_failure_after_earlier_success():
    first = ok_run()[:-1]
    second = ok_run(assistant("error"))
    result = pi.parse_output(stream(*first, *second), 0, SID)
    assert "stopped with error" in result.error


def test_retry_recovery_succeeds():
    failed = ok_run(assistant("error"))[:-1]
    retry = [{"type": "auto_retry_start"}, *ok_run(assistant("stop", "recovered"))]
    result = pi.parse_output(stream(*failed, *retry), 0, SID)
    assert result.error is None and result.text == "recovered"


@pytest.mark.parametrize("renewed", sorted(pi.RENEWED))
def test_renewed_activity_after_end_invalidates(renewed):
    records = ok_run(None, {"type": renewed})
    result = pi.parse_output(stream(*records), 0, SID)
    assert result.error is not None and "event contract" in result.error


def test_unknown_event_tolerated_but_cannot_be_terminal():
    tolerated = ok_run(None, {"type": "custom_note"})
    assert pi.parse_output(stream(*tolerated), 0, SID).error is None
    trailing = ok_run() + [{"type": "custom_note"}]
    assert "not settled" in pi.parse_output(stream(*trailing), 0, SID).error


def test_no_assistant_message_fails():
    records = ok_run()
    records[-2] = {"type": "agent_end", "messages": [{"role": "user", "content": []}]}
    assert "no final assistant" in pi.parse_output(stream(*records), 0, SID).error


@pytest.mark.parametrize("returncode, phrase", [(1, "harness exited 1"), (124, "harness exited 124"),
                                                (None, "did not exit")])
def test_nonzero_or_missing_exit_fails_even_with_valid_output(returncode, phrase):
    result = pi.parse_output(stream(*ok_run()), returncode, SID)
    assert phrase in result.error and result.text is None and result.session_id == SID and result.settled


def test_session_header_rules():
    assert "no session header" in pi.parse_output(stream(*ok_run(), session=None), 0).error
    bad = pi.parse_output(stream(*ok_run(), session="not-a-uuid"), 0)
    assert "canonical UUID" in bad.error and bad.session_id is None
    upper = pi.parse_output(stream(*ok_run(), session=SID.upper()), 0)
    assert upper.error is not None and upper.session_id is None
    mismatch = pi.parse_output(stream(*ok_run(), session=OTHER), 0, SID)
    assert "differs" in mismatch.error and mismatch.session_id == OTHER  # observed, not manufactured
    repeated = stream({"type": "session", "id": SID}, *ok_run())
    assert "repeated session header" in pi.parse_output(repeated, 0, SID).error
    nothing = pi.parse_output(b"", 1, SID)
    assert nothing.session_id is None  # requested id is never manufactured


@pytest.mark.parametrize("tail, phrase", [
    (b'{"type": "agent_end", "messa', "malformed or truncated"),
    (b"\xff\xfe garbage\n", "invalid UTF-8"),
    (b"[1, 2]\n", "record without a type"),
    (b'{"type": ""}\n', "record without a type"),
])
def test_malformed_records_preserve_session(tail, phrase):
    raw = stream(*ok_run()[:3]) + tail
    result = pi.parse_output(raw, 0, SID)
    assert phrase in result.error and result.session_id == SID and result.text is None


def test_split_multibyte_at_cap_fails_without_raising():
    raw = stream(*ok_run()).replace(b'"done"', '"ééé"'.encode())
    cut = raw[: raw.index("é".encode()) + 1]  # the read cap split a code point
    result = pi.parse_output(cut, 0, SID)
    assert result.error is not None and result.session_id == SID


def test_oversized_output_fails_with_session():
    raw = stream(*ok_run()) + b" " * pi.RAW_CAP
    result = pi.parse_output(raw, 0, SID)
    assert "exceeds the 8 MiB cap" in result.error and result.session_id == SID


def test_foreign_stream_gets_fixed_contract_diagnostic():
    raw = b'{"event":"start"}\n{"event":"done","ok":true}\n'
    result = pi.parse_output(raw, 0)
    assert result.error.startswith("pi: output did not match the Pi event contract")
    other = pi.parse_output(line({"type": "message", "text": "hello"}), 0)
    assert "output did not match the Pi event contract" in other.error


def test_diagnostics_never_echo_payloads():
    secret = "sk-very-secret-error-payload"
    records = ok_run(assistant("error", secret))
    records[-1] = {"type": "agent_settled", "aborted": False, "detail": secret}
    result = pi.parse_output(stream(*records), 0, SID)
    assert secret not in result.error and len(result.error) < 200


# -- omp event contract (measured live 2026-10-12: omp 18.8.3 emits no agent_settled) -------------

OMP_SID = "01a130e0-bae4-7154-9f37-4866d4655606"  # omp session ids are UUIDv7, still canonical form


def omp_run(message: dict | None = None, *after: dict) -> list[dict]:
    """The measured omp 18.8.3 shape: an advisor event after the header, no agent_settled."""
    return [{"type": "advisor_cost_changed"}, *ok_run(message, *after)[:-1]]


def test_omp_parser_success_ends_at_agent_end_and_exit_zero():
    records = omp_run(None, {"type": "notice", "level": "warning", "source": "auth", "message": "m"})
    result = pi.parse_omp_output(stream(*records, session=OMP_SID), 0)
    assert result == pi.PiResult("done", OMP_SID, None, "stop", True)


def test_omp_parser_measured_auth_failure_shape_fails_and_keeps_session():
    failed = {"role": "assistant", "content": [], "stopReason": "error", "errorStatus": 401}
    result = pi.parse_omp_output(stream(*omp_run(failed), session=OMP_SID), 1)
    assert result.text is None and result.session_id == OMP_SID and result.error == "pi: harness exited 1"
    assert result.stop_reason == "error"


@pytest.mark.parametrize("renewed", sorted(pi.RENEWED))
def test_omp_parser_renewed_activity_after_end_fails(renewed):
    result = pi.parse_omp_output(stream(*omp_run(None, {"type": renewed}), session=OMP_SID), 0)
    assert "not ended by a final agent_end" in result.error and result.session_id == OMP_SID


def test_omp_parser_keeps_every_other_rule():
    assert "stopped with error" in pi.parse_omp_output(stream(*omp_run(assistant("error")), session=OMP_SID), 0).error
    assert "no session header" in pi.parse_omp_output(stream(*omp_run(), session=None), 0).error
    assert "did not exit" in pi.parse_omp_output(stream(*omp_run(), session=OMP_SID), None).error
    header_only = pi.parse_omp_output(stream({"type": "advisor_cost_changed"}, session=OMP_SID), 0)
    assert "not ended by a final agent_end" in header_only.error


def test_flavour_parsers_differ_only_in_settlement():
    assert pi.FLAVOURS["omp"].parser is pi.parse_omp_output and pi.FLAVOURS["pi"].parser is pi.parse_output
    assert "not settled" in pi.parse_output(stream(*omp_run(), session=OMP_SID), 0).error


# -- flavours and capabilities ------------------------------------------------------------------

def test_flavour_table_pi_and_omp():
    p = pi.invocation("pi", bin="pi", provider="openai-codex", model="gpt-6-luna", session_id=SID, harness_timeout=60)
    assert p.command.startswith("PI_CODING_AGENT_DIR=/tmp/pi-agent pi --print --mode json --provider openai-codex")
    assert p.expected_session_id == SID and "--session-id" in p.required_flags and "--offline" in p.required_flags
    p = pi.invocation("pi", bin="pi", provider="openai-codex", model="gpt-6-luna", session_id=SID,
                      harness_timeout=60, comms=True)
    assert "--no-mcp" not in p.command
    assert "--no-mcp" not in p.required_flags
    assert "-e" in p.required_flags
    assert p.command.endswith(" -e /usr/local/lib/aos-agent/aos_pi.ts")
    assert " -e builtin:mcp " in p.command
    o = pi.invocation("omp", bin="/usr/local/bin/omp", provider=None, model="openai-codex/gpt-6-luna", session_id=SID,
                      harness_timeout=60)
    assert o.expected_session_id is None and "--session-id" not in o.command and SID not in o.command
    assert o.required_flags == ("--print", "--mode", "--model", "--session-dir", "--no-extensions", "--no-skills",
                                "--no-rules", "--no-title", "--max-time", "--approval-mode")
    # omp's native task tool (subagents) is on, as on Claude: no allowlist, so omp's default set (U6 probe, finding 3)
    assert o.command.endswith(" --max-time 60 --approval-mode=yolo") and "--tools" not in o.command
    # comms needs an allowlist for the aos tools (finding 3); it keeps the default set, task included. The list is
    # fixed here, never read from the module.
    o = pi.invocation("omp", bin="omp", provider=None, model="openai-codex/gpt-6-luna", session_id=None,
                      harness_timeout=60, comms=True)
    assert o.command.split("--tools=", 1)[1].split(",")[:11] == ["read", "bash", "edit", "eval", "glob", "grep", "wait",
                                                                  "todo", "write", "web_search", "task"]
    assert "--tools" in o.required_flags and "-e" in o.required_flags
    with pytest.raises(ValueError):
        pi.invocation("pi", bin="pi", provider=None, model="m", session_id=None, harness_timeout=60)


@pytest.mark.parametrize("value, ok", [("pi", True), ("omp", True), ("/opt/pi/bin/pi", True), ("-pi", False),
                                       ("pi x", False), ("../pi", False), ("/opt/../pi", False), ("pi;id", False)])
def test_bin_validation(value, ok):
    if ok:
        assert pi.check_bin(value) == value
    else:
        with pytest.raises(ValueError):
            pi.check_bin(value)


def test_capabilities_version_missing_and_warnings():
    required = ("--print", "--no-context-files")
    version, missing, warnings = pi.capabilities("pi", "1.1.0\n", "--print\n--no-context-files\n", required)
    assert (version, missing, warnings) == ("1.1.0", [], [])
    version, missing, warnings = pi.capabilities("pi", "pi v1.3.0", "--print\n--no-context-files-x\n", required)
    assert version == "1.3.0" and missing == ["--no-context-files"] and "untested" in warnings[0]
    version, missing, warnings = pi.capabilities("omp", "", "-p, --print\n", ("--print",))
    assert version is None and missing == [] and "not detected" in warnings[0]
    _, missing, _ = pi.capabilities("omp", "omp v18.8.3", "--approval-mode=<value>\n", ("--approval-mode",))
    assert missing == []


# -- worker auth --------------------------------------------------------------------------------

def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def jwt(exp: float) -> str:
    return f"{b64({'alg': 'none'})}.{b64({'exp': exp})}.c2lnbmF0dXJl"


def auth_file(tmp_path: Path, entry: dict | None = None, **top) -> Path:
    now = time.time()
    base = {"type": "oauth", "access": jwt(now + 86400), "refresh": "rt-real-refresh-secret",
            "expires": (now + 86400) * 1000, "accountId": "acct-worker-1"}
    data = {"openai-codex": {**base, **(entry or {})}, "anthropic": {"type": "api_key", "key": "sk-ant-x"}, **top}
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(data))
    return path


def test_selected_auth_selects_codex_only_and_replaces_refresh(tmp_path):
    auth = pi.selected_auth(str(auth_file(tmp_path)))
    snapshot = json.loads(auth.snapshot())
    assert list(snapshot) == ["openai-codex"]
    entry = snapshot["openai-codex"]
    assert entry["refresh"] == pi.PLACEHOLDER_REFRESH and entry["type"] == "oauth" and entry["accountId"] == "acct-worker-1"
    assert "rt-real-refresh-secret" in auth.secrets and pi.PLACEHOLDER_REFRESH not in auth.secrets
    assert "rt-real" not in repr(auth) and auth.access not in repr(auth)


def test_omp_auth_store_snapshot_is_a_one_row_sqlite_db(tmp_path):
    import sqlite3

    auth = pi.selected_auth(str(auth_file(tmp_path)))
    db = tmp_path / "agent.db"
    db.write_bytes(auth.omp_store_snapshot())
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT provider, credential_type, data, disabled_cause FROM auth_credentials").fetchall()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables <= {"auth_credentials", "sqlite_sequence"}  # omp creates and versions the rest itself
    assert len(rows) == 1 and rows[0][:2] == ("openai-codex", "oauth") and rows[0][3] is None
    data = json.loads(rows[0][2])
    assert data == {"access": auth.access, "refresh": pi.PLACEHOLDER_REFRESH, "expires": auth.expires_ms,
                    "accountId": "acct-worker-1"}
    assert b"rt-real-refresh-secret" not in auth.omp_store_snapshot()


def test_flavour_auth_targets():
    assert pi.FLAVOURS["pi"].auth_path == "/tmp/pi-agent/auth.json"
    assert pi.FLAVOURS["omp"].auth_path == "/tmp/pi-agent/agent.db"


def test_selected_auth_effective_expiry_is_min_of_declared_and_jwt(tmp_path):
    now = time.time()
    auth = pi.selected_auth(str(auth_file(tmp_path, {"access": jwt(now + 3600), "expires": (now + 86400) * 1000})))
    assert auth.expiry_source == "jwt" and abs(auth.expires_ms - (now + 3600) * 1000) < 2000
    with pytest.raises(pi.AuthError, match="less than the required 7200 s"):
        pi.selected_auth(str(auth_file(tmp_path, {"access": jwt(now + 3600)})), min_validity_seconds=7200)


def test_selected_auth_non_jwt_uses_declared_and_says_so(tmp_path):
    now = time.time()
    auth = pi.selected_auth(str(auth_file(tmp_path, {"access": "opaque-token-value"})))
    assert auth.expiry_source == "declared"
    with pytest.raises(pi.AuthError, match="not a decodable JWT; declared expiry used"):
        pi.selected_auth(str(auth_file(tmp_path, {"access": "opaque-token-value", "expires": (now + 100) * 1000})))


@pytest.mark.parametrize("entry, phrase", [
    ({"type": "api_key"}, "not OAuth"),
    ({"access": ""}, "no valid access"),
    ({"refresh": None}, "no valid refresh"),
    ({"accountId": "a b"}, "no valid accountId"),
    ({"expires": "soon"}, "numeric millisecond expires"),
    ({"expires": True}, "numeric millisecond expires"),
    ({"expires": float("nan")}, "numeric millisecond expires"),
    ({"expires": 1000, "access": "opaque"}, "less than the required"),
])
def test_selected_auth_refusals(tmp_path, entry, phrase):
    with pytest.raises(pi.AuthError, match=phrase):
        pi.selected_auth(str(auth_file(tmp_path, entry)))


def test_selected_auth_file_rules(tmp_path):
    with pytest.raises(pi.AuthError, match="not readable"):
        pi.selected_auth(str(tmp_path / "missing.json"))
    with pytest.raises(pi.AuthError, match="not a regular file"):
        pi.selected_auth(str(tmp_path))
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (pi.AUTH_FILE_CAP + 1))
    with pytest.raises(pi.AuthError, match="larger than 1 MiB"):
        pi.selected_auth(str(big))
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"anthropic": {"type": "oauth"}}))
    with pytest.raises(pi.AuthError, match="no openai-codex entry"):
        pi.selected_auth(str(other))
    original = auth_file(tmp_path)
    before = original.read_bytes()
    pi.selected_auth(str(original))
    assert original.read_bytes() == before  # never written back


def test_ensure_validity_recheck(tmp_path, monkeypatch):
    auth = pi.selected_auth(str(auth_file(tmp_path)))
    pi.ensure_validity(auth, 3600)
    monkeypatch.setattr(pi, "time", type("T", (), {"time": staticmethod(lambda: auth.expires_ms / 1000 - 10)}))
    with pytest.raises(pi.AuthError):
        pi.ensure_validity(auth, 300)


def test_redact_longest_first_and_placeholder_untouched(tmp_path):
    auth = pi.selected_auth(str(auth_file(tmp_path, {"accountId": "acct-worker-1", "refresh": "acct-worker-1-refresh"})))
    text = f"a={auth.access} r=acct-worker-1-refresh id=acct-worker-1 p={pi.PLACEHOLDER_REFRESH}"
    redacted = pi.redact(text, auth)
    assert redacted == f"a=[REDACTED] r=[REDACTED] id=[REDACTED] p={pi.PLACEHOLDER_REFRESH}"
    assert pi.redact(None, auth) is None and pi.redact("x", None) == "x"
    assert pi.redact_bytes(text.encode(), auth) == redacted.encode()


# -- bundle validation --------------------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def bundle_with(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, str, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("a\n")
    git(source, "init", "-q", "-b", "main")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "base")
    base = git(source, "rev-parse", "HEAD")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(source), str(work)], check=True)
    for name, data in files.items():
        (work / name).write_bytes(data)
    git(work, "add", "-A")
    git(work, "commit", "-qm", "result")
    bundle = tmp_path / "out.bundle"
    git(work, "bundle", "create", str(bundle), f"{base}..HEAD")
    return source, base, bundle


def test_validate_bundle_accepts_clean_and_placeholder(tmp_path):
    auth = pi.selected_auth(str(auth_file(tmp_path)))
    source, base, bundle = bundle_with(tmp_path, {"b.py": f"P = '{pi.PLACEHOLDER_REFRESH}'\n".encode(),
                                                  "bin.dat": bytes(range(256))})
    refs_before = git(source, "for-each-ref")
    pi.validate_bundle(str(source), str(bundle), base, auth)
    assert git(source, "for-each-ref") == refs_before  # nothing imported into the source


@pytest.mark.parametrize("which", ["access", "refresh", "accountId"])
def test_validate_bundle_refuses_secret_in_new_object(tmp_path, which):
    path = auth_file(tmp_path)
    auth = pi.selected_auth(str(path))
    value = json.loads(path.read_text())["openai-codex"][which]
    source, base, bundle = bundle_with(tmp_path, {"leak.bin": b"\x00\xff" + value.encode() + b"\x00"})
    with pytest.raises(pi.BundleError, match="contains a selected credential value"):
        pi.validate_bundle(str(source), str(bundle), base, auth)


def test_validate_bundle_refuses_oversized_object(tmp_path, monkeypatch):
    auth = pi.selected_auth(str(auth_file(tmp_path)))
    monkeypatch.setattr(pi, "OBJECT_CAP", 1000)
    source, base, bundle = bundle_with(tmp_path, {"big.bin": os.urandom(5000)})
    with pytest.raises(pi.BundleError, match="exceeds the 1000 byte object cap"):
        pi.validate_bundle(str(source), str(bundle), base, auth)


# -- review fixes: redaction around cuts and whole-pack bundle scan ------------------------------

def test_safe_tail_and_head_redact_before_and_around_cuts(tmp_path):
    auth = pi.selected_auth(str(auth_file(tmp_path)))
    secret = auth.access
    text = "a" * 50 + secret + "b" * 50
    # redaction happens before the cut, so a cut through the secret cannot leave a raw suffix/prefix
    tail = pi.safe_tail(text, 60, auth)
    assert secret[-10:] not in tail and len(tail) <= 60
    head = pi.safe_head(text, 70, auth)
    assert secret[:20] not in head and len(head) <= 70
    # text that was already cut upstream (e.g. `tail -c`) loses a leading secret suffix
    assert pi.safe_tail(secret[-20:] + " ok", 100, auth) == "[REDACTED] ok"
    assert pi.safe_head("ok " + secret[:20], 100, auth) == "ok [REDACTED]"
    assert pi.safe_tail(secret[-5:] + " ok", 100, auth) == secret[-5:] + " ok"  # fragments under 8 are kept
    assert pi.safe_tail("plain text", 5, None) == " text"


def _crafted_bundle(tmp_path: Path, extra_blob: bytes) -> tuple[Path, str, Path]:
    """A bundle whose pack carries a blob that no commit reaches."""
    source, base, _ = bundle_with(tmp_path, {"b.txt": b"clean\n"})
    work = tmp_path / "work"
    tip = git(work, "rev-parse", "HEAD")
    blob = subprocess.run(["git", "-C", str(work), "hash-object", "-w", "--stdin"], input=extra_blob,
                          capture_output=True, check=True).stdout.decode().strip()
    objects = subprocess.run(["git", "-C", str(work), "rev-list", "--objects", f"{base}..{tip}"],
                             capture_output=True, text=True, check=True).stdout.split()
    shas = [o for o in objects if len(o) == 40] + [blob]
    pack = subprocess.run(["git", "-C", str(work), "pack-objects", "--stdout"], input="\n".join(shas).encode(),
                          capture_output=True, check=True).stdout
    bundle = tmp_path / "crafted.bundle"
    bundle.write_bytes(f"# v2 git bundle\n-{base} base\n{tip} HEAD\n\n".encode() + pack)
    return source, base, bundle


def test_validate_bundle_refuses_secret_in_unreachable_pack_object(tmp_path):
    path = auth_file(tmp_path)
    auth = pi.selected_auth(str(path))
    source, base, bundle = _crafted_bundle(tmp_path, b"stash " + auth.access.encode() + b"\n")
    git(source, "bundle", "verify", str(bundle))  # a valid bundle that git would import whole
    with pytest.raises(pi.BundleError, match="contains a selected credential value"):
        pi.validate_bundle(str(source), str(bundle), base, auth)


def test_validate_bundle_caps_unreachable_pack_object(tmp_path, monkeypatch):
    auth = pi.selected_auth(str(auth_file(tmp_path)))
    source, base, bundle = _crafted_bundle(tmp_path, os.urandom(5000))
    monkeypatch.setattr(pi, "OBJECT_CAP", 1000)
    with pytest.raises(pi.BundleError, match="object cap"):
        pi.validate_bundle(str(source), str(bundle), base, auth)


def test_pi_comms_extension_is_last_after_operator_extensions():
    import shlex
    extensions = ("/sandbox/plugins/first.ts", "/sandbox/plugins/replace.ts")
    skills = ("/sandbox/plugins/guards/skills/dispatch",)
    invocation = pi.invocation("pi", bin="pi", provider="openai-codex", model="gpt-6-luna", session_id=SID,
                               harness_timeout=600, extensions=extensions, skills=skills, comms=True)
    argv = shlex.split(invocation.command)
    loaded = [argv[index + 1] for index, flag in enumerate(argv) if flag == "-e"]
    assert loaded == ["builtin:mcp", *extensions, "/usr/local/lib/aos-agent/aos_pi.ts"]
    assert argv[-1] == "/usr/local/lib/aos-agent/aos_pi.ts"
    skill = argv.index("--skill")
    assert argv[skill + 1] == "/sandbox/plugins/guards/skills/dispatch"
    assert skill < len(argv) - 2 and argv[-2] == "-e"
    assert "--no-mcp" not in argv
    assert "-e" in invocation.required_flags
    off = pi.invocation("pi", bin="pi", provider="openai-codex", model="gpt-6-luna", session_id=SID,
                        harness_timeout=600, extensions=extensions, skills=skills, comms=False)
    argv = shlex.split(off.command)
    assert "--no-mcp" in argv
    assert "builtin:mcp" not in argv and "/usr/local/lib/aos-agent/aos_pi.ts" not in argv
    assert [argv[index + 1] for index, flag in enumerate(argv) if flag == "-e"] == list(extensions)


def test_omp_comms_extension_is_last_after_operator_extensions():
    import shlex
    extensions = ("/sandbox/plugins/first.ts", "/sandbox/plugins/replace.ts")
    invocation = pi.invocation("omp", bin="omp", provider=None, model="test-model", session_id=None,
                               harness_timeout=600, extensions=extensions, comms=True)
    argv = shlex.split(invocation.command)
    loaded = [argv[index + 1] for index, flag in enumerate(argv) if flag == "-e"]
    assert loaded == [*extensions, "/usr/local/lib/aos-agent/aos_pi.ts"]
    assert "-e" in invocation.required_flags
    tools = next(flag.removeprefix("--tools=") for flag in argv if flag.startswith("--tools=")).split(",")
    assert "task" in tools
    assert set(tools) == {
        "read", "bash", "edit", "eval", "glob", "grep", "wait", "todo", "write", "web_search", "task",
        "mcp__aos_ask", "mcp__aos_reply", "mcp__aos_inbox", "mcp__aos_wait", "mcp__aos_declare", "mcp__aos_propose",
    }
    off = pi.invocation("omp", bin="omp", provider=None, model="test-model", session_id=None,
                        harness_timeout=600, extensions=extensions, comms=False)
    assert "/usr/local/lib/aos-agent/aos_pi.ts" not in off.command
    assert "--tools=" not in off.command


@pytest.mark.parametrize("flavour, model, session_id", [("pi", "gpt-6-luna", SID), ("omp", "test-model", None)])
def test_comms_pull_keeps_the_aos_tools_and_drops_only_the_extension(flavour, model, session_id):
    # AOS_OPENSHELL_COMMS=pull: the explicit path (MCP loading, the omp allowlist, the operator's extensions) as with
    # comms on; the image-owned extension, the automatic path, is not loaded.
    import shlex
    extensions = ("/sandbox/plugins/first.ts",)
    common = dict(bin=flavour, provider="openai-codex" if flavour == "pi" else None, model=model,
                  session_id=session_id, harness_timeout=600, extensions=extensions, comms=True)
    on = shlex.split(pi.invocation(flavour, **common).command)
    pull = pi.invocation(flavour, **common, automatic=False)
    argv = shlex.split(pull.command)
    assert "/usr/local/lib/aos-agent/aos_pi.ts" not in argv
    assert on[-2:] == ["-e", "/usr/local/lib/aos-agent/aos_pi.ts"] and argv == on[:-2]
    assert [argv[index + 1] for index, flag in enumerate(argv) if flag == "-e"][-1] == extensions[-1]
    if flavour == "pi":
        assert "--no-mcp" not in argv and argv[argv.index("builtin:mcp") - 1] == "-e"
    else:
        tools = next(flag.removeprefix("--tools=") for flag in argv if flag.startswith("--tools=")).split(",")
        assert {"task", *(f"mcp__aos_{verb}" for verb in ("ask", "reply", "inbox", "wait", "declare", "propose"))} <= set(tools)


def test_omp_interactive_invocation_has_no_print_mode_and_names_the_submit_tool():
    result = pi.invocation("omp", bin="omp", provider=None, model="openai-codex/gpt-6-luna", session_id=None,
                           harness_timeout=60, comms=True, interactive=True, extensions=("/sandbox/plugin.ts",))
    command = result.command
    assert command == ("PI_CODING_AGENT_DIR=/tmp/pi-agent HOME=/tmp/pi-home omp --model openai-codex/gpt-6-luna "
                       "--session-dir /sandbox/pi-sessions --no-extensions --no-skills --no-rules --no-title --max-time 60 "
                       "--approval-mode=yolo --config /tmp/pi-agent/interactive.yml --tools="
                       + ",".join(pi.OMP_TOOLS + pi.OMP_AOS_TOOLS_INTERACTIVE)
                       + ' -e /sandbox/plugin.ts -e /usr/local/lib/aos-agent/aos_pi.ts "$(cat /sandbox/prompt.txt)"')
    assert "--print" not in result.required_flags and "--mode" not in result.required_flags
    assert "--config" in result.required_flags
    assert pi.OMP_AOS_TOOLS_INTERACTIVE[-4:] == (
        "mcp__aos_inbox_wait", "mcp__aos_family", "mcp__aos_import", "mcp__aos_export_candidate")


def test_pi_flavour_refuses_interactive():
    with pytest.raises(ValueError, match="interactive execution needs the omp flavour"):
        pi.invocation("pi", bin="pi", provider=None, model="gpt-6-luna", session_id=SID,
                      harness_timeout=60, interactive=True)
