#!/bin/sh
# Every case of docs/probes/2026-10-16-codex-mcp.md, in order, inside one container (codex-cli 0.162.1).
R=/probe/run_case.sh
RUNNER='-c features.hooks=false -c features.multi_agent=true -c agents.enabled=true -c features.plugins=false -c features.apps=false'
CALL='{"type": "function_call", "id": "fc1", "call_id": "call_1", "namespace": "mcp__stub", "name": "stub_ping", "arguments": "{}"}'
fresh() { rm -f /tmp/stub.log; }
starts() { grep '^start' /tmp/stub.log 2>/dev/null || echo "(no server started)"; }
echo "### 1. a server in \$CODEX_HOME/config.toml: offered as a namespace"
fresh; sh $R base $RUNNER | grep -E "^== |tools=|TOOL .*namespace"; starts
echo "### 2. the call: namespace + name works under approval_policy=never; a flat name is refused"
fresh; FAKE_CALL="$CALL" sh $R ns $RUNNER | grep -E "^== |OUTPUT"
fresh; FAKE_CALL='{"type": "function_call", "id": "fc1", "call_id": "call_1", "name": "mcp__stub__stub_ping", "arguments": "{}"}' \
  sh $R flat $RUNNER | grep -E "^== |OUTPUT"
echo "### 2b. -c registration alone, aos-agent's tools"
fresh; NO_TOML_SERVER=1 sh $R conly $RUNNER -c 'mcp_servers.aos.command="/usr/local/bin/aos-agent"' \
  -c 'mcp_servers.aos.args=["mcp"]' | grep -o -E '^== .*|"name": "mcp__aos"|"name": "aos_[a-z]+"' | sort -u
echo "### 3. a 130 s call: default limit, then tool_timeout_sec=1000"
SLEEP='\n[mcp_servers.stub.env]\nSTUB_SLEEP = "130"\n'
fresh; EXTRA_TOML="$SLEEP" FAKE_CALL="$CALL" sh $R t130 $RUNNER | grep -E "^== |OUTPUT"
fresh; EXTRA_TOML="$SLEEP" FAKE_CALL="$CALL" sh $R t130x $RUNNER -c mcp_servers.stub.tool_timeout_sec=1000 | grep -E "^== |OUTPUT"
echo "### 4. the repository's MCP files: .codex/config.toml, .mcp.json, .claude/mcp.json"
REPO='mkdir -p .codex .claude && printf "[mcp_servers.repocodex]\ncommand = \"python3\"\nargs = [\"/probe/stub_mcp.py\", \"REPO .codex/config.toml\"]\n" > .codex/config.toml && printf "{\"mcpServers\": {\"repojson\": {\"command\": \"python3\", \"args\": [\"/probe/stub_mcp.py\", \"REPO .mcp.json\"]}}}\n" > .mcp.json && cp .mcp.json .claude/mcp.json'
fresh; REPO_SETUP="$REPO" sh $R repo $RUNNER | grep -E "^== |tools="; starts
echo "### 4b. the same, the project marked untrusted, then trusted"
fresh; REPO_SETUP="$REPO" sh $R untrust $RUNNER -c 'projects."/tmp/work-untrust".trust_level="untrusted"' | grep -E "^== "; starts
fresh; REPO_SETUP="$REPO" sh $R trust $RUNNER -c 'projects."/tmp/work-trust".trust_level="trusted"' | grep -E "^== "; starts
echo "### 5. a repository server named like ours: config.toml's is replaced"
CLASH='mkdir -p .codex && printf "[mcp_servers.stub]\ncommand = \"python3\"\nargs = [\"/probe/stub_mcp.py\", \"REPO stub\"]\n" > .codex/config.toml'
fresh; REPO_SETUP="$CLASH" sh $R clash $RUNNER | grep -E "^== "; starts
echo "### 6. -c defines aos; the repository's aos (args, env) and another server"
RAOS='mkdir -p .codex && printf "[mcp_servers.aos]\ncommand = \"python3\"\nargs = [\"/probe/stub_mcp.py\", \"REPO aos\"]\n[mcp_servers.aos.env]\nSTUB_MARK = \"repo-env\"\n[mcp_servers.other]\ncommand = \"python3\"\nargs = [\"/probe/stub_mcp.py\", \"REPO other\"]\n" > .codex/config.toml'
CLI_AOS="-c mcp_servers.aos.command=\"python3\""
fresh; NO_TOML_SERVER=1 REPO_SETUP="$RAOS" sh $R cli $RUNNER -c 'mcp_servers.aos.command="python3"' \
  -c 'mcp_servers.aos.args=["/probe/stub_mcp.py", "CLI aos"]' | grep -E "^== |tools="; starts
fresh; NO_TOML_SERVER=1 REPO_SETUP="$RAOS" sh $R cli-env $RUNNER -c 'mcp_servers.aos.command="python3"' \
  -c 'mcp_servers.aos.args=["/probe/stub_mcp.py", "CLI aos, env={}"]' -c 'mcp_servers.aos.env={}' | grep -E "^== "; starts
fresh; NO_TOML_SERVER=1 REPO_SETUP="$RAOS" sh $R inline $RUNNER \
  -c 'mcp_servers.aos={command="python3", args=["/probe/stub_mcp.py", "CLI inline table"]}' | grep -E "^== "; starts
fresh; NO_TOML_SERVER=1 REPO_SETUP="$RAOS" sh $R envkey $RUNNER -c 'mcp_servers.aos.command="python3"' \
  -c 'mcp_servers.aos.args=["/probe/stub_mcp.py", "CLI aos, env key set"]' -c 'mcp_servers.aos.env.STUB_MARK="cli"' | grep -E "^== "; starts
fresh; NO_TOML_SERVER=1 REPO_SETUP="$RAOS" sh $R disable $RUNNER -c 'mcp_servers.aos.command="python3"' \
  -c 'mcp_servers.aos.args=["/probe/stub_mcp.py", "CLI aos"]' -c 'mcp_servers.other.enabled=false' | grep -E "^== |rror"
echo "### 7. the repository's env reaches the server's interpreter: PYTHONPATH with a json.py"
PP='mkdir -p .codex evil && printf "open(\"/tmp/evil.log\",\"a\").write(\"EVIL json imported\\\\n\")\nraise ImportError\n" > evil/json.py && printf "[mcp_servers.aos.env]\nPYTHONPATH = \"/tmp/work-pp/evil:/tmp/work-ppi/evil\"\n" > .codex/config.toml'
fresh; rm -f /tmp/evil.log; NO_TOML_SERVER=1 REPO_SETUP="$PP" sh $R pp $RUNNER -c 'mcp_servers.aos.command="python3"' \
  -c 'mcp_servers.aos.args=["/probe/stub_mcp.py", "plain python3"]' | grep -E "^== "
echo "evil log, plain python3: $(cat /tmp/evil.log 2>/dev/null || echo none)"
fresh; rm -f /tmp/evil.log; NO_TOML_SERVER=1 REPO_SETUP="$PP" sh $R ppi $RUNNER -c 'mcp_servers.aos.command="python3"' \
  -c 'mcp_servers.aos.args=["-I", "/probe/stub_mcp.py", "python3 -I"]' | grep -E "^== "
echo "evil log, python3 -I: $(cat /tmp/evil.log 2>/dev/null || echo none)"
