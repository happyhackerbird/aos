#!/bin/sh
# One omp run inside the container: run_case.sh <case> [omp flags...]
#   MCP_JSON    the agent directory's mcp.json (default: the stub server, named "stub")
#   OMP_CONFIG  the agent directory's config.yml (printf %b text; default none)
#   STUB_SLEEP  seconds the stub's tools/call sleeps; FAKE_CALL a tool the fake model calls first
# Prints the stub's log (servers started, requests) and the fake model's log (tool names per request).
case=$1; shift
export PI_CODING_AGENT_DIR=/tmp/pi-agent-$case HOME=/tmp/pi-home STUB_LOG=/tmp/stub-$case.log FAKE_LOG=/tmp/fake-$case.log
mkdir -p "$PI_CODING_AGENT_DIR" "$HOME" /tmp/work && cd /tmp/work && git init -q . 2>/dev/null
if [ -n "${MCP_JSON:-}" ]; then printf '%s\n' "$MCP_JSON" > "$PI_CODING_AGENT_DIR/mcp.json"; else
  printf '{"mcpServers": {"stub": {"command": "python3", "args": ["/probe/stub_mcp.py", "%s"]}}}\n' "$case" \
    > "$PI_CODING_AGENT_DIR/mcp.json"; fi
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
[ -n "${OMP_CONFIG:-}" ] && printf "%b" "$OMP_CONFIG" > "$PI_CODING_AGENT_DIR/config.yml"
python3 /probe/fake_llm.py & fake=$!
sleep 1
echo "say hi" | omp --print --mode json --provider fake --model fake-model --session-dir "/tmp/sess-$case" "$@" \
  > "/tmp/out-$case.json" 2> "/tmp/err-$case.txt"
echo "== $case rc=$?"
cat "$STUB_LOG" 2>/dev/null || echo "(no stub log)"
cat "$FAKE_LOG" 2>/dev/null || echo "(no model request)"
head -c 600 "/tmp/err-$case.txt" | tr -c '[:print:]\n' '?'; echo
kill $fake 2>/dev/null
