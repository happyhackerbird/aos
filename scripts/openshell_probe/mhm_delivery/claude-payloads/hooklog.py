#!/usr/bin/env python3
"""Claude Code hook logger (record F130): append each hook's stdin JSON as one line to $MHM_CLAUDE_LOG
(default: hooks.jsonl beside this file). Registered by settings.json for PostToolUse, SubagentStart, SubagentStop
and Stop; prints nothing, so it never changes Claude's behaviour."""
import os
import sys
from pathlib import Path

raw = sys.stdin.read()
log = os.environ.get("MHM_CLAUDE_LOG") or str(Path(__file__).resolve().with_name("hooks.jsonl"))
with open(log, "a", encoding="utf-8") as f:
    f.write(raw.strip() + "\n")
