#!/usr/bin/env python3
"""Checks for the statiker-mechanism probe (docs/probes/2026-10-14-statiker-mechanisms.md).

Reads what one run left behind (Claude's stream-json `harness.out`, the in-sandbox exec reads the
driver saved as `exec-reads.txt`, the downloaded `probe-report.md`) and writes two files into the
evidence directory: `stream-events.txt` (the ordered event list: tool calls, hook events, tool
results, text, the subagent marker and the model of every assistant message) and `checks.txt` (one
block per row, R1-R7 for the main modes and S1-S4 for `stopguard`, each ending in PASS, FAIL or
NOT EXERCISED with the evidence lines it rests on, then the secret scan). A verdict here is the
mechanical reading of the evidence; the record quotes the lines and owns the final word.

The secret scan counts, per downloaded file, the occurrences of the real Claude OAuth token (read
from the worker's token file) and of the Codex auth file's token values; it never prints a value.
Its positive control plants a random string into a copy of the first scanned file and must count
exactly 1 in the same invocation.

    uv run python scripts/openshell_probe/statiker_mech/checks.py --mode plugin --downloads D \\
        --evidence E --codeword CODEWORD-xxxxxx [--claude-token-file F] [--codex-auth F]
    uv run python scripts/openshell_probe/statiker_mech/checks.py --self-test [--scratch DIR]
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

HEAD = 400
VERDICT_RE = re.compile(r"^(STATIKER-(?:GIT|RECORD) VERDICT): (\{.*\})\s*$")
BASE_DIR_RE = re.compile(r"Base directory for this skill: (\S+)")
SECTION_RE = re.compile(r"^@@ (\S+)\s*$")
HEADER_RE = re.compile(r"^## (R\d+[a-z0-9]*)\b")
STOPGUARD_TRACKER = "/sandbox/repo/.clippy/runs/2026-10-14-probe-statiker.md"
EXPECTED_HOOKS = ("PreToolUse:Agent", "PreToolUse:Bash", "PostToolUse:", "SubagentStop", "Stop")
AGENT_CALLS = {  # description -> row label
    "sonnet: preload probe": "R5a",
    "ungated probe": "R4(i)",
    "opus: probe": "R4(ii)",
    "haiku: probe": "R4(iii)",
    "sonnet: probe": "R4(iv)",
    "sonnet: push probe": "R4(v)",
}
MODEL_FAMILY = {"R4(ii)": "opus", "R4(iii)": "haiku", "R4(iv)": "sonnet"}
CURL_DENIED = ("curl: (6)", "curl: (7)", "curl: (28)", "curl: (35)", "curl: (56)", "Could not resolve host",
               "Failed to connect", "403", "Forbidden", "Connection refused", "denied")
FS_DENIED = ("Permission denied", "Read-only file system", "Operation not permitted")


def short(value: object, limit: int = HEAD) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + f"... [{len(text)} chars]"


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")) if item.get("type", "text") == "text" else json.dumps(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return "" if content is None else json.dumps(content)


# -- the stream ------------------------------------------------------------------------------

class Stream:
    """Claude's stream-json output, indexed: tool calls, results, hook events, text, models."""

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
        self.init = next((e for e in self.events if e.get("type") == "system" and e.get("subtype") == "init"), None)
        self.result = next((e for e in reversed(self.events) if e.get("type") == "result"), None)
        self.tool_uses: dict[str, dict] = {}
        self.tool_results: dict[str, dict] = {}
        self.hooks: list[dict] = []
        self.texts: list[dict] = []
        self.models: dict[str | None, list[str]] = collections.defaultdict(list)
        for index, event in enumerate(self.events):
            kind, sub = event.get("type"), event.get("subtype")
            parent = event.get("parent_tool_use_id")
            if kind == "system" and sub in ("hook_started", "hook_response"):
                self.hooks.append({"index": index, "sub": sub, **{k: event.get(k) for k in (
                    "hook_name", "hook_event", "exit_code", "outcome", "stdout", "stderr", "output", "tool_use_id")}})
            if kind not in ("assistant", "user"):
                continue
            message = event.get("message") or {}
            if kind == "assistant" and message.get("model"):
                self.models[parent].append(message["model"])
            content = message.get("content")
            if isinstance(content, str):
                self.texts.append({"index": index, "kind": kind, "parent": parent, "text": content})
                continue
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    self.texts.append({"index": index, "kind": kind, "parent": parent, "text": block.get("text") or ""})
                elif btype == "tool_use":
                    self.tool_uses[block.get("id")] = {"index": index, "parent": parent, "name": block.get("name"),
                                                       "input": block.get("input") or {}, "id": block.get("id")}
                elif btype == "tool_result":
                    self.tool_results[block.get("tool_use_id")] = {
                        "index": index, "parent": parent, "is_error": bool(block.get("is_error")),
                        "text": content_text(block.get("content"))}

    def desk_calls(self, name: str) -> list[dict]:
        return sorted((t for t in self.tool_uses.values() if t["name"] == name and t["parent"] is None),
                      key=lambda t: t["index"])

    def result_of(self, tool_use_id: str) -> dict | None:
        return self.tool_results.get(tool_use_id)

    def events_under(self, tool_use_id: str) -> list[dict]:
        return [e for e in self.events if e.get("parent_tool_use_id") == tool_use_id]

    def hooks_between(self, start: int, end: int) -> list[dict]:
        return [h for h in self.hooks if start < h["index"] < end]

    def excerpt(self) -> list[str]:
        out = []
        for index, event in enumerate(self.events):
            kind, sub = event.get("type"), event.get("subtype")
            parent = event.get("parent_tool_use_id")
            tag = f"[sub {str(parent)[-6:]}] " if parent else ""
            if kind == "system" and sub == "init":
                out.append(f"{tag}system/init model={event.get('model')} permissionMode={event.get('permissionMode')} "
                           f"tools={len(event.get('tools') or [])} skills={short(event.get('skills'), 600)} "
                           f"plugins={short(event.get('plugins'), 600)} agents={short(event.get('agents'), 300)} "
                           f"slash_commands={short(event.get('slash_commands'), 300)}")
            elif kind == "system" and sub in ("hook_started", "hook_response"):
                fields = {k: event.get(k) for k in ("hook_name", "hook_event", "exit_code", "outcome")}
                line = f"{tag}system/{sub} " + " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
                for key in ("stdout", "stderr", "output"):
                    if event.get(key):
                        line += f" {key}={short(event[key])}"
                out.append(line)
            elif kind == "system":
                out.append(f"{tag}system/{sub} {short({k: v for k, v in event.items() if k not in ('type', 'subtype')}, 300)}")
            elif kind in ("assistant", "user"):
                message = event.get("message") or {}
                model = f" model={message.get('model')}" if kind == "assistant" and message.get("model") else ""
                content = message.get("content")
                if isinstance(content, str):
                    out.append(f"{tag}{kind}/text{model} {short(content)}")
                    continue
                for block in content or []:
                    if not isinstance(block, dict):
                        out.append(f"{tag}{kind}/text{model} {short(block)}")
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        out.append(f"{tag}{kind}/text{model} {short(block.get('text'))}")
                    elif btype == "tool_use":
                        out.append(f"{tag}{kind}/tool_use{model} {block.get('name')} id=...{str(block.get('id'))[-6:]} "
                                   f"{short(block.get('input'), 500)}")
                    elif btype == "tool_result":
                        out.append(f"{tag}{kind}/tool_result id=...{str(block.get('tool_use_id'))[-6:]} "
                                   f"is_error={block.get('is_error', False)} {short(content_text(block.get('content')))}")
                    else:
                        out.append(f"{tag}{kind}/{btype}{model} {short(block, 200)}")
            elif kind == "result":
                out.append(f"result subtype={event.get('subtype')} is_error={event.get('is_error')} "
                           f"num_turns={event.get('num_turns')} duration_ms={event.get('duration_ms')} "
                           f"cost_usd={event.get('total_cost_usd')} permission_denials={short(event.get('permission_denials'), 600)} "
                           f"result={short(event.get('result'))}")
            else:
                out.append(f"{tag}{kind}/{sub} {short(event, 200)}")
        return [f"{i:03d} {line}" for i, line in enumerate(out)]


# -- sections in Bash output ------------------------------------------------------------------

def sections(stream: Stream) -> dict[tuple[str, str], str]:
    """(header, label) -> text, from the desk's Bash results whose commands echo `## R<n>` and
    `@@ <label>` lines (the prompts' tee'd steps). The first occurrence of a pair wins."""
    found: dict[tuple[str, str], str] = {}
    for call in stream.desk_calls("Bash"):
        result = stream.result_of(call["id"])
        if result is None:
            continue
        header, label, buf = "?", None, []
        for line in result["text"].splitlines():
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


def verdicts(text: str) -> list[tuple[str, str, str, str]]:
    """Every VERDICT line: (prefix, verdict, route, the line)."""
    out = []
    for line in text.splitlines():
        m = VERDICT_RE.match(line.strip())
        if not m:
            continue
        try:
            body = json.loads(m.group(2))
        except ValueError:
            out.append((m.group(1), "?", "?", line.strip()))
            continue
        out.append((m.group(1), str(body.get("verdict")), str(body.get("route")), line.strip()))
    return out


def rc_of(text: str) -> str | None:
    found = re.findall(r"^rc=(-?\d+)\s*$", text, re.M)
    return found[-1] if found else None


# -- exec reads --------------------------------------------------------------------------------

def exec_blocks(text: str) -> dict[str, str]:
    """`== label ==` blocks of the driver's exec-reads.txt."""
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
    """The text after a `-- <path>` line inside an exec block, up to the next `-- ` line."""
    out, inside = [], False
    for line in block.splitlines():
        if line.startswith("-- "):
            if inside:
                break
            inside = line[3:].strip() == path
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


# -- rows ----------------------------------------------------------------------------------------

class Row:
    def __init__(self, name: str, title: str) -> None:
        self.name, self.title, self.lines, self.verdict = name, title, [], "NOT EXERCISED"

    def add(self, line: str) -> None:
        self.lines.append(line)

    def render(self) -> str:
        body = "\n".join(f"  {line}" for line in self.lines) if self.lines else "  (no evidence line)"
        return f"{self.name} {self.title}\n{body}\n  => {self.verdict}"


def quote(text: str, limit: int = 300) -> str:
    return short(text.strip(), limit)


def row_r1(stream: Stream, mode: str) -> Row:
    row = Row("R1", "Skill tool (statiker:statiker, dispatch-guards:dispatch, negative statiker:nope)")
    calls = stream.desk_calls("Skill")
    if stream.init is not None:
        row.add(f"init skills={short(stream.init.get('skills'), 500)}")
        row.add(f"init plugins={short(stream.init.get('plugins'), 500)}")
    injected = [(t["index"], m.group(1)) for t in stream.texts for m in [BASE_DIR_RE.search(t["text"])] if m]
    for index, path in injected:
        row.add(f"event {index}: 'Base directory for this skill: {path}'")
    version_hits = sum(t["text"].count("0.2.104") for t in stream.texts if BASE_DIR_RE.search(t["text"]))
    row.add(f"'0.2.104' occurrences inside injected skill texts: {version_hits}")
    if not calls:
        row.add("no Skill tool_use by the desk")
        return row
    outcome: dict[str, tuple[bool, str]] = {}
    for call in calls:
        skill = str(call["input"].get("skill") or call["input"].get("command") or call["input"])
        result = stream.result_of(call["id"])
        if result is None:
            row.add(f"event {call['index']}: Skill {skill}: no tool_result")
            continue
        row.add(f"event {call['index']}: Skill {skill} -> is_error={result['is_error']} {quote(result['text'], 240)}")
        outcome.setdefault(skill, (result["is_error"], result["text"]))
    denials = [d for d in (stream.result or {}).get("permission_denials") or [] if d.get("tool_name") == "Skill"]
    row.add(f"result.permission_denials naming Skill: {len(denials)}")
    stat_ok = outcome.get("statiker:statiker", (True, ""))[0] is False and any(p.endswith("/statiker") for _, p in injected)
    disp_ok = outcome.get("dispatch-guards:dispatch", (True, ""))[0] is False and any(p.endswith("/dispatch") for _, p in injected)
    nope = outcome.get("statiker:nope")
    nope_err = nope is not None and (nope[0] or "unknown" in nope[1].lower() or "not found" in nope[1].lower())
    if mode == "control":
        refused = all(err for err, _ in outcome.values()) and not injected
        unknown = sorted(s for s, (err, text) in outcome.items() if err and "Unknown skill" in text)
        row.add(f"refused as 'Unknown skill' (the lookup, not the permission): {unknown}")
        # A permission refusal names the permission, or lands in result.permission_denials; an
        # unknown-skill or disable-model-invocation answer is the skill's own state, not the
        # permission (control run 2's "verify" answer was the latter).
        permission = bool(denials) or any(
            err and any(w in text.lower() for w in ("permission", "not allowed", "denied"))
            for err, text in outcome.values())
        row.add("expected (control): every Skill call refused, no skill text injected, and one refusal by the "
                f"Skill permission itself; permission-shaped refusal seen: {permission}")
        if injected or not outcome:
            row.verdict = "FAIL"
        else:
            row.verdict = "PASS" if refused and permission else "NOT EXERCISED"
    else:
        row.add(f"statiker loaded={stat_ok} dispatch loaded={disp_ok} nonexistent errored={nope_err}")
        row.verdict = "PASS" if stat_ok and disp_ok and nope_err else "FAIL"
    return row


def row_r2(stream: Stream, found: dict, blocks: dict) -> Row:
    row = Row("R2", "statiker record and git tools (lint, closure, filter, state-gate, preflight)")
    labels = ("lint", "closure", "filter", "state-gate", "preflight")
    seen = {label: found.get(("R2", label)) for label in labels}
    if all(v is None for v in seen.values()):
        row.add("no R2 section in the desk's Bash output")
        return row
    ok = True
    for label in labels:
        text = seen[label]
        if text is None:
            row.add(f"{label}: no output section")
            ok = False
            continue
        lines = verdicts(text)
        for prefix, verdict, route, line in lines:
            row.add(f"{label}: rc={rc_of(text)} {quote(line, 260)}")
        if len(lines) != 1 or lines[0][2] in ("", "None", "?"):
            row.add(f"{label}: {len(lines)} VERDICT lines (expected exactly one with a route)")
            ok = False
    filt = verdicts(seen["filter"] or "")
    if not filt or filt[0][1] != "ARTIFACT_WRITTEN":
        ok = False
    artifact = blocks.get("filter-artifact", "")
    row.add(f"exec /tmp/stk-filter.md: {quote(artifact, 200)}")
    if "exists" not in artifact:
        ok = False
    row.add(f"exec ~/.local/state/statiker after the run: {quote(blocks.get('statiker-state', '(no read)'), 400)}")
    row.verdict = "PASS" if ok else "FAIL"
    return row


def row_r3(stream: Stream, found: dict, blocks: dict) -> Row:
    row = Row("R3", "worktrees via statiker_git.py (add/list/remove; inside-repo and /usr negatives)")
    if ("R3", "wt-add-tmp") not in found:
        row.add("no R3 section in the desk's Bash output")
        return row
    expect = {"wt-add-tmp": "WORKTREE_ADDED", "wt-remove": "WORKTREE_REMOVED", "wt-add-inside": "PATH_INSIDE_REPO"}
    ok = True
    for label, want in expect.items():
        text = found.get(("R3", label), "")
        got = verdicts(text)
        row.add(f"{label}: rc={rc_of(text)} " + (quote(got[0][3], 260) if got else "no VERDICT line"))
        ok &= bool(got) and got[0][1] == want
    usr = found.get(("R3", "wt-add-usr"), "")
    got = verdicts(usr)
    row.add(f"wt-add-usr: rc={rc_of(usr)} " + (quote(got[0][3], 400) if got else "no VERDICT line"))
    usr_denied = bool(got) and got[0][1] != "WORKTREE_ADDED" and any(s in usr for s in FS_DENIED)
    ok &= usr_denied
    listing = found.get(("R3", "wt-list"), "")
    row.add(f"wt-list: {quote(listing, 300)}")
    ok &= "/tmp/stk-wt-1" in listing
    row.add(f"in-run before: {quote(found.get(('R3', 'worktrees-before'), ''), 200)}")
    row.add(f"in-run after: {quote(found.get(('R3', 'worktrees-after'), ''), 300)}")
    post = blocks.get("worktrees", "")
    row.add(f"exec after the run: {quote(post, 400)}")
    ok &= bool(post) and "stk-wt" not in post
    usr_exists = blocks.get("usr-paths", "")
    row.add(f"exec /usr paths: {quote(usr_exists, 200)}")
    ok &= "No such file" in usr_exists or "absent" in usr_exists
    row.verdict = "PASS" if ok else "FAIL"
    return row


def agent_call_report(stream: Stream, call: dict, row: Row, label: str) -> dict:
    """Evidence for one desk Agent call; returns the facts the verdicts use."""
    ti = call["input"]
    result = stream.result_of(call["id"])
    end = result["index"] if result else len(stream.events)
    fields = {k: ti.get(k) for k in ("subagent_type", "model", "name", "description")}
    row.add(f"{label} event {call['index']}: Agent {json.dumps(fields)}")
    hooks = stream.hooks_between(call["index"], end)
    agent_hooks = [h for h in hooks if str(h.get("hook_name") or "").startswith("PreToolUse:Agent")]
    for h in agent_hooks:
        if h["sub"] == "hook_response":
            row.add(f"{label}   hook_response {h.get('hook_name')} exit={h.get('exit_code')} outcome={h.get('outcome')} "
                    f"stdout={quote(str(h.get('stdout') or ''), 220)} stderr={quote(str(h.get('stderr') or ''), 220)}")
    started = collections.Counter(h.get("hook_name") for h in agent_hooks if h["sub"] == "hook_started")
    if started:
        row.add(f"{label}   hook_started PreToolUse:Agent x{sum(started.values())}")
    sub_events = stream.events_under(call["id"])
    models = sorted(set(stream.models.get(call["id"], [])))
    row.add(f"{label}   subagent events={len(sub_events)} models={models}")
    if result is not None:
        row.add(f"{label}   tool_result is_error={result['is_error']} {quote(result['text'], 300)}")
    else:
        row.add(f"{label}   no tool_result")
    joined = " ".join(str(h.get("stdout") or "") + str(h.get("stderr") or "") for h in agent_hooks)
    joined += " " + (result["text"] if result else "")
    return {"result": result, "models": models, "sub_events": sub_events, "hook_text": joined, "hooks": agent_hooks}


def row_r4(stream: Stream, blocks: dict, mode: str, codeword: str) -> Row:
    row = Row("R4", "subagents with an explicit model (opus, haiku, sonnet), the model gate, the push gate")
    calls = stream.desk_calls("Agent")
    if not calls:
        row.add("no Agent tool_use by the desk")
        return row
    by_label: dict[str, list[dict]] = collections.defaultdict(list)
    for call in calls:
        label = AGENT_CALLS.get(str(call["input"].get("description") or "").strip(), "unclassified")
        by_label[label].append(call)
    facts: dict[str, list[dict]] = collections.defaultdict(list)
    for label in ("R4(i)", "R4(ii)", "R4(iii)", "R4(iv)", "R4(v)", "unclassified"):
        for call in by_label.get(label, []):
            facts[label].append(agent_call_report(stream, call, row, label))
    files = blocks.get("subagent-files", "")
    row.add(f"exec subagent files: {quote(files, 900)}")
    verdict = {}
    # (i): denied by agent-model-gate in hook modes, runs in control.
    if facts.get("R4(i)"):
        f = facts["R4(i)"][0]
        if mode == "control":
            verdict["i"] = bool(f["sub_events"]) or (f["result"] is not None and not f["result"]["is_error"])
        else:
            verdict["i"] = (f["result"] is not None and f["result"]["is_error"] and not f["sub_events"]
                            and "Model gate" in f["hook_text"])
    # (ii)-(iv): the subagent ran on the requested family, wrote its files, curl denied.
    for label, key in (("R4(ii)", "ii"), ("R4(iii)", "iii"), ("R4(iv)", "iv")):
        family = MODEL_FAMILY[label]
        runs = facts.get(label) or []
        ran = [f for f in runs if f["models"]]
        model_ok = bool(ran) and all(all(family in m for m in f["models"]) for f in ran)
        block = inner(files, f"/sandbox/sub-{family}.txt")
        curl = inner(files, f"/sandbox/sub-{family}-curl.txt")
        file_ok = family in block.lower() and "No such file" not in block
        curl_ok = bool(curl.strip()) and "No such file" not in curl and any(s in curl for s in CURL_DENIED + ("rc=", "exit"))
        row.add(f"{label}: subagent models={[f['models'] for f in runs]} file={quote(block, 120)} curl={quote(curl, 160)}")
        verdict[key] = model_ok and file_ok and curl_ok if runs else None
    # (v): push denied by subagent-push-gate in hook modes; a plain git failure in control.
    if facts.get("R4(v)"):
        f = facts["R4(v)"][0]
        push_results = []
        for event in f["sub_events"]:
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and "push" in json.dumps(block.get("input")):
                    r = stream.result_of(block.get("id"))
                    push_results.append(r["text"] if r else "(no result)")
        sub_hooks = [h for h in stream.hooks if h["sub"] == "hook_response" and "subagent-push-gate" in
                     (str(h.get("stdout") or "") + str(h.get("stderr") or ""))]
        for text in push_results:
            row.add(f"R4(v)   subagent push tool_result: {quote(text, 300)}")
        for h in sub_hooks[:2]:
            row.add(f"R4(v)   hook_response {h.get('hook_name')} event {h['index']}: {quote(str(h.get('stdout')), 260)}")
        if mode == "control":
            verdict["v"] = any(("fatal" in t.lower() or "does not appear" in t.lower() or "error" in t.lower())
                               for t in push_results)
        else:
            verdict["v"] = bool(sub_hooks) or any("subagent-push-gate" in t for t in push_results)
    # name probe on (ii)
    for f in facts.get("R4(ii)", []):
        if f["result"] is not None and "name" in f["result"]["text"].lower():
            row.add(f"R4(ii) name probe: {quote(f['result']['text'], 300)}")
    # the code word: never passed to a subagent, absent from every subagent's output and files
    passed = sum(json.dumps(c["input"]).count(codeword) for c in calls) if codeword else 0
    sub_hits = sum(json.dumps(e).count(codeword) for e in stream.events if e.get("parent_tool_use_id")) if codeword else 0
    file_hits = files.count(codeword) if codeword else 0
    desk_hits = str((stream.result or {}).get("result") or "").count(codeword) if codeword else 0
    row.add(f"code word: in Agent inputs={passed}, in subagent events={sub_hits}, in subagent files={file_hits}; "
            f"positive control, in the desk's final result={desk_hits}")
    cw_ok = passed == 0 and sub_hits == 0 and file_hits == 0 and desk_hits >= 1
    row.add(f"sub-verdicts: {verdict}; code word clean with control: {cw_ok}")
    exercised = [v for v in verdict.values() if v is not None]
    if not exercised:
        return row
    row.verdict = "PASS" if all(exercised) and len(exercised) == 5 and cw_ok else "FAIL"
    return row


def row_r5(stream: Stream, blocks: dict, mode: str) -> Row:
    row = Row("R5", "hooks: the hook_started set, the skill gate before the dispatch skill load, the fire log")
    started = collections.Counter(h.get("hook_name") for h in stream.hooks if h["sub"] == "hook_started")
    row.add(f"hook_started counts: {dict(sorted(started.items(), key=lambda kv: str(kv[0])))}")
    responses = [h for h in stream.hooks if h["sub"] == "hook_response"]
    denies = [h for h in responses if '"permissionDecision": "deny"' in str(h.get("stdout")) or
              '"permissionDecision":"deny"' in str(h.get("stdout")) or h.get("exit_code") == 2]
    row.add(f"hook_response total={len(responses)} deny-or-exit-2={len(denies)}")
    present = {name: any(str(k).startswith(name) for k in started) for name in EXPECTED_HOOKS}
    row.add(f"expected names present: {present}")
    pre = [c for c in stream.desk_calls("Agent") if str(c["input"].get("description") or "").strip() == "sonnet: preload probe"]
    gate = False
    if pre:
        tmp = Row("", "")
        f = agent_call_report(stream, pre[0], tmp, "R5a")
        for line in tmp.lines:
            row.add(line)
        gate = "dispatch-skill-gate" in f["hook_text"] and f["result"] is not None and f["result"]["is_error"]
        ran = bool(f["sub_events"]) or (f["result"] is not None and not f["result"]["is_error"])
    else:
        row.add("R5a: no pre-load Agent call")
        ran = False
    firelog = blocks.get("firelog", "")
    row.add(f"exec ~/.local/share/claude: {quote(firelog, 500)}")
    # A fire log counts only as a listed file with a line count (an error message naming the glob
    # does not: stkm-plugin run 1 passed on exactly that text before this predicate).
    logged = re.findall(r"^-- (\S+\.jsonl) lines=(\d+)", firelog, re.M)
    row.add(f"fire-log files with line counts: {logged}")
    if not stream.events:
        return row
    if mode == "control":
        row.verdict = "PASS" if not started and pre and ran else "FAIL"
    else:
        row.verdict = "PASS" if all(present.values()) and gate and any(int(n) > 0 for _, n in logged) else "FAIL"
    if not pre and not started:
        row.verdict = "NOT EXERCISED"
    return row


def row_r6(stream: Stream, found: dict, blocks: dict) -> Row:
    row = Row("R6", "Codex as implementer inside the sandbox (codex exec), with the network negative control")
    codex = [c for c in stream.desk_calls("Bash") if "codex exec" in str(c["input"].get("command"))]
    if not codex:
        row.add("no codex exec Bash call by the desk")
        return row
    for call in codex:
        result = stream.result_of(call["id"])
        cmd = str(call["input"].get("command"))
        model = re.search(r"-m (\S+)", cmd)
        sandbox = "danger-full-access" if "danger-full-access" in cmd else ("workspace-write" if "workspace-write" in cmd else "?")
        header = HEADER_RE.search(result["text"]) if result else None
        row.add(f"event {call['index']}: {header.group(1) if header else '?'} model={model.group(1) if model else '?'} "
                f"sandbox={sandbox} timeout={call['input'].get('timeout')} rc={rc_of(result['text']) if result else None}")
        if result is not None:
            tail = result["text"][-600:]
            row.add(f"  output tail: {quote(tail, 600)}")
    hello = blocks.get("codex-hello", "")
    row.add(f"exec /sandbox/repo/codex-hello.txt: {quote(hello, 200)}")
    file_ok = "terra-ok" in hello and "No such file" not in hello
    denied = False
    for key in ("R6b", "R6d"):
        for label in ("stdout", "stderr-tail"):
            text = found.get((key, label), "")
            if any(s in text for s in CURL_DENIED):
                denied = True
    for key in ("R6b", "R6d"):
        if any((key, label) in found for label in ("stdout", "stderr-tail")):
            row.add(f"{key} stdout: {quote(found.get((key, 'stdout'), ''), 300)}")
    row.add(f"file created: {file_ok}; network denial seen in a negative control: {denied}")
    row.verdict = "PASS" if file_ok and denied else "FAIL"
    return row


def row_r7(found: dict) -> Row:
    row = Row("R7", "boundaries through the mechanisms: the token placeholder, a /usr write, the statiker state write")
    if ("R7", "env-count") not in found:
        row.add("no R7 section in the desk's Bash output")
        return row
    count = found.get(("R7", "env-count"), "").strip()
    prefix = found.get(("R7", "printenv-prefix"), "").strip()
    usr = found.get(("R7", "usr-write"), "")
    state = found.get(("R7", "state-write"), "")
    mkdir = found.get(("R7", "state-write-mkdir"), "")
    row.add(f"env | grep -c CLAUDE_CODE_OAUTH_TOKEN: {quote(count, 40)}")
    row.add(f"printenv ... | cut -c1-12: {quote(prefix, 40)}")
    row.add(f"/usr/stk-mark: rc={rc_of(usr)} {quote(usr, 260)}")
    row.add(f"~/.local/state/statiker/mark (literal): rc={rc_of(state)} {quote(state, 260)}")
    row.add(f"~/.local/state/statiker/mark2 (makedirs first): rc={rc_of(mkdir)} {quote(mkdir, 300)}")
    home = found.get(("R7", "state-write-home"))
    if home is not None:
        row.add(f"expanduser('~/.local/state/statiker')/mark3: rc={rc_of(home)} {quote(home, 200)}")
    ok = (count.splitlines()[:1] == ["1"] and prefix.startswith("openshell:")
          and rc_of(usr) not in (None, "0") and any(s in usr for s in FS_DENIED) and rc_of(mkdir) == "0")
    row.verdict = "PASS" if ok else "FAIL"
    return row


def rows_stopguard(stream: Stream, blocks: dict, harness_rc: str) -> list[Row]:
    s1 = Row("S1", "the Stop guard reads the -statiker.md fixture under cwd /sandbox/repo and blocks")
    stops = [h for h in stream.hooks if str(h.get("hook_name") or "") == "Stop"]
    started = sum(1 for h in stops if h["sub"] == "hook_started")
    blocks_seen = [h for h in stops if h["sub"] == "hook_response" and '"decision": "block"' in str(h.get("stdout"))]
    s1.add(f"hook_started Stop x{started}; hook_response with decision block x{len(blocks_seen)}")
    for h in blocks_seen[:2]:
        s1.add(f"event {h['index']}: exit={h.get('exit_code')} stdout={quote(str(h.get('stdout')), 400)}")
    if stops:
        s1.verdict = "PASS" if blocks_seen and STOPGUARD_TRACKER in str(blocks_seen[0].get("stdout")) else "FAIL"
    s2 = Row("S2", "the block reaches the model as Stop hook feedback")
    fed = [t for t in stream.texts if "Stop hook feedback" in t["text"] or "P16 stop-guard" in t["text"]]
    for t in fed[:2]:
        s2.add(f"event {t['index']} {t['kind']}: {quote(t['text'], 300)}")
    if blocks_seen:
        s2.verdict = "PASS" if fed else "FAIL"
    s3 = Row("S3", "the run ends (the guard keeps blocking; the turn cap ends it)")
    res = stream.result or {}
    s3.add(f"result subtype={res.get('subtype')} is_error={res.get('is_error')} num_turns={res.get('num_turns')} "
           f"cost_usd={res.get('total_cost_usd')} harness.rc={harness_rc}")
    if stream.events:
        s3.verdict = "PASS" if res else "FAIL"
    s4 = Row("S4", "the one-step task ran")
    ran = blocks.get("stopguard-file", "")
    s4.add(f"exec /sandbox/repo/stopguard-ran.txt: {quote(ran, 120)}")
    if stream.events:
        s4.verdict = "PASS" if "stopguard-ran" in ran and "No such file" not in ran else "FAIL"
    return [s1, s2, s3, s4]


# -- the secret scan ------------------------------------------------------------------------------

def scan(paths: list[Path], values: dict[str, str]) -> dict[str, dict[str, int]]:
    """Occurrence counts per file per secret label; values are never returned or printed."""
    out: dict[str, dict[str, int]] = {}
    for path in paths:
        data = path.read_bytes()
        out[str(path)] = {label: data.count(value.encode()) for label, value in values.items() if value}
    return out


def scan_with_control(paths: list[Path], values: dict[str, str]) -> tuple[dict, int, int]:
    """The scan over `paths` plus its positive control: a random string planted once into a copy of
    the first file must count exactly 1 with the same function."""
    hits = scan(paths, values)
    planted = "PLANTED-" + secrets.token_hex(12)
    with tempfile.TemporaryDirectory(prefix="stkm-scan-") as scratch:
        sample = Path(scratch) / "control.txt"
        base = paths[0].read_bytes() if paths else b""
        sample.write_bytes(base + b"\n" + planted.encode() + b"\n")
        control = scan([sample], {"planted": planted})[str(sample)]["planted"]
    total = sum(sum(v.values()) for v in hits.values())
    return hits, control, total


def append_evidence_scan(evidence: Path, values: dict[str, str]) -> str:
    """The evidence directory itself, scanned before anything in it is copied into the record;
    appended to checks.txt."""
    files = sorted(p for p in evidence.rglob("*") if p.is_file())
    hits, control, total = scan_with_control(files, values)
    lines = [f"secret scan over the {len(files)} evidence files (values never printed):"]
    lines += [f"  {os.path.relpath(p, evidence)}: {json.dumps(c)}" for p, c in hits.items()]
    lines.append(f"  total hits: {total}; positive control: {control}")
    text = "\n".join(lines)
    with open(evidence / "checks.txt", "a") as stream:
        stream.write("\n" + text + "\n")
    return text


def load_secrets(claude_token_file: str | None, codex_auth: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if claude_token_file and os.path.exists(os.path.expanduser(claude_token_file)):
        values["claude_oauth_token"] = Path(os.path.expanduser(claude_token_file)).read_text().strip()
    if codex_auth and os.path.exists(os.path.expanduser(codex_auth)):
        tokens = (json.loads(Path(os.path.expanduser(codex_auth)).read_text()).get("tokens") or {})
        for key in ("refresh_token", "access_token", "id_token"):
            if tokens.get(key):
                values[f"codex_{key}"] = str(tokens[key])
    return values


# -- the run ------------------------------------------------------------------------------------

def analyze(mode: str, downloads: Path, evidence: Path, codeword: str, values: dict[str, str]) -> tuple[list[Row], str]:
    stream_path = downloads / "harness.out"
    stream = Stream(stream_path.read_text(errors="replace").splitlines() if stream_path.exists() else [])
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "stream-events.txt").write_text("\n".join(stream.excerpt()) + "\n")
    exec_path = downloads / "exec-reads.txt"
    blocks = exec_blocks(exec_path.read_text(errors="replace")) if exec_path.exists() else {}
    harness_rc = (downloads / "harness.rc").read_text().strip() if (downloads / "harness.rc").exists() else "?"
    found = sections(stream)
    if mode == "stopguard":
        rows = rows_stopguard(stream, blocks, harness_rc)
    else:
        rows = [row_r1(stream, mode), row_r2(stream, found, blocks), row_r3(stream, found, blocks),
                row_r4(stream, blocks, mode, codeword), row_r5(stream, blocks, mode), row_r6(stream, found, blocks),
                row_r7(found)]
    res = stream.result or {}
    head = [f"mode={mode} events={len(stream.events)} non-json-lines={stream.bad_lines} harness.rc={harness_rc}",
            f"init model={(stream.init or {}).get('model')} permissionMode={(stream.init or {}).get('permissionMode')}",
            f"result subtype={res.get('subtype')} is_error={res.get('is_error')} num_turns={res.get('num_turns')} "
            f"duration_ms={res.get('duration_ms')} cost_usd={res.get('total_cost_usd')}",
            f"models by context: {json.dumps({str(k)[-6:] if k else 'desk': sorted(set(v)) for k, v in stream.models.items()})}"]
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

def _fixture_stream(planted: str, inject: bool = True) -> list[str]:
    def a(content, parent=None, model="claude-sonnet-5-5"):
        return {"type": "assistant", "parent_tool_use_id": parent, "message": {"model": model, "content": content}}

    def u(content, parent=None):
        return {"type": "user", "parent_tool_use_id": parent, "message": {"content": content}}

    events = [
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5", "permissionMode": "dontAsk",
         "tools": ["Bash", "Skill", "Agent"], "skills": ["statiker:statiker", "dispatch-guards:dispatch"]},
        {"type": "system", "subtype": "hook_started", "hook_name": "PreToolUse:Agent", "hook_event": "PreToolUse"},
        {"type": "system", "subtype": "hook_response", "hook_name": "PreToolUse:Agent", "hook_event": "PreToolUse",
         "exit_code": 0, "outcome": "success",
         "stdout": json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                      "permissionDecisionReason": "[dispatch-guards/dispatch-skill-gate] load it"}})},
        a([{"type": "tool_use", "id": "toolu_pre", "name": "Agent",
            "input": {"subagent_type": "general-purpose", "model": "sonnet", "description": "sonnet: preload probe",
                      "prompt": "Reply with the single word PRELOAD-OK and nothing else."}}]),
        u([{"type": "tool_result", "tool_use_id": "toolu_pre", "is_error": True,
            "content": "Hook PreToolUse:Agent denied this tool: [dispatch-guards/dispatch-skill-gate] load it"}]),
        a([{"type": "tool_use", "id": "toolu_s1", "name": "Skill", "input": {"skill": "statiker:statiker"}}]),
        u([{"type": "tool_result", "tool_use_id": "toolu_s1", "content": "Launching skill: statiker:statiker"}]),
        a([{"type": "tool_use", "id": "toolu_s2", "name": "Skill", "input": {"skill": "dispatch-guards:dispatch"}}]),
        u([{"type": "tool_result", "tool_use_id": "toolu_s2", "content": "Launching skill: dispatch-guards:dispatch"}]),
        a([{"type": "tool_use", "id": "toolu_s3", "name": "Skill", "input": {"skill": "statiker:nope"}}]),
        u([{"type": "tool_result", "tool_use_id": "toolu_s3", "is_error": True, "content": "Unknown skill: statiker:nope"}]),
        a([{"type": "tool_use", "id": "toolu_b1", "name": "Bash", "input": {"command": "echo hi"}}]),
        u([{"type": "tool_result", "tool_use_id": "toolu_b1", "content": f"hi {planted}"}]),
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 6, "total_cost_usd": 0.01,
         "result": "FINAL: CODEWORD-abc123", "permission_denials": []},
    ]
    if inject:
        events[7:7] = [u([{"type": "text", "text": "Base directory for this skill: /sandbox/plugins/statiker/skills/statiker\n\n# Statiker"}])]
        events[10:10] = [u([{"type": "text", "text": "Base directory for this skill: /sandbox/plugins/dispatch-guards/skills/dispatch\n\n# Dispatch"}])]
    return [json.dumps(e) for e in events]


def self_test(scratch: Path) -> int:
    scratch.mkdir(parents=True, exist_ok=True)
    planted = "PLANTED-TOKEN-" + secrets.token_hex(8)
    results = {}
    for name, inject in (("green", True), ("red", False)):
        downloads = scratch / name / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        (downloads / "harness.out").write_text("\n".join(_fixture_stream(planted, inject)) + "\n")
        (downloads / "harness.rc").write_text("0\n")
        rows, text = analyze("plugin", downloads, scratch / name / "evidence", "CODEWORD-abc123", {"planted_token": planted})
        results[name] = (rows, text)
        print(f"--- self-test fixture '{name}' (skill text injected: {inject}) ---")
        print(text)
    green_rows, green_text = results["green"]
    red_rows, _ = results["red"]
    checks = {
        "R1 PASS on the injected fixture": green_rows[0].verdict == "PASS",
        "R1 FAIL without the injected skill text": red_rows[0].verdict == "FAIL",
        "R5 saw the skill-gate deny on the pre-load call": "dispatch-skill-gate" in green_rows[4].render(),
        "R2/R3/R6/R7 NOT EXERCISED on a fixture without them": all(
            r.verdict == "NOT EXERCISED" for r in (green_rows[1], green_rows[2], green_rows[5], green_rows[6])),
        "scan counted the planted token exactly once": '"planted_token": 1' in green_text,
        "positive control counted 1": "positive control (planted string in a copy of the first file): 1" in green_text,
    }
    print("--- self-test assertions ---")
    for label, ok in checks.items():
        print(f"{'ok  ' if ok else 'FAIL'} {label}")
    hits = green_text.count('"planted_token": 1')
    print(f"scan hit count for the planted string: {hits}")
    return 0 if all(checks.values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Checks for the statiker-mechanism probe.")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--scratch", help="self-test: where the planted fixture goes (default: a temp dir)")
    parser.add_argument("--mode", choices=("plugin", "settings-hooks", "control", "stopguard"))
    parser.add_argument("--downloads")
    parser.add_argument("--evidence")
    parser.add_argument("--codeword", default="")
    parser.add_argument("--claude-token-file", default="~/.aos/worker/claude-oauth-token")
    parser.add_argument("--codex-auth", default="~/.aos/worker/codex-home/auth.json")
    parser.add_argument("--evidence-scan", action="store_true",
                        help="after the checks, scan the evidence directory too and append the counts")
    args = parser.parse_args(argv)
    if args.self_test:
        if args.scratch:
            return self_test(Path(args.scratch))
        with tempfile.TemporaryDirectory(prefix="stkm-selftest-") as scratch:
            return self_test(Path(scratch))
    if not (args.mode and args.downloads and args.evidence):
        parser.error("--mode, --downloads and --evidence are required without --self-test")
    values = load_secrets(args.claude_token_file, args.codex_auth)
    _, text = analyze(args.mode, Path(args.downloads), Path(args.evidence), args.codeword, values)
    print(text)
    if args.evidence_scan:
        print(append_evidence_scan(Path(args.evidence), values))
    return 0


if __name__ == "__main__":
    sys.exit(main())
