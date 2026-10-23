#!/usr/bin/env python3
"""A repository- or plugin-supplied Codex hook for the isolation and coexistence cases: appends one line per fire to
$HOME/.local/share/codex-probe/fires.jsonl and writes /tmp/evil-hook-fired; never blocks."""
import json, os, sys, time
raw = sys.stdin.read()
try:
    payload = json.loads(raw) if raw.strip() else {}
except ValueError:
    payload = {}
d = os.path.join(os.path.expanduser("~"), ".local", "share", "codex-probe")
os.makedirs(d, exist_ok=True)
with open(os.path.join(d, "fires.jsonl"), "a") as f:
    f.write(json.dumps({"t": round(time.time(), 3), "evil_hook": payload.get("hook_event_name"), "argv": sys.argv[1:]}) + "\n")
open("/tmp/evil-hook-fired", "a").write(f"{time.time()} {payload.get('hook_event_name')} {sys.argv[1:]}\n")

if sys.argv[1:] == ["plugin-post"]:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "PLUGIN-CONTEXT-MARKER from the plugin hook"}}))
