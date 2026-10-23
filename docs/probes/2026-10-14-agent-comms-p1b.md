# Agent communication probe P1b: `aos-agent` inside a real OpenShell sandbox on the Mac, 2026-10-14

Historical internal design/directives: `2026-10-14-agent-communication`, U2 and U3
(not distributed). [P1](2026-10-14-agent-comms-p1.md) ·
[Selected probes](public-index.md) · kit: [`scripts/openshell_probe/comms_p1b/`](../../scripts/openshell_probe/comms_p1b/README.md) ·
evidence: [`2026-10-14-agent-comms-p1b/`](2026-10-14-agent-comms-p1b/)

Closes P1's limit "`docker run` is not an OpenShell sandbox". The wave-1 `aos-agent`
(`docker/harness/aos_agent.py` at `agent-comms` 25a34af) ran inside real OpenShell sandboxes on
this Mac (gateway 0.1.3 on Colima's Docker 29.9.0, aarch64), in the image **`aos-harness:comms`**
(id `34ad426d78a4`, built from the copy's `docker/harness` with the legacy builder, every layer
before the `aos-agent` copy from cache; Claude Code 2.1.296), with the provider
`aos-claude-oauth`, against a disposable bus on `0.0.0.0:8012` run from the same checkout
(`uvicorn.run(create_app(Settings()), host="0.0.0.0", port=8012)`, data under the session
scratchpad, stopped and deleted afterwards). The U3 directive's S2-S6 files were written by
hand by the kit's driver, which imports the runner's own building blocks (`harness_command`,
`deadline_lines`, `wait_job_lines`, `SWEEP`, `build_prompt`, `CLAUDE_SETTINGS`,
`compare_effective`) so that the sandbox command, main script, prompt and settings are the
runner's. Each answer below is an observed line, cited to its evidence file.

**Headline.** The rendered `aos-bus` rule (`host.docker.internal:8012`) gives a sandbox on this
Mac no reach to the bus (Q2: no), as `docs/reference/openshell-backend-setup.md` recorded on
2026-10-12; U3 S2's `bus_url` built from `BUS_ALIAS[platform]` therefore cannot work on
`colima-mac`. Through an IPv4-literal rule for `192.168.5.2:8012` (the renderer's `services`
facility, the address `openshell_services.DB_DEFAULTS["colima-mac"]` already hands the sandbox)
everything else holds: the tools load, `aos_ask` / operator reply / `aos_wait` round-trip, the
no-matcher PostToolUse hook injects operator messages after an MCP call and after a Bash call,
the Stop hook blocks once, and the claim token leaks nowhere (Q1, Q3-Q6: yes). With
`"disableAllHooks": true` nothing is injected and nothing blocks; a message then reaches the
model only through its own `aos_inbox` call. Three Claude runs, $0.13 in all.

## Kit

`scripts/openshell_probe/comms_p1b/driver.py` (one `run` per mode) does, in order: create,
ready, claim and start a task on the bus (the inbox route needs a live run); `openshell sandbox
create --name p1b-* --from aos-harness:comms --policy <rendered> --provider aos-claude-oauth
--env CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 --env DISABLE_AUTOUPDATER=1 --cpu 2 --memory
2Gi --detach --no-tty -- bash -lc <main script>` (the runner's call, `openshell_runner.py` ~998);
wait for `/sandbox/incoming`; read back the effective policy and compare it with the rendering
(`compare_effective`, legacy mode); the Q1 exec; upload the S2 run file from a 0600 local copy
that is unlinked at once, then `chmod 600` inside; the Q2 exec; upload `/sandbox/aos-mcp.json`
(S3), `/sandbox/claude-settings.json` (S4 on top of `CLAUDE_SETTINGS`: `mcp__aos__*` appended
to `allow`, `deny` = `["WebFetch", "WebSearch"]`, `disableAllHooks` false, PostToolUse with no
matcher and Stop, both `/usr/local/bin/aos-agent ... --hook` with timeout 10) and
`/sandbox/prompt.txt` (`build_prompt(task)` plus the S6 paragraph verbatim); release the job
tarball (172 files, `run_job.py provision` of `~/code/aos-example` at `b75f6a6`) the way the
runner does; then play the operator against the bus with the bearer. The main script is
`openshell_runner.main_script`'s shape with the command below in the harness slot
(`run2-main/main-script.sh`).

The command is the runner's with S5's two flags after `--settings`, plus the probe's
instrumentation (recorded as a deviation): `--output-format json` replaced by
`--output-format stream-json --verbose --include-hook-events` so that which tool a hook fired
after is visible, and `--max-budget-usd 2` appended; model `sonnet` as in P1:

```
claude -p "$(cat /sandbox/prompt.txt)" --output-format stream-json --verbose --include-hook-events
  --setting-sources "" --settings /sandbox/claude-settings.json
  --mcp-config /sandbox/aos-mcp.json --strict-mcp-config --model sonnet --max-budget-usd 2
```

The task (`run2-main/prompt.txt`; no code word in it) asks the model to `aos_ask` the operator
"P1b: which fruit should the final line name?" and `aos_wait`, run `sleep 6; echo
step-two-done` and `sleep 6; echo step-three-done` with Bash, and end with a `FINAL:` line
naming the fruit and repeating every code word the operator sent; it says the operator's
messages need no reply (so that Q5 has an acknowledged, unanswered message to block on) and
asks for no commit. The `ready` route refused a task without an acceptance command, so the
task carries `test -f aos-policy.toml`, which nothing runs.

The operator side: on seeing the ask row, the driver posts `{to_task_id, body}` with a first
random code word (`<WORD>-<6 hex>` from `secrets.token_hex(3)`), then `{reply_to, body:
"The fruit is papaya."}`; once the first message is `acknowledged` on the bus it posts a second
code word. Two messages at different times because U2 S7's hook pulls at most once per 5 s,
and the two `sleep 6` steps keep the Bash hooks outside that interval.

Q6's search runs inside the sandbox with the token read from `/sandbox/aos-run.json` there
(never on a command line), reports counts only, and pairs every zero with a positive control
in the same invocation: `aos-policy` for the tree, the commit subject `aos run under AOS` for
the decompressed git objects (`git cat-file --batch-all-objects --batch`), the code words for
the stream. The downloads are scanned again on the host.

## Runs

| Run | Sandbox | Bus rule | Hooks | Task / run | Turns, time, cost | Result |
|---|---|---|---|---|---|---|
| 1 | `p1b-main` | rendered alias `host.docker.internal:8012` | on | AO-2 / AO-2-r1 | 7, 28.4 s, $0.046 | `success`; every bus call `bus unreachable: [Errno -5] No address associated with hostname` |
| 2 | `p1b-main2` | IPv4 literal `192.168.5.2:8012` (`aos-services`) | on | AO-3 / AO-3-r1 | 8, 34.0 s, $0.047 | `success`; `FINAL: papaya MANGO-2f4157 KIWI-3e768a` |
| 3 | `p1b-control` | IPv4 literal | `disableAllHooks: true` | AO-4 / AO-4-r1 | 7, 23.0 s, $0.041 | `success`; `FINAL: papaya MANGO-5b410f`; no hook event; the second word never arrived |

Two throwaway sandboxes without Claude (`p1b-dns`, `p1b-dns2`) measured name resolution
(`dns-sandboxes.txt`). Every sandbox was deleted after its run (`sandbox list` empty).
Every `policy get --full` matched its rendering (`effective_policy_problems: []`).

## Answers

**Q1. `/usr/local/bin/aos-agent` exists, mode 0755, and under `bash -lc` as uid 1000
`aos-agent mcp` answers `initialize`: yes.** Exec in every sandbox (`run2-main/checks.txt`, q1):
`mode=755 uid=0 path=/usr/local/bin/aos-agent`, `uid=1000 shell=bash`, and the initialize
request answered `{"jsonrpc":"2.0","id":0,"result":{"protocolVersion":"2025-06-18",
"capabilities":{"tools":{}},"serverInfo":{"name":"aos","version":"1"}}}`, `mcp_rc=0`. The
Claude run's `system/init` lists `"mcp_servers":[{"name":"aos","status":"connected",
"source":"dynamic"}]` and exactly the five `mcp__aos__*` tools (25 tools in all), in run 1 as
in run 2 (`*/stream-events.txt` line 000).

**Q2. `aos-agent` reaches the 8012 bus through the rendered policy's `aos-bus` rule at
`http://host.docker.internal:8012`, and nothing else is opened for it: no.** Run 1
(`run1-alias/checks.txt`, q2): `aos_inbox` through `aos-agent mcp` returned `{"content":[{"type":
"text","text":"bus unreachable: [Errno -5] No address associated with hostname"}],"isError":true}`;
`curl` to `host.docker.internal:8012` rc 6 (cannot resolve), and the Claude run's every
`aos_ask`, `aos_inbox` and hook call failed the same way (`run1-alias/stream-events.txt` 007,
010, 019 and the hooks' stderr at 004, 017, 024). Cause (`dns-sandboxes.txt`): inside the
workload `getent hosts host.docker.internal` is rc 2 (also `host.openshell.internal`), the
resolver is the supervisor's policy DNS (`nameserver 127.0.0.53`), and the supervisor logs
`NET:REFUSE [MED] DENIED host.docker.internal [reason:policy_dns_trusted_gateway_unavailable]`,
while `pypi.org` and `api.anthropic.com` get `198.18.0.x` placeholders and connect. A plain
`docker run` container under the same Colima resolves the alias to `192.168.5.2`. The
rendering is right and the effective policy carries the rule; the name has no address where
the rule applies. `docs/reference/openshell-backend-setup.md` ("Measured 2026-10-12 ... an
agent on the Mac cannot reach the bus from inside its sandbox") already says so; the P1b brief
and U3 S2 (`bus_url` from `BUS_ALIAS[settings.platform]`) did not carry it. Linux was not
measured here; the 2026-10-11 record has `host.openshell.internal` resolving there (IPv4-literal
`grpc_endpoint`, supervisor `ExtraHosts`).

What does work (`dns-sandboxes.txt`, p1b-dns2, and run 2's q2): a policy rendered with
`services=(("192.168.5.2", 8012),)` carries an `aos-services` endpoint with the literal; from
inside, `curl http://192.168.5.2:8012/health` is 200 with the bus's health body, `aos_inbox`
through `aos-agent mcp` returns `inbox empty`, and the gateway logs `NET:OPEN [INFO] ALLOWED
/usr/bin/python3.11(0) -> 192.168.5.2:8012 [policy:aos-services engine:opa]` followed by
`HTTP:POST [INFO] ALLOWED POST http://192.168.5.2:8012/runs/AO-3-r1/requests` (`run2-main/
checks.txt`, gateway lines). "Nothing else": `192.168.5.2:8010` and `:8011` are
`transparent_tcp_policy_denied` (curl rc 7), `example.com` is `policy_dns_ineligible` and
denied, the alias stays unresolvable; the effective policy holds exactly the rendered rules
plus the gateway's `_provider_aos_claude_oauth` rule (`run2-main/effective-policy.yaml`).
Runs 2 and 3 use this rule and `bus_url = http://192.168.5.2:8012` in the run file (the
orchestrator accepted the deviation mid-run).

**Q3. One real `claude -p` run loads the aos tools, calls `aos_ask("operator", ...)`, the row
appears on the bus, the operator answers with the bearer, `aos_wait` returns the answer: yes.**
Run 2 (`run2-main/stream-events.txt`): 001 the model's `ToolSearch
{"query": "select:mcp__aos__aos_ask,mcp__aos__aos_wait,mcp__aos__aos_inbox"}` (deferred MCP
tools, as P1 found); 006 `mcp__aos__aos_ask {"to": "operator", "text": "P1b: which fruit should
the final line name?"}`; 009 tool result `sent AO-3-i1 to operator`. On the bus
(`run2-main/checks.txt` timeline, `bus-intents.json`): `operator.ask_seen id=AO-3-i1
state=stored` at 19:25:48.396; the driver's `POST /tasks/operator/intents {reply_to: "AO-3-i1",
body: "The fruit is papaya."}` made `operator-i2`; 010 `mcp__aos__aos_wait {"id": "AO-3-i1"}`;
013 tool result `<operator-message id="operator-i2" to-task="AO-3" reply-to="AO-3-i1">\nThe
fruit is papaya.\n</operator-message>\nThis is from the operator.` Final bus rows: `AO-3-i1`
`answered`, `operator-i2` `acknowledged` (acked by `aos_wait`), gateway lines `POST
.../tasks/AO-3/intents`, `POST .../intents/AO-3-i1/wait`, `POST .../intents/operator-i2/ack`.

**Q4. An operator message sent during the run is injected by the PostToolUse hook after the
agent's next tool call, after an MCP tool call as well as after a Bash call, and the model uses
the code word: yes, both.** The first message (`operator-i1`, code word `MANGO-2f4157`) was
posted at 19:25:48.404 while the model sat in `aos_wait`; 011-012 `hook_started
hook_name=PostToolUse:mcp__aos__aos_wait` then `hook_response exit_code=0 stdout=
{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"<operator-message
id=\"operator-i1\" to-task=\"AO-3\">\nOperator note: the code word for this run is
MANGO-2f4157. No reply needed.\n</operator-message>\nThis is from the operator."}}`; the bus
saw `operator-i1` `acknowledged` at 19:25:49.966. The second (`operator-i3`, `KIWI-3e768a`)
was posted at 19:25:49.974; 014 `Bash {"command": "sleep 6; echo step-two-done"}`, 017-018
`hook_started hook_name=PostToolUse:Bash` then `hook_response ... additionalContext:
"<operator-message id=\"operator-i3\" ...a second code word, KIWI-3e768a..."`. The hook after
the second Bash (024) and after `aos_inbox` (028) printed nothing (nothing pending; 029
`inbox empty`). The model's text at 030 and its result at 041: `FINAL: papaya MANGO-2f4157
KIWI-3e768a`; the host-side scan counts 6 lines with each code word in `harness.out` and 0 in
`harness.err`. The no-matcher hook also fires after `ToolSearch` (002-004, in both runs) and
after `aos_ask` (007-008), which P1 could not show.

**Q5. The Stop hook blocks once on an acknowledged, unanswered operator message, and the run
ends: yes.** 031-032 `hook_started hook_name=Stop` then `hook_response exit_code=0 stdout=
{"decision":"block","reason":"Requests you acknowledged and have not answered:\n- operator-i1
from operator: Operator note: the code word for this run is MANGO-2f4157. No reply needed.\n-
operator-i3 from operator: Operator note: a second code word, KIWI-3e768a. No reply
needed.\nAnswer each with aos_reply, or say in your report why you did not."}`; 033 the model
received it as `Stop hook feedback: ...`; 038 it answered `I didn't send aos_reply for
operator-i1 or operator-i3. Both said "No reply needed", and the task said the operator's
messages need no reply.\n\nFINAL: papaya MANGO-2f4157 KIWI-3e768a`; 039-040 the second Stop
hook printed nothing; 041 `result subtype=success is_error=False num_turns=8 duration_ms=34019
cost_usd=0.047`, `harness.rc` 0. The in-sandbox ledger had 8 lines (`ledger_lines=8`); the
cosmetic `stop-hook-error` notification (034) is the one P1 saw. The rows stayed
`acknowledged` on the bus, which is what the Stop reason reflects.

**Q6. The claim token appears nowhere in Claude's output JSON, the hook stderr, the job tree
(including git objects) or anything downloaded: yes, with controls.** Run 2 in-sandbox scan
(`run2-main/checks.txt`, q6): `token_in_repo_tree_files=0` (`control_in_repo_tree_files=2`),
`token_in_git_objects_lines=0` (`control_in_git_objects_lines=1`), `token_in_harness_out_lines=0`
(code words 6 and 6), `token_in_harness_err_lines=0`, `token_in_ledger_lines=0`,
`token_in_claude_home_files=0` over 7 files under `/tmp/claude-home`, `runfile_mode=600`. Host
side: `token_lines` 0 in `harness.out` (27,472 bytes), `harness.err` (0 bytes: the hooks'
stderr goes into the stream's `hook_response` events, which hold none) and `harness.rc`; the
gateway log (`gateway-log.txt`, 92 network lines) names paths only, never bodies. Run 1 gave
the same zeros with the same controls. The hook stderr seen in run 1 (`aos-agent: inbox hook:
bus unreachable: ...`) carries the error, not the credential.

## Negative control (`"disableAllHooks": true`, run 3)

Run 3 (`p1b-control`, task AO-4 / run AO-4-r1, the same files as run 2 except
`"disableAllHooks": true` in `claude-settings.json`; `run3-control/`): 7 turns, 23.0 s, $0.041,
`success`. The stream holds no `hook_started` or `hook_response` event at all (0 against 16 in
run 2; `run3-control/stream-events.txt`). The exchange itself needs no hook: 005 `aos_ask` gave
`sent AO-4-i1 to operator`, 007-008 `aos_wait` returned the operator's framed answer
(`operator-i5`). The first code word (`operator-i4`, `MANGO-5b410f`, posted at 19:27:17.127
while the model sat in `aos_wait`) was **not injected**: no `additionalContext` anywhere. It
reached the model only when the model itself called `mcp__aos__aos_inbox` after the two Bash
steps (020; S6's "Call aos_inbox before you commit" and the task's step 4 invite it), and the
tool result at 021 carries the frame; the bus saw `operator-i4` acknowledged at 19:27:34.596,
17 s after it was sent (run 2, by the hook: 1.6 s). The second code word (`operator-i6`,
`KIWI-d89f69`, posted at 19:27:34.603 on that ack) never reached the model: 0 lines in the
stream, the row stayed `stored` on the bus, the run ended 3 s later with `FINAL: papaya
MANGO-5b410f`. No Stop hook ran, so the acknowledged, unanswered `operator-i4` blocked nothing
(run 2's Q5 block was the hook's). So with hooks off nothing is injected, and a message reaches
the model only through its own `aos_inbox` call; the two paths are distinguishable in the
stream (`user/tool_result` of `mcp__aos__aos_inbox` against `hook_response ...
additionalContext`). The brief's literal expectation that the code word must not reach the
model holds for the second word and is missed for the first by the tool path, the tension
between the control and S6's paragraph flagged in the critique before the first build call.

**Instrument check.** The code words were drawn per run (`MANGO-25c0fb`/`KIWI-1f3dff` in run 1,
`MANGO-2f4157`/`KIWI-3e768a` in run 2, `MANGO-5b410f`/`KIWI-d89f69` in run 3) and appear only
in the operator's message bodies; the prompt (`run2-main/prompt.txt`) holds none, and run 1,
where no message was ever delivered, has both counts at 0 in its stream.

## Findings for U3 and U4

1. **Bus endpoint on `colima-mac`.** U3 S2's `bus_url = http://{BUS_ALIAS[platform]}:{port}`
   and the rendered `aos-bus` rule give a Mac sandbox no reach (Q2). The working shape is an
   IPv4 literal (`192.168.5.2`) in a policy rule and in the run file; whether it goes into the
   `aos-bus` rule per platform (Linux keeps the alias or takes the docker0 address) is a
   sandbox-boundary design decision for the operator, not settled here.
2. **The hook fires after every tool, `ToolSearch` included**, so the first inbox pull happens
   before the model's first real tool call; the 5 s interval (U2 S7) then governs. Two tool
   calls inside 5 s pull once (run 2: the hook after `aos_ask` at 008 had nothing to pull, the
   one after `aos_wait` did).
3. **The ledger under `/tmp/aos-agent` did not come back through `openshell sandbox
   download`** (run 2: no error surfaced, no file), while `sandbox exec cat` reads it; the
   driver now reads it that way (after run 3 had started, so no run here has a ledger copy;
   the in-sandbox counts stand in).
4. `build_prompt`'s fixed sentence "Commit your changes on the current branch" sits under a
   task that says "make no commit"; the model made none and said so (030). A probe task is an
   odd fit for the runner's prompt shape, not a defect.
5. The gateway logged two `claude.exe -> 198.18.0.3:443 [reason:transparent_tcp_mapping_denied]`
   lines in run 2 beside the allowed provider traffic; the run succeeded, so some Claude
   connection to the provider host is refused by the credential mapping. Not pursued.
6. Claude's `init` reports `hooks` absent even with the S4 hooks in force (`hooks=absent-or-
   empty` at line 000 in every run); the `hook_started` events are the evidence, not `init`.
7. **Without hooks, delivery is only as fresh as the model's last `aos_inbox` call** (run 3:
   17 s for the first word, never for the second, which stayed `stored`). U4's expectation of
   "at the next tool call" delivery holds only with hooks on; with O1 (reading acknowledges)
   a message the model never reads stays unacknowledged on the bus, which is what the
   operator sees.
8. The orchestrator confirmed the Q2 finding and the IPv4-literal deviation mid-run (message
   received 19:26 CEST) and placed the endpoint question (finding 1) with the operator.

## Limits

- One Mac, one gateway version, one model (`claude-sonnet-5-5`), one image build; Linux and
  the alias on Linux are unmeasured.
- The bus rule used for Q3-Q6 is the renderer's `aos-services` facility, not the `aos-bus` rule
  the brief named; the rule name does not change what the sandbox can reach, the endpoint does.
- Output format `stream-json` instead of the runner's `json`; the hook and MCP mechanics do not
  depend on it, but the runner's own `json` output was not exercised with comms on.
- No ledger file was copied out (finding 3); no two-agent exchange (the operator was scripted).
- The Stop block was reached because the task told the model not to reply; a model that
  answers with `aos_reply` first leaves nothing open and the hook passes, by design (U2 S8).
