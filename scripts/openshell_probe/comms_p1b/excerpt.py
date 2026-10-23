#!/usr/bin/env python3
"""Compact evidence from a P1b run: the ordered event sequence of Claude's stream-json output
(tool calls, hook events, tool results, assistant text, the result line), the init event's MCP
facts, and the ledger. Writes text files beside the stream; prints nothing secret itself, and the
driver's checks.json records that the downloads hold no claim token before anything here is copied
into the record.

    uv run python scripts/openshell_probe/comms_p1b/excerpt.py <downloads dir> <out dir>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HEAD = 400


def short(value: object, limit: int = HEAD) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + f"... [{len(text)} chars]"


def excerpt(stream_path: Path, out: Path) -> dict:
    lines = stream_path.read_text(errors="replace").splitlines()
    events, init, result = [], None, None
    for raw in lines:
        try:
            event = json.loads(raw)
        except ValueError:
            events.append(f"non-json line: {short(raw, 120)}")
            continue
        kind, sub = event.get("type"), event.get("subtype")
        if kind == "system" and sub == "init":
            init = event
            mcp_tools = sorted(t for t in event.get("tools", []) if str(t).startswith("mcp__"))
            events.append(f"system/init model={event.get('model')} permissionMode={event.get('permissionMode')} "
                          f"mcp_servers={json.dumps(event.get('mcp_servers'))} mcp_tools={mcp_tools} "
                          f"tools={len(event.get('tools', []))} hooks={'yes' if event.get('hooks') else 'absent-or-empty'}")
        elif kind == "system" and sub in ("hook_started", "hook_response"):
            fields = {k: event.get(k) for k in ("hook_name", "hook_event", "exit_code", "outcome")}
            line = f"system/{sub} " + " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
            for key in ("stdout", "stderr"):
                if event.get(key):
                    line += f" {key}={short(event[key])}"
            events.append(line)
        elif kind == "system":
            events.append(f"system/{sub} {short({k: v for k, v in event.items() if k not in ('type', 'subtype')}, 200)}")
        elif kind in ("assistant", "user"):
            for block in event.get("message", {}).get("content", []) or []:
                if not isinstance(block, dict):
                    events.append(f"{kind}/text {short(block)}")
                    continue
                btype = block.get("type")
                if btype == "text":
                    events.append(f"{kind}/text {short(block.get('text'))}")
                elif btype == "tool_use":
                    events.append(f"{kind}/tool_use {block.get('name')} {short(block.get('input'), 200)}")
                elif btype == "tool_result":
                    content = block.get("content")
                    if isinstance(content, list):
                        content = " ".join(short(c.get("text", c), 300) if isinstance(c, dict) else short(c, 300) for c in content)
                    events.append(f"{kind}/tool_result is_error={block.get('is_error', False)} {short(content)}")
                else:
                    events.append(f"{kind}/{btype} {short(block, 200)}")
        elif kind == "result":
            result = event
            events.append(f"result subtype={event.get('subtype')} is_error={event.get('is_error')} "
                          f"num_turns={event.get('num_turns')} duration_ms={event.get('duration_ms')} "
                          f"cost_usd={event.get('total_cost_usd')} result={short(event.get('result'))}")
        else:
            events.append(f"{kind}/{sub} {short(event, 200)}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "stream-events.txt").write_text("\n".join(f"{i:03d} {line}" for i, line in enumerate(events)) + "\n")
    summary = {"lines": len(lines), "events": len(events),
               "init_mcp_servers": (init or {}).get("mcp_servers"),
               "init_mcp_tools": sorted(t for t in (init or {}).get("tools", []) if str(t).startswith("mcp__")),
               "result": {k: (result or {}).get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd", "result")}}
    (out / "stream-summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main(argv: list[str]) -> int:
    downloads, out = Path(argv[1]), Path(argv[2])
    summary = excerpt(downloads / "harness.out", out)
    ledger = next(iter(sorted(downloads.glob("*.jsonl"))), None)
    if ledger is not None:
        (out / "ledger.jsonl").write_text(ledger.read_text())
    err = downloads / "harness.err"
    if err.exists():
        (out / "harness-err-tail.txt").write_text(err.read_text(errors="replace")[-3000:])
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
