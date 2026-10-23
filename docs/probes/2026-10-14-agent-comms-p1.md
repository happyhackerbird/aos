# Agent communication probe P1: hooks and MCP under `claude -p` in the harness image, 2026-10-14

[Selected probes](public-index.md). Historical design: `2026-10-14-agent-communication` (internal, not distributed).

Answers the five P1 questions of the agent-communication plan on this Mac, with the
Linux harness image (`aos-harness:mvp`, image id `f127d68c236a`, Claude Code 2.1.296 per
`/etc/aos-harness/versions`) run by Colima's Docker (29.9.0, aarch64). Each answer below
is an observed transcript line, not an inference.

**What this is not.** `docker run` is not an OpenShell sandbox: no Landlock policy, no
gateway, no provider; image `ENV` reaches a docker-run process while it does not reach a
sandbox process. The run was shaped like the runner's as far as that goes: `bash -lc` (so
`/etc/profile.d/aos-harness.sh` was sourced), uid 1000 with `HOME=/home/sandbox` (the
image's `USER` and `ENV`), `CLAUDE_CONFIG_DIR=/tmp/claude-home` as
`openshell_runner.main_script` exports it, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`
and `DISABLE_AUTOUPDATER=1`. U3 re-checks the same questions inside a real sandbox.

## Kit

All files were sent into the container with `tar` over stdin and unpacked under `/tmp/p1`;
the credential went into docker's environment only
(`export CLAUDE_CODE_OAUTH_TOKEN="$(cat ~/.aos/worker/claude-oauth-token)"` in a subshell,
then `docker run --rm -i -e CLAUDE_CODE_OAUTH_TOKEN aos-harness:mvp bash -lc 'tar x -C /tmp;
bash /tmp/p1/run.sh <mode>'`), never on a command line and never printed. A first attempt
with the assignment prefixed to the `tar` command reached the container with no token
(`token_set=no`, result `Not logged in · Please run /login`): the prefix binds to the first
command of a pipeline only.

`settings.json` (the `--settings` file; the `deny` is the negative control):

```json
{"permissions": {"defaultMode": "dontAsk", "allow": ["Bash(*)", "Read(*)", "mcp__aos__*"], "deny": ["Write"]},
 "sandbox": {"enabled": false}, "disableAllHooks": false,
 "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/tmp/p1/hook-post.sh", "timeout": 30}]}],
           "Stop": [{"hooks": [{"type": "command", "command": "/tmp/p1/hook-stop.sh", "timeout": 30}]}]}}
```

`hook-post.sh` appends its stdin to `/tmp/p1-out/post.log` and prints
`{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "AOS-P1 hook
context: the code word is PINEAPPLE-7f3e. Include it verbatim in your final line."}}`.
`hook-stop.sh` appends its stdin to `/tmp/p1-out/stop.log`, exits 0 silently when the input
contains `"stop_hook_active":true`, else prints `{"decision": "block", "reason": "AOS-P1 stop
hook: before you finish, write one more line starting with FINAL2: that contains the code
word BANANA-21c4."}`. `stub_mcp.py` is a 60-line standard-library stdio server
(newline-delimited JSON-RPC: `initialize`, `notifications/initialized`, `tools/list`,
`tools/call`, `ping`) with tools `aos_ping` (returns `pong-3f9a from the aos stub`),
`aos_echo` and `aos_sleep(seconds)`, logging every method to `/tmp/p1-out/mcp-aos.log`.
`mcp.json`: `{"mcpServers": {"aos": {"command": "python3", "args": ["/tmp/p1/stub_mcp.py"],
"env": {"STUB_NAME": "aos"}}}}`.

Three runs, each `claude -p "<prompt>" --output-format stream-json --verbose
--setting-sources "" --settings /tmp/p1/settings.json --mcp-config /tmp/p1/mcp.json --model
sonnet --max-budget-usd 1 --no-session-persistence`, plus:

| Run | Extra flags and setup | Prompt (abridged) | Turns, time, cost |
|---|---|---|---|
| main | `--strict-mcp-config --include-hook-events --max-turns 12` | say WRITE-PRESENT or WRITE-ABSENT; Bash `echo hello-aos`; call `mcp__aos__aos_ping`; a FINAL line with every code word a hook asked for | 5, 6.9 s, $0.074 |
| control | no `--strict-mcp-config`; `/tmp/claude-home/.claude.json` with an `mcpServers.other_user` entry and `/tmp/p1/proj/.mcp.json` with `other_project`, both the same stub | list every tool name starting with `mcp__`, call none | 2, 3.8 s, $0.032 |
| timeout | `--strict-mcp-config`, `MCP_TOOL_TIMEOUT=15000` | call `mcp__aos__aos_sleep` with seconds 30, quote the result or error | 4, 21.2 s, $0.025 |

Every run printed `P1 <mode>: claude 2.1.296 (Claude Code) uid=1000 HOME=/home/sandbox
CLAUDE_CONFIG_DIR=/tmp/claude-home cwd=/tmp/p1/proj token_set=yes profile_d=sourced` and
ended with `claude exit 0`; every `result` event had `"subtype":"success","is_error":false`.

## Answers

**1. Do PostToolUse and Stop hooks declared in the `--settings` file fire under `claude -p
--setting-sources ""`?** Yes, both. Main run, stream events:
`{"type":"system","subtype":"hook_started","hook_name":"PostToolUse:Bash","hook_event":"PostToolUse"}`
then `hook_response` with `"exit_code": 0, "outcome": "success"` and the hook's JSON in
`"stdout"`; then `hook_started` / `hook_response` for `"hook_name": "Stop"` twice. The hook
logs written inside the container: `post.log` one line `{"hook_event_name": "PostToolUse",
"tool_name": "Bash", "permission_mode": "dontAsk", "cwd": "/tmp/p1/proj", "tool_response":
{"stdout": "hello-aos", ...}}`; `stop.log` two lines, `"stop_hook_active": false` then
`"stop_hook_active": true`, each with `last_assistant_message`. The control and timeout runs
show the same two Stop lines.

**2. Does `additionalContext` reach the model?** Yes. The PostToolUse hook injected
`PINEAPPLE-7f3e`; the assistant's next text was `Ping result: pong-3f9a from the aos
stub\n\nFINAL: PINEAPPLE-7f3e`. The Stop block reached it as a user message `Stop hook
feedback:\nAOS-P1 stop hook: before you finish, write one more line starting with FINAL2:
...`, answered by `FINAL2: BANANA-21c4`; the second Stop call (`stop_hook_active: true`)
printed nothing and the turn ended with `"result":"FINAL2: BANANA-21c4"`. A block also
emits `{"type":"system","subtype":"notification","key":"stop-hook-error","text":"Stop hook
error occurred · ctrl+o to see"}` in the stream: cosmetic, the run succeeded.

**3. Does a stdio server from `--mcp-config` load under `--strict-mcp-config` in `-p`?** Yes.
Main run `system/init`: `"mcp_servers":[{"name":"aos","status":"connected","source":"dynamic"}]`
and `"tools"` containing `mcp__aos__aos_echo`, `mcp__aos__aos_ping`, `mcp__aos__aos_sleep`.
The stub's log: `recv server/discover`, `recv initialize id=0`, `recv
notifications/initialized`, `recv tools/list id=1`, `recv tools/call id=2`, `call aos_ping
{}`; the transcript's tool result: `pong-3f9a from the aos stub`. Before the call the model
issued `ToolSearch {"query": "select:mcp__aos__aos_ping"}`: on 2.1.296 MCP tools are
deferred and loaded by name on demand, so the tool name and description are what the
model finds them by.

**4. Does a settings file allowing `mcp__aos__*`, with no `mcp__*` deny, expose only that
server's tools?** With the runner's flags, yes: the main run's `init.tools` holds exactly the
three `mcp__aos__*` names and no other `mcp__` entry (24 tools in all). The control run,
without `--strict-mcp-config` and with a user-scope `.claude.json` server and a project
`.mcp.json` server present, gave the same `init`: `"mcp_servers":[{"name":"aos",...}]`, the
same three tools, and the model's listing `mcp__aos__aos_echo\nmcp__aos__aos_ping\nmcp__aos__aos_sleep`.
So in this configuration the allow rule is not what keeps other tools out: nothing else
loaded even without strict. The project `.mcp.json` not loading matches the permissions
documentation ("`--setting-sources user` ... reads neither the project's settings files nor
its `.mcp.json`"); whether `$CLAUDE_CONFIG_DIR/.claude.json` is the location of user-scope
servers under `CLAUDE_CONFIG_DIR` is inferred from the config-dir documentation, unverified,
so the user-scope half of the control is weaker than the project half. `--strict-mcp-config`
stays in U3's command line as the documented guarantee.

**Negative control (the settings file was applied).** `init` reports
`"permissionMode":"dontAsk"` (from the file's `defaultMode`) and its `tools` list has no
`Write` while `Edit` and `Bash` are present: the bare-name deny removed the tool from the
model's context, as the permissions documentation states for bare tool names. The model's
own first line was `WRITE-PRESENT`, which was wrong: a model's self-report about its tool
list is not evidence; the `init` list is.

**5. How long may one MCP tool call block under `claude -p`?** From the environment-variable
reference (read 2026-10-14): `MCP_TOOL_TIMEOUT` "Timeout in milliseconds for MCP tool
execution (default: 100000000, about 28 hours)"; "Stdio and WebSocket servers have no
per-request timer"; "A per-server `timeout` field in `.mcp.json` overrides this for that
server". `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`: "When a stdio ... MCP server sends no response
and no progress notification for this long, the tool call aborts ... Overrides the
per-transport defaults of 300000 (5 minutes) for network servers and 1800000 (30 minutes)
for stdio servers ... capped at the effective `MCP_TOOL_TIMEOUT`".
`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` (default 120000) moves a still-running call to a
background task, but the MCP page says this does not apply in non-interactive mode unless
`CLAUDE_AUTO_BACKGROUND_TASKS=1`. Measured: with `MCP_TOOL_TIMEOUT=15000` and a 30 s call,
the tool result was `is_error: true`, text `MCP server "aos" tool "aos_sleep" timed out after
15s`, and the model wrote `RESULT: MCP server "aos" tool "aos_sleep" timed out after 15s`;
the stub logged `call aos_sleep {"seconds": 30}` and never a completion (the session ended
at 21 s). So under the runner's environment (no `MCP_TOOL_TIMEOUT`, no per-server timeout) a
silent stdio call is cut at 30 minutes; `aos_wait` in U2 caps one call at 15 minutes (the
plan's D5), well below that, and U3 may set a per-server `timeout` in `/sandbox/aos-mcp.json`
if it wants a tighter bound.

## Limits

- Not an OpenShell sandbox (above); the sandbox's Landlock policy and the `aos-bus` network
  rule are U3's to re-check, together with whether `/sandbox` paths behave as `/tmp/p1` did.
- One model (`claude-sonnet-5-5`, the `sonnet` alias) and one image version; the deferred
  `ToolSearch` step is a 2.1.x behaviour that may change.
- The Stop hook stub blocks on every first Stop, so each run spent one extra turn on it;
  that is the stub, not a finding.
