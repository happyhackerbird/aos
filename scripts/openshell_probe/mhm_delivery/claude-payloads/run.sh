#!/bin/sh
# Claude Code subagent hook payload case (record F130, F158): which hook payloads name the subagent.
# Host Claude Code (the image's claude is unmeasured for this case: record D52), model haiku, about 16 s.
# Usage, from the kit directory:  OUT=<dir> sh claude-payloads/run.sh     (OUT default: claude-payloads/out)
# Writes OUT/{hooks.jsonl,result.json,stderr.txt,version.txt,summary.txt}. Expected summary (2.1.302, 2026-10-17):
#   SubagentStart, the subagent's PostToolUse Bash and both SubagentStop lines carry agent_id and agent_type;
#   the parent's PostToolUse Agent, PostToolUse Bash and Stop lines carry neither key.
set -eu
KIT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${OUT:-$KIT/claude-payloads/out}
mkdir -p "$OUT"
: > "$OUT/hooks.jsonl"
cd "$KIT"
claude --version > "$OUT/version.txt" 2>&1 || true
set +e
MHM_CLAUDE_LOG="$OUT/hooks.jsonl" env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT claude -p "Use the Agent tool with subagent_type general-purpose to run exactly the shell command: echo subprobe-child   (with its Bash tool) and have it report the output. Then run exactly: echo subprobe-parent   with your own Bash tool. Reply with the two outputs on two lines and nothing else." --model haiku --settings "$KIT/claude-payloads/settings.json" --allowedTools "Bash,Agent" --output-format json < /dev/null > "$OUT/result.json" 2> "$OUT/stderr.txt"
rc=$?
set -e
python3 - "$OUT/hooks.jsonl" "$rc" > "$OUT/summary.txt" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
print(f"claude rc={sys.argv[2]} hook lines={len(rows)}")
for d in rows:
    print(f"{d.get('hook_event_name')} tool={d.get('tool_name')} agent_id={'present' if 'agent_id' in d else 'absent'} agent_type={d.get('agent_type')}")
PY
cat "$OUT/summary.txt"
