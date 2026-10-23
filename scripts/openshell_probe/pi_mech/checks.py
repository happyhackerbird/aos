#!/usr/bin/env python3
"""Checks for the Pi-family mechanism probe (docs/probes/2026-10-14-pi-mechanisms.md).

Reads what one run left behind (the flavour's print-mode JSONL in `harness.out`, the in-sandbox
exec reads the driver saved as `exec-reads.txt`, the downloaded `probe-report.md`) and writes two
files into the evidence directory: `stream-events.txt` (the ordered event list: tool calls with
their arguments and results, assistant messages with their model, session header, settlement) and
`checks.txt` (one block per row, P1-P8, each ending in PASS, FAIL or NOT EXERCISED with the evidence
lines it rests on, then the secret scan). A verdict here is the mechanical reading of the evidence;
the record quotes the lines and owns the final word. P8 applies the runner's own parser
(`agent_os.pi`) to the stream, so the contract verdict is the runner's.

The secret scan counts, per downloaded file, the occurrences of the worker's selected Pi secrets
(the access token and account id the driver read through `pi.selected_auth`); it never prints a
value. Its positive control plants a random string into a copy of the first scanned file and must
count exactly 1 in the same invocation.

    uv run python scripts/openshell_probe/pi_mech/checks.py --flavour pi --mode mech --downloads D --evidence E \\
        --codeword CODEWORD-xxxxxx --prepare-summary prepare.json [--pi-auth F] [--session-id UUID]
    uv run python scripts/openshell_probe/pi_mech/checks.py --self-test [--scratch DIR]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

from agent_os import pi

HEAD = 400
SECTION_RE = re.compile(r"^@@ (\S+)\s*$")
HEADER_RE = re.compile(r"^## (P\d+[a-z0-9]*)\b")
SKILLS = ("probe-skill", "probe-skill-home", "repo-skill")
CURL_DENIED = ("curl: (6)", "curl: (7)", "curl: (28)", "curl: (35)", "curl: (56)", "Could not resolve host",
               "Failed to connect", "403", "Forbidden", "Connection refused", "denied")
FS_DENIED = ("Permission denied", "Read-only file system", "Operation not permitted", "EACCES", "EROFS", "EPERM", "errno 13")
CHILD_MODEL = "gpt-5.6-terra"
SUBAGENT_TOOLS = {"pi": "subagent", "omp": "task"}


def short(value: object, limit: int = HEAD) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + f"... [{len(text)} chars]"


def quote(text: str, limit: int = 300) -> str:
    return short(text.strip(), limit)


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) if isinstance(b, dict) and b.get("type", "text") == "text" else json.dumps(b)
                         for b in content)
    return "" if content is None else json.dumps(content)


# -- the stream ------------------------------------------------------------------------------

class Stream:
    """Pi print-mode JSONL, indexed: tool calls (start+end by id), assistant messages, session."""

    def __init__(self, lines: list[str]) -> None:
        self.events: list[dict] = []
        self.bad_lines = 0
        for raw in lines:
            try:
                event = json.loads(raw)
            except ValueError:
                self.bad_lines += 1
                continue
            if isinstance(event, dict):
                self.events.append(event)
        self.session = next((e for e in self.events if e.get("type") == "session"), None)
        self.calls: dict[str, dict] = {}
        self.order: list[str] = []
        self.assistant: list[dict] = []       # {index, text, model, provider, stopReason}
        self.kinds = collections.Counter(str(e.get("type")) for e in self.events)
        for index, event in enumerate(self.events):
            t = event.get("type")
            if t == "tool_execution_start":
                cid = str(event.get("toolCallId"))
                self.calls[cid] = {"index": index, "name": event.get("toolName"), "args": event.get("args") or {}, "result": None,
                                   "is_error": None, "parent": event.get("parentToolCallId")}
                self.order.append(cid)
            elif t == "tool_execution_end":
                cid = str(event.get("toolCallId"))
                entry = self.calls.setdefault(cid, {"index": index, "name": event.get("toolName"), "args": {}, "result": None,
                                                    "is_error": None, "parent": event.get("parentToolCallId")})
                result = event.get("result") or {}
                entry["result"] = content_text(result.get("content") if isinstance(result, dict) else result)
                entry["is_error"] = bool(event.get("isError"))
            elif t == "message_end":
                message = event.get("message") or {}
                if message.get("role") == "assistant":
                    self.assistant.append({"index": index, "text": pi._assistant_text(message), "model": message.get("model"),
                                           "provider": message.get("provider"), "stop": message.get("stopReason")})
        self.final = self.assistant[-1]["text"] if self.assistant else None

    def calls_named(self, name: str) -> list[dict]:
        return [self.calls[c] for c in self.order if self.calls[c].get("name") == name]

    def models(self) -> list[str]:
        out = []
        for m in self.assistant:
            tag = f"{m.get('provider')}/{m.get('model')}"
            if not out or out[-1] != tag:
                out.append(tag)
        return out

    def excerpt(self) -> list[str]:
        out = []
        for event in self.events:
            t = event.get("type")
            if t == "session":
                out.append(f"session id={event.get('id')} version={event.get('version')} cwd={event.get('cwd')}")
            elif t == "tool_execution_start":
                out.append(f"tool_start {event.get('toolName')} id=...{str(event.get('toolCallId'))[-6:]} "
                           f"parent={event.get('parentToolCallId')} args={short(event.get('args'), 500)}")
            elif t == "tool_execution_end":
                result = event.get("result") or {}
                out.append(f"tool_end {event.get('toolName')} id=...{str(event.get('toolCallId'))[-6:]} isError={event.get('isError')} "
                           f"result={short(content_text(result.get('content') if isinstance(result, dict) else result), 500)}")
            elif t == "message_end":
                message = event.get("message") or {}
                if message.get("role") == "assistant":
                    out.append(f"assistant model={message.get('provider')}/{message.get('model')} stop={message.get('stopReason')} "
                               f"text={short(pi._assistant_text(message), 400)}")
                elif message.get("role") == "user":
                    out.append(f"user {short(content_text(message.get('content')), 160)}")
            elif t in ("agent_end", "agent_settled", "turn_end", "agent_start", "turn_start", "compaction_start", "auto_retry_start", "notice"):
                out.append(f"{t} {short({k: v for k, v in event.items() if k not in ('type', 'messages', 'message')}, 240)}")
        return [f"{i:03d} {line}" for i, line in enumerate(out)]


# -- sections in bash results ------------------------------------------------------------------

def sections(stream: Stream) -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for cid in stream.order:
        call = stream.calls[cid]
        if call.get("name") != "bash" or call.get("result") is None:
            continue
        header, label, buf = "?", None, []
        for line in call["result"].splitlines():
            h = HEADER_RE.match(line)
            s = SECTION_RE.match(line)
            if h or s:
                if label is not None:
                    found.setdefault((header, label), "\n".join(buf))
                if h:
                    header, label, buf = h.group(1), None, []
                else:
                    label, buf = s.group(1), []
                continue
            if label is not None:
                buf.append(line)
        if label is not None:
            found.setdefault((header, label), "\n".join(buf))
    return found


def rc_of(text: str) -> str | None:
    found = re.findall(r"^rc=(-?\d+)\s*$", text, re.M)
    return found[-1] if found else None


# -- exec reads --------------------------------------------------------------------------------

def exec_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    label, buf = None, []
    for line in text.splitlines():
        m = re.match(r"^== (\S+) ==", line)
        if m:
            if label is not None:
                blocks[label] = "\n".join(buf)
            label, buf = m.group(1), []
        elif label is not None:
            buf.append(line)
    if label is not None:
        blocks[label] = "\n".join(buf)
    return blocks


def inner(block: str, path: str) -> str:
    out, inside = [], False
    for line in block.splitlines():
        if line.startswith("-- "):
            if inside:
                break
            inside = line[3:].strip().split(" ")[0] == path
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def fire_lines(block: str, path_hint: str = "pi-probe") -> tuple[str | None, list[dict]]:
    """The probe extension's fire log: which of the candidate paths exists, and its JSON lines."""
    where, out, inside = None, [], False
    for line in block.splitlines():
        if line.startswith("-- "):
            inside = path_hint in line and "absent" not in line
            if inside:
                where = line[3:].split(" lines=")[0]
            continue
        if inside and line.strip().startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return where, out


def report_lines(report: str, header: str) -> list[str]:
    out, inside = [], False
    for line in report.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().split()[0] == header if line[3:].strip() else False
            continue
        if inside:
            out.append(line)
    return out


def curl_denied(text: str) -> bool:
    lines = [l for l in text.strip().splitlines() if l.strip()]
    if not lines or "No such file" in text:
        return False
    first = lines[0].strip()
    m = re.search(r"(-?\d+)", first)
    if m and int(m.group(1)) != 0 and len(first) <= 40:
        return True
    return any(s in text for s in CURL_DENIED)


# -- rows ----------------------------------------------------------------------------------------

class Row:
    def __init__(self, name: str, title: str) -> None:
        self.name, self.title, self.lines, self.verdict = name, title, [], "NOT EXERCISED"

    def add(self, line: str) -> None:
        self.lines.append(line)

    def render(self) -> str:
        body = "\n".join(f"  {line}" for line in self.lines) if self.lines else "  (no evidence line)"
        return f"{self.name} {self.title}\n{body}\n  => {self.verdict}"


def row_p1(stream: Stream, report: str, mode: str, blocks: dict | None = None) -> Row:
    row = Row("P1", "a custom tool from the explicit -e extension (probe_echo)")
    calls = stream.calls_named("probe_echo")
    for call in calls[:2]:
        row.add(f"probe_echo call args={short(call['args'], 120)} isError={call['is_error']} result={quote(call['result'] or '', 160)}")
    # omp exposes a custom tool to the model as an xd:// device (`read xd://probe_echo` for the schema,
    # `write xd://probe_echo` with the JSON arguments), so the call is a write, not a tool of that name.
    device = [stream.calls[c] for c in stream.order if "xd://probe_echo" in json.dumps(stream.calls[c]["args"])]
    for call in device[:2]:
        row.add(f"xd device call: {call['name']} args={short(call['args'], 120)} isError={call['is_error']} result={quote(call['result'] or '', 160)}")
    _, fires = fire_lines((blocks or {}).get("firelog", ""))
    fired = sum(1 for f in fires if f.get("event") == "probe_echo")
    row.add(f"probe_echo executions in the extension's fire log: {fired}")
    lines = report_lines(report, "P1")
    row.add(f"report P1: {quote(' | '.join(l for l in lines if l.strip()), 240)}")
    if not stream.events:
        return row
    ok = (any(not c["is_error"] and "PROBE-ECHO-OK" in (c["result"] or "") for c in calls + device)
          or (fired > 0 and any("PROBE-ECHO-OK" in l for l in lines)))
    if mode in ("control", "tools-off"):
        row.verdict = "PASS" if not calls and not device and any("NO-TOOL" in l for l in lines) else "FAIL"
    else:
        row.verdict = "PASS" if ok else "FAIL"
    return row


def row_p2(stream: Stream, report: str, phrases: dict[str, str], flavour: str, mode: str) -> Row:
    row = Row("P2", "skills: probe-skill (pi: explicit --skill; omp: $PI_CODING_AGENT_DIR/skills), probe-skill-home (omp: ~/.omp/agent/skills), "
                    "repo-skill (.agents/skills); nope-skill negative")
    lines = report_lines(report, "P2")
    if not lines:
        row.add("no P2 section in probe-report.md")
        return row
    got: dict[str, str] = {}
    for line in lines:
        m = re.match(r"^\s*-?\s*`?(probe-skill-home|probe-skill|repo-skill|nope-skill)`?\s*(path)?\s*:\s*(.*)$", line)
        if m:
            got[(m.group(1) + (" path" if m.group(2) else ""))] = m.group(3).strip()
    for key in sorted(got):
        row.add(f"report {key}: {quote(got[key], 200)}")
    reads = [c for cid in stream.order for c in [stream.calls[cid]] if "SKILL.md" in json.dumps(c.get("args"))]
    for call in reads[:6]:
        row.add(f"read: {call['name']} {short(call['args'], 200)}")
    found = {name: bool(phrases.get(name)) and phrases[name] in got.get(name, "") for name in SKILLS}
    nope = "NO-SKILL" in got.get("nope-skill", "").upper() and not any(p in got.get("nope-skill", "") for p in phrases.values())
    row.add(f"phrase found: {found}; nope-skill negative holds: {nope}")
    if not phrases or not stream.events:
        return row
    if mode in ("control", "tools-off"):
        row.verdict = "PASS" if not any(found.values()) and nope else "FAIL"
    else:
        # The explicit skill is the row; the other locations are measured and recorded either way.
        row.verdict = "PASS" if found["probe-skill"] and nope else "FAIL"
    return row


def row_p3(stream: Stream, found: dict, blocks: dict, mode: str) -> Row:
    row = Row("P3", "the extension's hooks: session_start boundary read, tool_call deny, tool_result note, fire log location")
    where, fires = fire_lines(blocks.get("firelog", ""))
    counts = collections.Counter(str(f.get("event")) for f in fires)
    row.add(f"fire log at {where}: lines={len(fires)} events={dict(sorted(counts.items()))}")
    start = next((f for f in fires if f.get("event") == "session_start"), None)
    if start is not None:
        row.add(f"session_start boundary={short(start.get('boundary'), 600)}")
    blocks_seen = [f for f in fires if f.get("decision") == "block"]
    notes = [f for f in fires if f.get("decision") == "note"]
    spawns = [f for f in fires if f.get("event") == "before_subagent_spawn"]
    failed = [f for f in fires if f.get("event") == "register_failed"]
    row.add(f"blocks={len(blocks_seen)} notes={len(notes)} before_subagent_spawn={len(spawns)} register_failed={[f.get('what') for f in failed]}")
    for f in spawns[:3]:
        row.add(f"spawn hook: {short(f, 300)}")
    deny_calls = [stream.calls[c] for c in stream.order if stream.calls[c]["name"] == "bash" and "EXT-DENY-ME" in json.dumps(stream.calls[c]["args"])]
    ran = any("EXT-DENY-ME" in (c["result"] or "") and not c["is_error"] for c in deny_calls)
    for c in deny_calls[:2]:
        row.add(f"P3b bash isError={c['is_error']} result={quote(c['result'] or '', 200)}")
    note_calls = [stream.calls[c] for c in stream.order if stream.calls[c]["name"] == "bash" and "EXT-NOTE-ME" in json.dumps(stream.calls[c]["args"])]
    noted = any("[pi-probe] note" in (c["result"] or "") for c in note_calls)
    for c in note_calls[:1]:
        row.add(f"P3c bash result={quote(c['result'] or '', 200)}")
    allowed = any("EXT-ALLOWED-OK" in (stream.calls[c]["result"] or "") for c in stream.order)
    row.add(f"P3a ran={allowed}; P3b command calls={len(deny_calls)} ran(output seen, no error)={ran}; P3c note seen={noted}")
    guards = blocks.get("firelog", "")
    gl = [l for l in guards.splitlines() if "dispatch-guards-fires" in l]
    row.add(f"pi-dispatch-guards fire log: {quote(' '.join(gl), 200)}")
    if not stream.events:
        return row
    if mode in ("control", "tools-off"):
        row.verdict = "PASS" if not fires and ran else "FAIL"
        return row
    boundary = (start or {}).get("boundary") or {}
    b_ok = (isinstance(boundary, dict) and str(boundary.get("fetch", "")).startswith("error") and
            any(s in str(boundary.get("usr_write")) for s in FS_DENIED) and str(boundary.get("state_write", "")).startswith("ok"))
    row.add(f"session_start boundary holds (fetch error, /usr denied, state ok): {b_ok}")
    row.verdict = "PASS" if start and blocks_seen and not ran and noted and allowed and b_ok else "FAIL"
    return row


def row_p4(found: dict, blocks: dict) -> Row:
    row = Row("P4", "worktrees: git worktree add under /tmp (ok), /usr/local (denied), remove")
    if ("P4", "wt-add-tmp") not in found:
        row.add("no P4 section in the bash output")
        return row
    add = found.get(("P4", "wt-add-tmp"), "")
    listing = found.get(("P4", "wt-list"), "")
    remove = found.get(("P4", "wt-remove"), "")
    usr = found.get(("P4", "wt-add-usr"), "")
    row.add(f"wt-add-tmp rc={rc_of(add)} {quote(add, 200)}")
    row.add(f"wt-list: {quote(listing, 200)}")
    row.add(f"wt-remove rc={rc_of(remove)} {quote(remove, 160)}")
    row.add(f"wt-add-usr rc={rc_of(usr)} {quote(usr, 300)}")
    post = blocks.get("worktrees", "")
    row.add(f"exec after the run: {quote(post, 300)}")
    usr_exists = blocks.get("usr-paths", "")
    row.add(f"exec /usr paths: {quote(usr_exists, 200)}")
    ok = (rc_of(add) == "0" and "/tmp/pi-wt-1" in listing and rc_of(remove) == "0" and rc_of(usr) not in (None, "0")
          and any(s in usr for s in FS_DENIED) and "pi-wt" not in post and "No such file" in usr_exists)
    row.verdict = "PASS" if ok else "FAIL"
    return row


def row_p5(stream: Stream, blocks: dict, report: str, codeword: str, flavour: str, mode: str) -> Row:
    tool = SUBAGENT_TOOLS[flavour]
    row = Row("P5", f"subagents through the `{tool}` tool: probe-agent with model {CHILD_MODEL}, a second run; files, curl, env, cwd, code word")
    calls = stream.calls_named(tool)
    for c in calls[:4]:
        row.add(f"{tool} call args={short(c['args'], 500)} isError={c['is_error']} result={quote(c['result'] or '', 300)}")
    k = report_lines(report, "P5")
    no_tool = next((l for l in k if "NO-SUBAGENT-TOOL" in l or "SUBAGENT-TOOL" in l), None)
    agents_line = next((l for l in k if l.strip().lower().startswith("agents:")), None)
    if agents_line:
        row.add(f"report {quote(agents_line, 300)}")
    call_args = [l for l in report.splitlines() if l.strip().lower().startswith("call-args:")]
    for line in call_args[:2]:
        row.add(f"report {quote(line, 300)}")
    files = blocks.get("subagent-files", "")
    probe = inner(files, "/sandbox/sub-probe.txt")
    second = inner(files, "/sandbox/sub-second.txt")
    probe_curl = inner(files, "/sandbox/sub-probe-curl.txt")
    second_curl = inner(files, "/sandbox/sub-second-curl.txt")
    probe_env = inner(files, "/sandbox/sub-probe-env.txt")
    second_env = inner(files, "/sandbox/sub-second-env.txt")
    probe_cwd = inner(files, "/sandbox/sub-probe-cwd.txt")
    second_cwd = inner(files, "/sandbox/sub-second-cwd.txt")
    row.add(f"sub-probe.txt={quote(probe, 80)} curl={quote(probe_curl, 160)} env={quote(probe_env, 40)} cwd={quote(probe_cwd, 120)}")
    row.add(f"sub-second.txt={quote(second, 80)} curl={quote(second_curl, 160)} env={quote(second_env, 40)} cwd={quote(second_cwd, 120)}")
    sessions = blocks.get("sessions", "")
    row.add(f"sessions: {quote(sessions.splitlines()[0] if sessions else '', 160)}")
    child_models: list[str] = []
    for chunk in sessions.split("-- ")[1:]:
        head = chunk.splitlines()[0] if chunk else ""
        models = re.search(r"assistant models=(\[.*?\])", chunk)
        first = re.search(r"first=(\{.*?\}) cwd=(\S*)", chunk)
        row.add(f"session {quote(head, 100)} models={models.group(1) if models else '?'} first={quote(first.group(1) if first else '', 200)} cwd={first.group(2) if first else '?'}")
        if models:
            child_models.append(models.group(1))
    passed = codeword and (json.dumps(call_args).count(codeword) + sum(json.dumps(c["args"]).count(codeword) for c in calls))
    sub_hits = files.count(codeword) if codeword else 0
    desk_hits = str(stream.final or "").count(codeword) if codeword else 0
    row.add(f"code word: in {tool} args={passed}, in subagent files={sub_hits}; positive control, in the final message={desk_hits}")
    if mode in ("control", "tools-off"):
        row.add(f"tool line: {quote(no_tool or '', 200)}; {tool} calls={len(calls)}; files absent={'No such file' in probe}")
        row.verdict = "PASS" if no_tool and not calls and "No such file" in probe else "FAIL"
        return row
    if not calls and not k:
        return row
    if no_tool and "NO-SUBAGENT-TOOL" in no_tool:
        row.add("the mech run had no subagent tool (the extension did not load or registers none)")
        row.verdict = "FAIL"
        return row
    probe_ok = CHILD_MODEL in probe and "No such file" not in probe
    child_recorded = any(CHILD_MODEL in m for m in child_models)
    second_ok = bool(second.strip()) and "No such file" not in second
    curl_ok = curl_denied(probe_curl) and curl_denied(second_curl)
    env_ok = probe_env.strip().splitlines()[:1] == ["0"] and second_env.strip().splitlines()[:1] == ["0"]
    cw_ok = not passed and sub_hits == 0 and desk_hits >= 1
    row.add(f"sub-verdicts: probe self-report={probe_ok} child model in a session file={child_recorded} second ran={second_ok} "
            f"curl denied both={curl_ok} env 0 both={env_ok} code word clean with control={cw_ok}")
    row.verdict = "PASS" if probe_ok and child_recorded and second_ok and curl_ok and env_ok and cw_ok else "FAIL"
    return row


def row_p6(found: dict, blocks: dict, flavour: str) -> Row:
    row = Row("P6", "boundaries in the model's bash: the token entries, HOME and the agent dir, a /usr write, a state write")
    if ("P6", "env-count") not in found:
        row.add("no P6 section in the bash output")
        return row
    count = found.get(("P6", "env-count"), "").strip()
    prefix = found.get(("P6", "printenv-prefix"), "").strip()
    home = found.get(("P6", "home"), "").strip()
    usr = found.get(("P6", "usr-write"), "")
    state = found.get(("P6", "state-write-home"), "")
    row.add(f"env | grep -c CLAUDE_CODE_OAUTH_TOKEN: {quote(count, 40)} (no provider attached for a Pi run: 0 expected)")
    row.add(f"printenv ... | cut -c1-12: {quote(prefix, 40)}")
    row.add(f"home: {quote(home, 100)}")
    row.add(f"/usr/pi-mark: rc={rc_of(usr)} {quote(usr, 240)}")
    row.add(f"~/.local/state/pi-probe/mark-shell: rc={rc_of(state)} {quote(state, 200)}")
    row.add(f"exec state dirs after the run: {quote(blocks.get('state', ''), 300)}")
    expected_home = "HOME=/tmp/pi-home" if flavour == "omp" else "HOME=/sandbox"
    ok = (count.splitlines()[:1] == ["0"] and expected_home in home and f"PI_CODING_AGENT_DIR={pi.AGENT_DIR}" in home
          and rc_of(usr) not in (None, "0") and any(s in usr for s in FS_DENIED) and rc_of(state) == "0")
    row.add(f"expected {expected_home} and PI_CODING_AGENT_DIR={pi.AGENT_DIR}")
    row.verdict = "PASS" if ok else "FAIL"
    return row


def row_p7(blocks: dict, stream: Stream, flavour: str) -> Row:
    row = Row("P7", "state directories: the agent dir, the flavour's home, session files under --session-dir, the auth snapshot")
    row.add(f"agent-dir: {quote(blocks.get('agent-dir', ''), 400)}")
    row.add(f"pi-home: {quote(blocks.get('pi-home', ''), 400)}")
    row.add(f"sessions-dir: {quote(blocks.get('sessions-dir', ''), 300)}")
    row.add(f"auth: {quote(blocks.get('auth-sha', ''), 200)}")
    n = re.search(r"jsonl files under .*: (\d+)", blocks.get("sessions", ""))
    if not stream.events:
        return row
    expected_auth = "agent.db" if flavour == "omp" else "auth.json"
    row.verdict = "PASS" if n and int(n.group(1)) >= 1 and expected_auth in blocks.get("auth-sha", "") else "FAIL"
    return row


def row_p8(stream: Stream, raw: bytes, harness_rc: str, flavour: str, session_id: str | None) -> Row:
    row = Row("P8", "the run itself through the runner's own parser: session id, settlement, stop reason, exit, final message")
    rc = int(harness_rc) if harness_rc.lstrip("-").isdecimal() else None
    result = pi.FLAVOURS[flavour].parser(raw, rc, session_id)
    row.add(f"events={len(stream.events)} non-json-lines={stream.bad_lines} kinds={dict(stream.kinds)} harness.rc={harness_rc}")
    row.add(f"models seen: {stream.models()}")
    row.add(f"runner parser: session={result.session_id} settled={result.settled} stop={result.stop_reason} error={result.error}")
    row.add(f"final: {quote(stream.final or '', 300)}")
    if stream.events:
        row.verdict = "PASS" if result.error is None else "FAIL"
    return row


# -- the secret scan ------------------------------------------------------------------------------

def scan(paths: list[Path], values: dict[str, str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for path in paths:
        data = path.read_bytes()
        out[str(path)] = {label: data.count(value.encode()) for label, value in values.items() if value}
    return out


def scan_with_control(paths: list[Path], values: dict[str, str]) -> tuple[dict, int, int]:
    hits = scan(paths, values)
    planted = "PLANTED-" + secrets.token_hex(12)
    with tempfile.TemporaryDirectory(prefix="pim-scan-") as scratch:
        sample = Path(scratch) / "control.txt"
        base = paths[0].read_bytes() if paths else b""
        sample.write_bytes(base + b"\n" + planted.encode() + b"\n")
        control = scan([sample], {"planted": planted})[str(sample)]["planted"]
    total = sum(sum(v.values()) for v in hits.values())
    return hits, control, total


def append_evidence_scan(evidence: Path, values: dict[str, str]) -> str:
    files = sorted(p for p in evidence.rglob("*") if p.is_file())
    hits, control, total = scan_with_control(files, values)
    lines = [f"secret scan over the {len(files)} evidence files (values never printed):"]
    lines += [f"  {os.path.relpath(p, evidence)}: {json.dumps(c)}" for p, c in hits.items()]
    lines.append(f"  total hits: {total}; positive control: {control}")
    text = "\n".join(lines)
    with open(evidence / "checks.txt", "a") as stream:
        stream.write("\n" + text + "\n")
    return text


def load_secrets(pi_auth: str | None) -> dict[str, str]:
    if not pi_auth or not os.path.exists(os.path.expanduser(pi_auth)):
        return {}
    try:
        auth = pi.selected_auth(os.path.expanduser(pi_auth), 0)
    except pi.AuthError:
        return {}
    return {f"pi_secret_{i}": s for i, s in enumerate(auth.secrets)}


# -- the run ------------------------------------------------------------------------------------

def analyze(flavour: str, mode: str, downloads: Path, evidence: Path, codeword: str, values: dict[str, str],
            phrases: dict[str, str] | None = None, session_id: str | None = None) -> tuple[list[Row], str]:
    phrases = phrases or {}
    stream_path = downloads / "harness.out"
    raw = stream_path.read_bytes() if stream_path.exists() else b""
    stream = Stream(raw.decode(errors="replace").splitlines())
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "stream-events.txt").write_text("\n".join(stream.excerpt()) + "\n")
    exec_path = downloads / "exec-reads.txt"
    blocks = exec_blocks(exec_path.read_text(errors="replace")) if exec_path.exists() else {}
    report_path = downloads / "probe-report.md"
    report = report_path.read_text(errors="replace") if report_path.exists() else ""
    harness_rc = (downloads / "harness.rc").read_text().strip() if (downloads / "harness.rc").exists() else "?"
    found = sections(stream)
    rows = [row_p1(stream, report, mode, blocks), row_p2(stream, report, phrases, flavour, mode), row_p3(stream, found, blocks, mode),
            row_p4(found, blocks), row_p5(stream, blocks, report, codeword, flavour, mode), row_p6(found, blocks, flavour),
            row_p7(blocks, stream, flavour), row_p8(stream, raw, harness_rc, flavour, session_id)]
    head = [f"flavour={flavour} mode={mode} events={len(stream.events)} non-json-lines={stream.bad_lines} harness.rc={harness_rc}",
            f"session={(stream.session or {}).get('id')} models={stream.models()} kinds={dict(stream.kinds)}"]
    paths = sorted(p for p in downloads.rglob("*") if p.is_file())
    hits, control, total = scan_with_control(paths, values)
    scan_lines = [f"secret scan over {len(paths)} downloaded files, labels {sorted(values)} (values never printed):"]
    for path, counts in hits.items():
        scan_lines.append(f"  {os.path.relpath(path, downloads)}: {json.dumps(counts)} ({os.path.getsize(path)} bytes)")
    scan_lines.append(f"  total hits: {total}; positive control (planted string in a copy of the first file): {control}")
    text = "\n".join(head) + "\n\n" + "\n\n".join(r.render() for r in rows) + "\n\n" + "\n".join(scan_lines) + "\n"
    (evidence / "checks.txt").write_text(text)
    return rows, text


# -- self-test ------------------------------------------------------------------------------------

def _fixture(planted: str, hooked: bool) -> tuple[list[str], str, str]:
    sid = "7f1361e8-7451-4dfb-97fa-c6633deeec3e"

    def call(cid: str, name: str, args: dict, result: str, is_error: bool = False) -> list[dict]:
        return [{"type": "tool_execution_start", "toolCallId": cid, "toolName": name, "args": args},
                {"type": "tool_execution_end", "toolCallId": cid, "toolName": name, "isError": is_error,
                 "result": {"content": [{"type": "text", "text": result}], "details": {}}}]

    def assistant(text: str) -> dict:
        return {"type": "message_end", "message": {"role": "assistant", "model": "gpt-6-luna", "provider": "openai-codex",
                                                   "stopReason": "stop", "content": [{"type": "text", "text": text}]}}
    deny_result = "[pi-probe] denied by the probe extension (EXT-DENY-ME)" if hooked else "## P3b\n@@ deny\nEXT-DENY-ME\n"
    note_result = "## P3c\n@@ note\nEXT-NOTE-ME\n" + ("[pi-probe] note appended by the probe extension" if hooked else "")
    events = [
        {"type": "session", "version": 3, "id": sid, "cwd": "/sandbox/repo"},
        {"type": "agent_start"}, {"type": "turn_start"},
        *call("c1", "probe_echo", {"text": "hello-from-the-desk"}, "PROBE-ECHO-OK hello-from-the-desk"),
        *call("c2", "bash", {"command": "echo EXT-ALLOWED-OK"}, "## P3a\n@@ allowed\nEXT-ALLOWED-OK\n"),
        *call("c3", "bash", {"command": "echo EXT-DENY-ME"}, deny_result, is_error=hooked),
        *call("c4", "bash", {"command": "echo EXT-NOTE-ME"}, note_result),
        *call("c5", "bash", {"command": "git worktree add"}, "## P4\n@@ wt-add-tmp\nPreparing worktree\nrc=0\n@@ wt-list\n/sandbox/repo abc [main]\n/tmp/pi-wt-1 abc (detached HEAD)\n"
                                                             "@@ wt-remove\nrc=0\n@@ wt-add-usr\nfatal: could not create leading directories: Permission denied\nrc=128\n"),
        *call("c6", "bash", {"command": "env"}, f"## P6\n@@ env-count\n0\n@@ printenv-prefix\n\n@@ home\nHOME=/sandbox PI_CODING_AGENT_DIR=/tmp/pi-agent\n@@ usr-write\n"
                                                f"PermissionError: [Errno 13] Permission denied: '/usr/pi-mark'\nrc=1\n@@ state-write-home\n/sandbox/.local/state/pi-probe\nrc=0\n{planted}\n"),
        assistant("FINAL: CODEWORD-abc123"),
        {"type": "turn_end"},
        {"type": "agent_end", "messages": [{"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "FINAL: CODEWORD-abc123"}]}]},
        {"type": "agent_settled", "aborted": False},
    ]
    fire = ("-- /sandbox/.local/share/pi-probe/fires.jsonl lines=3\n"
            '{"event": "session_start", "boundary": {"fetch": "error TypeError ECONNREFUSED", "usr_write": "EACCES errno -13", "state_write": "ok /sandbox/.local/state/pi-probe"}}\n'
            '{"event": "tool_call", "tool": "bash", "decision": "block"}\n{"event": "tool_result", "tool": "bash", "decision": "note"}\n'
            if hooked else "-- /sandbox/.local/share/pi-probe/fires.jsonl absent\n")
    exec_reads = ("== firelog ==\n" + fire + "-- /tmp/pi-home/.local/share/pi-probe/fires.jsonl absent\n"
                  "== worktrees ==\n/sandbox/repo abc [main]\n== usr-paths ==\nls: cannot access '/usr/local/pi-wt': No such file or directory\n"
                  "== state ==\n-- /sandbox/.local/state\n== agent-dir ==\n/tmp/pi-agent\n/tmp/pi-agent/auth.json\n== auth-sha ==\nabc /tmp/pi-agent/auth.json\n"
                  "== sessions ==\njsonl files under [...]: 1\n-- /sandbox/pi-sessions/x.jsonl size=1\n   first={} cwd=/sandbox/repo\n   assistant models=[\"openai-codex/gpt-6-luna\"] kinds={}\n")
    report = "## P1\nPROBE-ECHO-OK hello-from-the-desk\n## P2\nprobe-skill: Code phrase: PHRASE-PROBE-aaa\nprobe-skill-home: NO-SKILL\nrepo-skill: NO-SKILL\nnope-skill: NO-SKILL\n"
    return [json.dumps(e) for e in events], exec_reads, report


def self_test(scratch: Path) -> int:
    scratch.mkdir(parents=True, exist_ok=True)
    planted = "PLANTED-TOKEN-" + secrets.token_hex(8)
    phrases = {"probe-skill": "PHRASE-PROBE-aaa", "probe-skill-home": "PHRASE-HOME-zzz", "repo-skill": "PHRASE-REPO-bbb"}
    results = {}
    for name, hooked in (("green", True), ("red", False)):
        downloads = scratch / name / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        lines, reads, report = _fixture(planted, hooked)
        (downloads / "harness.out").write_text("\n".join(lines) + "\n")
        (downloads / "exec-reads.txt").write_text(reads)
        (downloads / "probe-report.md").write_text(report)
        (downloads / "harness.rc").write_text("0\n")
        rows, text = analyze("pi", "mech", downloads, scratch / name / "evidence", "CODEWORD-abc123", {"planted_token": planted}, phrases,
                             "7f1361e8-7451-4dfb-97fa-c6633deeec3e")
        results[name] = (rows, text)
        print(f"--- self-test fixture '{name}' (extension fired: {hooked}) ---")
        print(text)
    green, green_text = results["green"]
    red, _ = results["red"]
    checks = {
        "P1 PASS on the probe_echo result": green[0].verdict == "PASS",
        "P2 PASS on the planted phrase": green[1].verdict == "PASS",
        "P3 PASS when the fire log, the block and the note are there": green[2].verdict == "PASS",
        "P3 FAIL when nothing fired and P3b ran": red[2].verdict == "FAIL",
        "P4 PASS on the planted worktree sections": green[3].verdict == "PASS",
        "P5 NOT EXERCISED without subagent evidence": green[4].verdict == "NOT EXERCISED",
        "P6 PASS on the planted boundary sections": green[5].verdict == "PASS",
        "P8 PASS through the runner's parser": green[7].verdict == "PASS",
        "scan counted the planted token exactly once": '"planted_token": 1' in green_text,
        "positive control counted 1": "positive control (planted string in a copy of the first file): 1" in green_text,
    }
    print("--- self-test assertions ---")
    for label, ok in checks.items():
        print(f"{'ok  ' if ok else 'FAIL'} {label}")
    return 0 if all(checks.values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Checks for the Pi-family mechanism probe.")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--scratch")
    parser.add_argument("--flavour", choices=("pi", "omp"))
    parser.add_argument("--mode", choices=("mech", "control", "statiker", "statiker-link", "tools-off"))
    parser.add_argument("--downloads")
    parser.add_argument("--evidence")
    parser.add_argument("--codeword", default="")
    parser.add_argument("--session-id")
    parser.add_argument("--prepare-summary")
    parser.add_argument("--pi-auth", default="~/.aos/worker/pi-auth.json")
    parser.add_argument("--evidence-scan", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        if args.scratch:
            return self_test(Path(args.scratch))
        with tempfile.TemporaryDirectory(prefix="pim-selftest-") as scratch:
            return self_test(Path(scratch))
    if not (args.flavour and args.mode and args.downloads and args.evidence):
        parser.error("--flavour, --mode, --downloads and --evidence are required without --self-test")
    values = load_secrets(args.pi_auth)
    phrases = json.loads(Path(args.prepare_summary).read_text())["phrases"] if args.prepare_summary else {}
    _, text = analyze(args.flavour, args.mode, Path(args.downloads), Path(args.evidence), args.codeword, values, phrases, args.session_id)
    print(text)
    if args.evidence_scan:
        print(append_evidence_scan(Path(args.evidence), values))
    return 0


if __name__ == "__main__":
    sys.exit(main())
