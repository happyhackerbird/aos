#!/bin/sh
# Every case of docs/probes/2026-10-16-omp-mcp.md, in order, inside one container.
F='--no-extensions --no-skills --no-rules --no-title --max-time 1200 --approval-mode=yolo'
T='read,bash,edit,eval,glob,grep,wait,todo,write,web_search'
R=/probe/run_case.sh
echo "### 1. the agent dir's server starts under the runner's flags; its tool is not offered by default"
sh $R plain $F
sh $R allow $F --tools=$T
echo "### 2-3. registered names (an unknown --tools name lists them); named, the tool is offered"
sh $R list $F --tools=no_such_tool | grep -o 'Other registered tools.*'
sh $R named $F --tools=$T,mcp__stub_ping
MCP_JSON='{"mcpServers": {"aos": {"command": "/usr/local/bin/aos-agent", "args": ["mcp"]}}}' sh $R aoslist $F --tools=no_such_tool \
  | grep -o 'Other registered tools.*'
MCP_JSON='{"mcpServers": {"aos": {"command": "/usr/local/bin/aos-agent", "args": ["mcp"]}}}' \
  sh $R aosnamed $F --tools=$T,mcp__aos_ask,mcp__aos_reply,mcp__aos_inbox,mcp__aos_wait,mcp__aos_declare,mcp__aos_propose
echo "### 5. the repository's MCP files: default, then enableProjectConfig false"
mkdir -p /tmp/work/.omp /tmp/work/.claude /tmp/work/.cursor
for f in .mcp.json mcp.json .omp/mcp.json .claude/mcp.json .cursor/mcp.json; do
  printf '{"mcpServers": {"r%s": {"command": "python3", "args": ["/probe/stub_mcp.py", "REPO %s"]}}}\n' \
    "$(echo $f | tr -c 'a-z\n' '_')" "$f" > /tmp/work/$f
done
USER_STUB='{"mcpServers": {"aos": {"command": "python3", "args": ["/probe/stub_mcp.py", "USER agentdir"]}}}'
MCP_JSON="$USER_STUB" sh $R repo-default $F --tools=$T | grep '^start'
MCP_JSON="$USER_STUB" OMP_CONFIG='mcp:\n  enableProjectConfig: false\n' sh $R repo-off $F --tools=$T | grep '^start'
echo "### 5b. a repository server named like the agent dir's: which one starts"
printf '{"mcpServers": {"aos": {"command": "python3", "args": ["/probe/stub_mcp.py", "REPO .omp/mcp.json"]}}}\n' > /tmp/work/.omp/mcp.json
MCP_JSON="$USER_STUB" sh $R clash $F --tools=$T | grep '^start'
rm -rf /tmp/work/.omp /tmp/work/.claude /tmp/work/.cursor /tmp/work/.mcp.json /tmp/work/mcp.json
echo "### 6. the MCP call timeout: 5 s, 130 s, 130 s with the server's timeout 1000000"
STUB_SLEEP=5 FAKE_CALL=mcp__stub_ping sh $R sleep5 $F --tools=read,bash,mcp__stub_ping
STUB_SLEEP=130 FAKE_CALL=mcp__stub_ping sh $R sleep130 $F --tools=read,bash,mcp__stub_ping
MCP_JSON='{"mcpServers": {"stub": {"command": "python3", "args": ["/probe/stub_mcp.py", "t"], "timeout": 1000000}}}' \
  STUB_SLEEP=130 FAKE_CALL=mcp__stub_ping sh $R timeout130 $F --tools=read,bash,mcp__stub_ping
