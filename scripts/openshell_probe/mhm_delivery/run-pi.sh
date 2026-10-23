#!/bin/sh
# One pi 1.1.0 run inside the probe container: run-pi.sh <case> [extra pi flags...]
#   FAKE_SCRIPT, SEED, INJECT_AFTER, INJECT_FROM, INJECT_BODY, NO_EXT, AOS_PI_* as in run-omp.sh
#   NO_MCP=1      keep the runner's --no-mcp (no aos server); default: drop --no-mcp, load -e builtin:mcp and the
#                 agent dir's mcp.json with exposure "direct" and timeout 1000 (seconds)
#   EXPOSURE      the aos server's exposure in mcp.json (default direct)
# Writes /tmp/cases/<case>/... as run-omp.sh does.
case=$1; shift
out=/tmp/cases/$case; mkdir -p "$out"
export PI_CODING_AGENT_DIR=/tmp/pi-agent-$case HOME=/tmp/pi-home-$case
export FAKE_LOG=$out/fake.log FAKE_BUS_LOG=$out/bus.log FAKE_SCRIPT=${FAKE_SCRIPT:-/probe/script-two-tools.json}
export FAKE_BUS_TOKEN=probe-claim-token-9f3e2c0b-never-printed
export AOS_AGENT_RUN_FILE=/tmp/aos-run-$case.json AOS_AGENT_STATE_DIR=/tmp/aos-agent-$case AOS_AGENT_BIN=/probe/aos-agent
export AOS_AGENT_HOOK_INTERVAL=${AOS_AGENT_HOOK_INTERVAL:-0}
mkdir -p "$PI_CODING_AGENT_DIR" "$HOME" "/tmp/work-$case" && cd "/tmp/work-$case" && git init -q . 2>/dev/null
printf '{"run_id":"AO-7-r1","task_id":"AO-7","claim_token":"%s","bus_url":"http://127.0.0.1:18090"}\n' "$FAKE_BUS_TOKEN" > "$AOS_AGENT_RUN_FILE"
chmod 600 "$AOS_AGENT_RUN_FILE"
[ -n "${REPO_SETUP:-}" ] && sh -c "$REPO_SETUP"
MCP="--no-mcp"
if [ -z "${NO_MCP:-}" ]; then
  if [ "${MCP_TIMEOUT:-1000}" = none ]; then
    printf '{"mcpServers":{"aos":{"command":"/probe/aos-agent","args":["mcp"],"exposure":"%s"}}}\n' "${EXPOSURE:-direct}" > "$PI_CODING_AGENT_DIR/mcp.json"
  else
    printf '{"mcpServers":{"aos":{"command":"/probe/aos-agent","args":["mcp"],"exposure":"%s","timeout":%s}}}\n' "${EXPOSURE:-direct}" "${MCP_TIMEOUT:-1000}" > "$PI_CODING_AGENT_DIR/mcp.json"
  fi
  MCP="-e builtin:mcp"
fi
cat > "$PI_CODING_AGENT_DIR/models.json" <<'JSON'
{"providers": {"fake": {"baseUrl": "http://127.0.0.1:18080/v1", "api": "openai-completions", "apiKey": "fake-key",
                        "models": [{"id": "fake-model", "name": "Fake"}]}}}
JSON
bus=""; [ -z "${NO_BUS:-}" ] && { python3 /probe/fake_bus.py & bus=$!; }
python3 /probe/fake_chat.py & fake=$!
sleep 1
[ -n "${SEED:-}" ] && [ -z "${NO_BUS:-}" ] && python3 /probe/seed.py "$SEED"
if [ -n "${INJECT_AFTER:-}" ] && [ -z "${NO_BUS:-}" ]; then
  ( sleep "$INJECT_AFTER"; python3 /probe/seed.py "[{\"from\":\"${INJECT_FROM:-operator}\",\"body\":\"${INJECT_BODY:-INJECTED-MID-RUN}\"}]"; echo "injected at $(date +%s)" >> "$out/inject.log" ) &
fi
EXT=""; [ -z "${NO_EXT:-}" ] && EXT="-e /probe/aos_pi.ts"
SID=$(python3 -c "import uuid; print(uuid.uuid4())")
echo "start $(date +%s) session $SID" > "$out/times.txt"
printf '%s\n' "${PROMPT:-Run the commands you are given and report.}" | pi --print --mode json --provider fake --model fake-model \
  --session-id "$SID" --session-dir "/tmp/sess-$case" --no-extensions --no-skills --no-prompt-templates $MCP --no-approve \
  --no-context-files --no-themes --offline $EXT "$@" > "$out/harness.out" 2> "$out/harness.err"
echo $? > "$out/rc"
echo "end $(date +%s)" >> "$out/times.txt"
cp "$HOME/.local/share/aos-pi-probe/fires.jsonl" "$out/fires.jsonl" 2>/dev/null
cp "$AOS_AGENT_STATE_DIR/AO-7-r1.jsonl" "$out/ledger.jsonl" 2>/dev/null
[ -n "$bus" ] && curl -s 127.0.0.1:18090/_probe/state > "$out/state.json"
kill $bus $fake 2>/dev/null; wait $bus $fake 2>/dev/null
ls /tmp/evil-* >> "$out/repo-markers.txt" 2>/dev/null
python3 /probe/summarize.py "$out" chat ${MARKERS:-} | tee "$out/summary.txt"
