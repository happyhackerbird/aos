#!/bin/sh
# One `codex exec` inside the container: run_case.sh <case> [codex exec flags...]
#   NO_TOML_SERVER  set: no [mcp_servers.stub] in $CODEX_HOME/config.toml (register with -c instead)
#   EXTRA_TOML      appended to $CODEX_HOME/config.toml (printf %b text)
#   REPO_SETUP      shell run in the working directory (the repository) before codex starts
#   FAKE_CALL       a Responses function_call item the fake model sends first
# Codex passes no parent environment to an MCP server, so the stub always logs to /tmp/stub.log.
case=$1; shift
export CODEX_HOME=/tmp/codex-$case HOME=/tmp/home-$case FAKE_LOG=/tmp/fake-$case.log FAKE_KEY=k
mkdir -p "$CODEX_HOME" "$HOME" "/tmp/work-$case" && cd "/tmp/work-$case" && git init -q . 2>/dev/null
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
[ -z "${NO_TOML_SERVER:-}" ] && printf '[mcp_servers.stub]\ncommand = "python3"\nargs = ["/probe/stub_mcp.py", "%s"]\n' "$case" \
  >> "$CODEX_HOME/config.toml"
[ -n "${EXTRA_TOML:-}" ] && printf "%b" "$EXTRA_TOML" >> "$CODEX_HOME/config.toml"
python3 /probe/fake_resp.py & fake=$!
sleep 1
codex exec --json --skip-git-repo-check --sandbox danger-full-access -c approval_policy="never" "$@" "say hi" \
  > "/tmp/out-$case.json" 2> "/tmp/err-$case.txt"
echo "== $case rc=$?"
cat "$FAKE_LOG" 2>/dev/null || echo "(no model request)"
grep -v -E 'PATH aliases|Reading additional input' "/tmp/err-$case.txt" | head -c 400 | tr -c '[:print:]\n' '?'
kill $fake 2>/dev/null
