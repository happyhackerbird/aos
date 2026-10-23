#!/bin/sh
# One `codex exec` inside the probe container: run-codex.sh <case> [extra codex exec flags...]
#   FAKE_SCRIPT   the fake Responses model's step file (default /probe/script-codex-two-tools.json)
#   SEED, INJECT_AFTER, INJECT_FROM, INJECT_BODY as in run-omp.sh
#   HOOKS         file (copy /probe/codex-hooks.json to $CODEX_HOME/hooks.json, the default) | cli (the same two
#                 hooks as -c hooks.* inline tables, no hooks.json) | none (no hooks at all; features.hooks=false)
#   NO_BYPASS=1   leave out --dangerously-bypass-hook-trust;  NO_MCP=1 no aos server
#   REPO_SETUP    shell run in the working directory before codex starts (a repository .codex/hooks.json etc.)
# The hook processes read the run file at its default path /sandbox/aos-run.json and keep state in /tmp/aos-agent
# (the environment may not reach them); AOS_AGENT_HOOK_INTERVAL is exported in case it does.
# Writes /tmp/cases/<case>/{harness.out,harness.err,rc,fake.log,bus.log,ledger.jsonl,state.json,summary.txt}
case=$1; shift
out=/tmp/cases/$case; mkdir -p "$out"
export CODEX_HOME=/tmp/codex-$case HOME=/tmp/home-$case FAKE_KEY=k
export FAKE_LOG=$out/fake.log FAKE_BUS_LOG=$out/bus.log FAKE_SCRIPT=${FAKE_SCRIPT:-/probe/script-codex-two-tools.json}
export FAKE_BUS_TOKEN=probe-claim-token-9f3e2c0b-never-printed
export AOS_AGENT_HOOK_INTERVAL=${AOS_AGENT_HOOK_INTERVAL:-0}
rm -rf /tmp/aos-agent; mkdir -p "$CODEX_HOME" "$HOME" "/tmp/work-$case" && cd "/tmp/work-$case" && git init -q . 2>/dev/null
printf '{"run_id":"AO-7-r1","task_id":"AO-7","claim_token":"%s","bus_url":"http://127.0.0.1:18090"}\n' "$FAKE_BUS_TOKEN" > /sandbox/aos-run.json
chmod 600 /sandbox/aos-run.json
[ -n "${REPO_SETUP:-}" ] && sh -c "$REPO_SETUP"
cat > "$CODEX_HOME/config.toml" <<'TOML'
model = "fake-model"
model_provider = "fake"
[model_providers.fake]
name = "fake"
base_url = "http://127.0.0.1:18080/v1"
wire_api = "responses"
env_key = "FAKE_KEY"
TOML
HOOKFLAGS="-c features.hooks=true"; [ -z "${NO_BYPASS:-}" ] && HOOKFLAGS="$HOOKFLAGS --dangerously-bypass-hook-trust"
case "${HOOKS:-file}" in
  file) cp /probe/codex-hooks.json "$CODEX_HOME/hooks.json" ;;
  cli) HOOKFLAGS="$HOOKFLAGS -c 'hooks.PostToolUse=[{hooks=[{type=\"command\",command=\"/probe/aos-agent inbox --hook\",timeout=10}]}]' -c 'hooks.Stop=[{hooks=[{type=\"command\",command=\"/probe/aos-agent stop --hook\",timeout=10}]}]'" ;;
  toml) printf '[[hooks.PostToolUse]]\n[[hooks.PostToolUse.hooks]]\ntype = "command"\ncommand = "/probe/aos-agent inbox --hook"\ntimeout = 10\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "/probe/aos-agent stop --hook"\ntimeout = 10\n' >> "$CODEX_HOME/config.toml" ;;
  none) HOOKFLAGS="-c features.hooks=false" ;;
esac
MCPFLAGS=""; [ -z "${NO_MCP:-}" ] && MCPFLAGS="-c 'mcp_servers.aos.command=\"/probe/aos-agent\"' -c 'mcp_servers.aos.args=[\"mcp\"]' -c mcp_servers.aos.tool_timeout_sec=1000"
bus=""; [ -z "${NO_BUS:-}" ] && { python3 /probe/fake_bus.py & bus=$!; }
python3 /probe/fake_resp.py & fake=$!
sleep 1
[ -n "${SEED:-}" ] && [ -z "${NO_BUS:-}" ] && python3 /probe/seed.py "$SEED"
if [ -n "${INJECT_AFTER:-}" ] && [ -z "${NO_BUS:-}" ]; then
  ( sleep "$INJECT_AFTER"; python3 /probe/seed.py "[{\"from\":\"${INJECT_FROM:-operator}\",\"body\":\"${INJECT_BODY:-INJECTED-MID-RUN}\"}]"; echo "injected at $(date +%s)" >> "$out/inject.log" ) &
fi
printf '%s\n' "${PROMPT:-Run the commands you are given and report.}" > "/tmp/prompt-$case.txt"
CMD="codex exec --json --skip-git-repo-check --sandbox danger-full-access -c approval_policy=\"never\" $HOOKFLAGS -c features.multi_agent=true -c agents.enabled=true $MCPFLAGS -c features.plugins=false -c features.apps=false $* \"\$(cat /tmp/prompt-$case.txt)\""
echo "start $(date +%s)" > "$out/times.txt"
echo "$CMD" > "$out/command.txt"
eval "$CMD" > "$out/harness.out" 2> "$out/harness.err"
echo $? > "$out/rc"
echo "end $(date +%s)" >> "$out/times.txt"
cp /tmp/aos-agent/AO-7-r1.jsonl "$out/ledger.jsonl" 2>/dev/null
[ -n "$bus" ] && curl -s 127.0.0.1:18090/_probe/state > "$out/state.json"
kill $bus $fake 2>/dev/null; wait $bus $fake 2>/dev/null
ls /tmp/evil-* >> "$out/repo-markers.txt" 2>/dev/null; cat "$HOME/.local/share/codex-probe/fires.jsonl" >> "$out/fires.jsonl" 2>/dev/null
python3 /probe/summarize.py "$out" resp ${MARKERS:-} | tee "$out/summary.txt"
