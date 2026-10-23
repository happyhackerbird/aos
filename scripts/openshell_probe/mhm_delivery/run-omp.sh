#!/bin/sh
# One omp run inside the probe container: run-omp.sh <case> [extra omp flags...]
#   FAKE_SCRIPT   the fake model's step file (default /probe/script-two-tools.json)
#   SEED          JSON list of rows pending before start: [{"from":"operator"|"AO-2","body":"..."}]
#   INJECT_AFTER  seconds after start to post one more row (INJECT_FROM, INJECT_BODY); unset: none
#   NO_EXT=1      no -e /probe/aos_pi.ts;  NO_MCP=1 no aos server (and no mcp__aos_* in --tools)
#   AOS_PI_DELIVER / AOS_PI_STOP / AOS_PI_STOP_EVENT  the extension's variants (see aos_pi.ts)
#   MAX_TIME      omp --max-time (default 300)
# Writes /tmp/cases/<case>/{harness.out,harness.err,rc,fake.log,bus.log,fires.jsonl,ledger.jsonl,state.json,summary.txt}
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
# AGENTS=1: ship the probe task agent (model fake/fake-model) where omp's task tool finds agents ($HOME/.omp/agent/agents)
[ -n "${AGENTS:-}" ] && mkdir -p "$HOME/.omp/agent/agents" && cp /probe/agents-omp/*.md "$HOME/.omp/agent/agents/"
TOOLS=read,bash,edit,eval,glob,grep,wait,todo,write,web_search,task
if [ -z "${NO_MCP:-}" ]; then
  if [ "${MCP_TIMEOUT:-1000000}" = none ]; then
    printf '{"mcpServers":{"aos":{"command":"/probe/aos-agent","args":["mcp"]}}}\n' > "$PI_CODING_AGENT_DIR/mcp.json"
  else
    printf '{"mcpServers":{"aos":{"command":"/probe/aos-agent","args":["mcp"],"timeout":%s}}}\n' "${MCP_TIMEOUT:-1000000}" > "$PI_CODING_AGENT_DIR/mcp.json"
  fi
  TOOLS=$TOOLS,mcp__aos_ask,mcp__aos_reply,mcp__aos_inbox,mcp__aos_wait,mcp__aos_declare,mcp__aos_propose
fi
[ -z "${NO_OMP_CONFIG:-}" ] && printf 'mcp:\n  enableProjectConfig: false\n  startupTimeoutMs: 0\n' > "$PI_CODING_AGENT_DIR/config.yml"
cat > "$PI_CODING_AGENT_DIR/models.yml" <<'YML'
providers:
  fake:
    baseUrl: http://127.0.0.1:18080/v1
    api: openai-completions
    apiKey: fake-key
    models:
      - id: fake-model
        name: Fake
YML
bus=""; [ -z "${NO_BUS:-}" ] && { python3 /probe/fake_bus.py & bus=$!; }
python3 /probe/fake_chat.py & fake=$!
sleep 1
[ -n "${SEED:-}" ] && [ -z "${NO_BUS:-}" ] && python3 /probe/seed.py "$SEED"
if [ -n "${INJECT_AFTER:-}" ] && [ -z "${NO_BUS:-}" ]; then
  ( sleep "$INJECT_AFTER"; python3 /probe/seed.py "[{\"from\":\"${INJECT_FROM:-operator}\",\"body\":\"${INJECT_BODY:-INJECTED-MID-RUN}\"}]"; echo "injected at $(date +%s)" >> "$out/inject.log" ) &
fi
EXT=""; [ -z "${NO_EXT:-}" ] && EXT="${EXT_FLAG:--e} /probe/aos_pi.ts"
echo "start $(date +%s)" > "$out/times.txt"
printf '%s\n' "${PROMPT:-Run the commands you are given and report.}" | omp --print --mode json --provider fake --model fake-model \
  --session-dir "/tmp/sess-$case" --no-extensions --no-skills --no-rules --no-title --max-time "${MAX_TIME:-300}" \
  --approval-mode=yolo "--tools=$TOOLS" $EXT "$@" > "$out/harness.out" 2> "$out/harness.err"
echo $? > "$out/rc"
echo "end $(date +%s)" >> "$out/times.txt"
cp "$HOME/.local/share/aos-pi-probe/fires.jsonl" "$out/fires.jsonl" 2>/dev/null
cp "$AOS_AGENT_STATE_DIR/AO-7-r1.jsonl" "$out/ledger.jsonl" 2>/dev/null
[ -n "$bus" ] && curl -s 127.0.0.1:18090/_probe/state > "$out/state.json"
kill $bus $fake 2>/dev/null; wait $bus $fake 2>/dev/null
ls -la /tmp/work-$case 2>/dev/null | grep -i marker >> "$out/repo-markers.txt" 2>/dev/null; ls /tmp/evil-* >> "$out/repo-markers.txt" 2>/dev/null
python3 /probe/summarize.py "$out" chat ${MARKERS:-} | tee "$out/summary.txt"
