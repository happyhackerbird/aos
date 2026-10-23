# Pi and omp mechanisms inside an aos OpenShell sandbox on the Mac, 2026-10-14/15

[Selected probes](public-index.md) · predecessors: [statiker mechanisms on Claude](2026-10-14-statiker-mechanisms.md),
[Codex mechanisms](2026-10-14-codex-mechanisms.md) ·
kit: [`scripts/openshell_probe/pi_mech/`](../../scripts/openshell_probe/pi_mech/README.md) ·
evidence: [`2026-10-14-pi-mechanisms/`](2026-10-14-pi-mechanisms/)

This probe asks, for each mechanism a statiker run would use on Pi 1.1.0 or omp 18.8.3, whether
it works inside an aos OpenShell sandbox under the aos-example policy, and whether the policy's
boundaries still hold through it. A Pi-family harness has no hooks file and no plugin loader: an
extension is the hook mechanism (its event handlers block, note or log), skills are directories,
and subagents are a package (Pi: pi-subagents) or a native tool (omp: `task`). The runner today
passes `--no-extensions --no-skills` (and Pi's `--no-mcp`, omp's `--no-rules`), so the question is
whether explicit paths carry the mechanisms under those flags, and what those flags leave on.

The runs used the Mac gateway (OpenShell 0.1.3 on Colima), the image `aos-harness:comms`, the
worker's nonrenewable Pi auth snapshot exactly as the runner uploads it (`pi.selected_auth`;
`auth.json` for pi, `agent.db` for omp), model `gpt-6-luna` (the qualification model), child model
`openai-codex/gpt-5.6-terra`. Seven `--print --mode json` runs. Each verdict below quotes the line
it rests on and names its evidence file; the kit's `checks.py` reads the evidence mechanically, and
its P8 row applies the runner's own parser (`agent_os.pi`).

**Headline.** An explicit `-e <path>` loads an extension under the runner's `--no-extensions` on
both flavours, and everything the extension does works: its custom tool, its `tool_call` block,
its `tool_result` note, its fire log, and its own process sees the sandbox boundary. Skills and
subagents differ per flavour:

- **Pi:** `--skill <dir>` loads a skill under `--no-skills`; discovery stays off (the repository's
  `.agents/skills` is not read). Subagents are pi-subagents: the worker's 0.68.0 (built for pi
  0.85.1) loads, lists agents and fills the model, but cannot spawn on 1.1.0 (`lacks
  @earendil-works/pi-agent-core/node`, a subpath pi 1.1.0's core no longer exports); 0.78.0 loads
  without error but registered no `subagent` tool in the print-mode session.
- **omp:** skills come from `$PI_CODING_AGENT_DIR/skills` and the repository's `.agents/skills`
  (not from `$HOME/.omp/agent/skills`); agents come from `$HOME/.omp/agent/agents` only (HOME is
  the runner's `/tmp/pi-home`); the native `task` tool is present under the runner's flags too;
  an isolated task runs in a copy under `$HOME/.omp/wt/<id>/m` on the agent file's model, and
  `before_subagent_spawn` fires with the agent and its model patterns.
- the operator's pi-dispatch-guards loads on Pi by `-e` and its model-fill patched the subagent call;
  its child registration needs pi-subagents at `~/.pi/agent/npm/node_modules/...` (a symlink
  there; the `.ts` path it names is gone in 0.78.0's layout).

## Setup

| Item | Value (source) |
|---|---|
| agent-os base | `731bc90` (worktree `codex-pi-mech`) |
| aos-example | `b75f6a6`; job fixture commit with `.agents/skills/repo-skill/SKILL.md`: `b85f234` (runs 1, 3-7), a fresh one for run 2 (`prepare-run2.json`) |
| Image | `aos-harness:comms` id `34ad426d78a4`; pi 1.1.0, omp/18.8.3 (`run*/exec-reads.txt`, `pre-version`) |
| Gateway | `Server: https://localhost:17670`, `Status: Connected`, `Version: 0.1.3` (`run*/gateway-status.txt`) |
| Policy | rendered from `~/code/aos-example/aos-policy.toml` (platform `colima-mac`, bus port 8015, no provider host, harness host `chatgpt.com:443`); `policy.yaml`; strict readback matched in every run (`"effective_policy_problems": []`) |
| Credential | `~/.aos/worker/pi-auth.json` selected by `pi.selected_auth` (valid 186 h at the time), snapshotted with the placeholder refresh value, uploaded 0600 to `/tmp/pi-agent/auth.json` (pi) or `/tmp/pi-agent/agent.db` (omp); its digest in `exec-reads.txt` `auth-sha` |
| Fixtures | `pi-probe/`: `index.ts` (the extension), `skills/{probe-skill,probe-skill-home}/SKILL.md` with fresh phrases, `agents/pi/probe-agent.md` and `agents/omp/{probe-agent,probe-agent-home}.md` (`model: openai-codex/gpt-5.6-terra`), `overlay-isolation.yml`, `dispatch-guards.json`; `pi-dispatch-guards/` (`~/code/pi-dispatch-guards` at `0487a5d`); `statiker/` (`~/plugins/statiker` at `7a4e10e`, without tests, docs and probes); the Pi packages tree: the worker's `~/.pi/agent/npm/node_modules` (pi-subagents 0.68.0, pi-intercom 0.13.0; 7.6 MB) for run 1, a scratch `npm install --legacy-peer-deps pi-subagents@0.78.0 pi-intercom` tree (0.78.0, 0.17.0; 9.2 MB, uploaded in two parts) for run 2 |
| Where they land | pi: `/tmp/pi-agent/agents/probe-agent.md`, `/sandbox/.pi/agent/dispatch-guards.json`, `/sandbox/.pi/agent/npm/node_modules` → the packages tree (run 2); omp: `/tmp/pi-agent/{agents,skills}/` and `/tmp/pi-home/.omp/agent/{agents,skills}/` (`install`) |

The command is the runner's (`pi_invocation`) with the mode's flags appended:

| Flavour, mode | Appended |
|---|---|
| pi mech | `-e /sandbox/plugins/pi-probe/index.ts -e /sandbox/plugins/pi-npm/node_modules/pi-subagents -e /sandbox/plugins/pi-dispatch-guards --skill /sandbox/plugins/pi-probe/skills/probe-skill` |
| pi control | nothing |
| omp mech | `--no-skills` dropped; `-e /sandbox/plugins/pi-probe/index.ts --config /sandbox/plugins/pi-probe/overlay-isolation.yml --skills=probe-*,repo-*` |
| omp control | nothing |
| omp statiker | omp mech plus `-e /sandbox/plugins/statiker/omp/extension.ts` |

The main script is `openshell_runner.main_script` line for line (asserted before every run), the
prompt on stdin from `/sandbox/prompt.txt`, the create call the runner's for a Pi run (no
provider). Each run's `harness-command.txt`, `main-script.sh` and `prompt.txt` are in its
evidence directory.

## Runs

| Run | Sandbox | Flavour, mode | Wall clock | Session | Events | `harness.rc` |
|---|---|---|---|---|---|---|
| 1 | `pim-pi-mech` | pi mech, pi-subagents 0.68.0 | 65.9 s | `25c334ad-…` requested and observed | 2181 | 0 |
| 2 | `pim-pi-mech` | pi mech, pi-subagents 0.78.0 | 40.7 s | `99172165-…` requested and observed | 1290 | 0 |
| 3 | `pim-pi-control` | pi control | 25.4 s | `c69d5f07-…` | 777 | 0 |
| 4 | `pim-omp-mech` | omp mech, `probe-agent` under `$PI_CODING_AGENT_DIR/agents` only | 127.0 s | `01a13c7c-…` observed | 4706 | 0 |
| 5 | `pim-omp-control` | omp control | 40.7 s | `01a13c7e-…` observed | 843 | 0 |
| 6 | `pim-omp-mech` | omp mech, `probe-agent` in both agent dirs | 127.2 s | `01a13c81-…` observed | 3258 | 0 |
| 7 | `pim-omp-statiker` | omp statiker (the operator's `statiker/omp/extension.ts` by `-e`) | 128.6 s | observed | 3721 | 0 |
| 8 | `pim-omp-stlink` | omp statiker-link (`omp plugin link` under omp's HOME, `--no-extensions` dropped) | 86.2 s | `01a13c8a-…` observed | 3746 | 0 |
| 9 | `pim-omp-toolsoff` | omp tools-off (the control command plus `--tools=`, 2026-10-15) | 40.8 s | `01a140b0-…` observed | 863 | 0 |

Wall clock from `run-summary.json`, events from `checks.txt`. Run 2 repeats run 1 with the newer
package after run 1's spawn failure; run 6 repeats run 4 after run 4 showed where omp reads its
agents. The three runs first launched for the control and omp modes never started (a shell loop
that did not split its arguments; no sandbox was created). Every sandbox list before and after
every run reads `No sandboxes found.`; the buses on 8010 and 8011 were not touched.

## Verdicts

Pi columns: run 1 (run 2 where it differs), run 3. omp columns: run 6 (run 4 where it differs),
run 5.

| Row | pi mech | pi control | omp mech | omp control | Evidence |
|---|---|---|---|---|---|
| P1 custom tool from the `-e` extension | PASS | PASS (no tool) | PASS | PASS (no tool) | below |
| P2 skills | PASS (explicit) | PASS (none) | PASS (agent dir + repo) | PASS (none) | below |
| P3 the extension's hooks | PASS | PASS (nothing fires) | PASS | PASS (nothing fires) | below |
| P4 worktrees | PASS | not asked | PASS | not asked | below |
| P5 subagents | FAIL (0.68.0 cannot spawn; 0.78.0 registers no tool) | PASS (no tool) | PASS (run 4: first spawn unknown agent) | PASS as measured: `task` present | below |
| P6 boundaries in bash | PASS | PASS | PASS | not asked | below |
| P7 state directories | PASS | PASS | PASS | PASS | below |
| P8 the runner's parser | PASS | PASS | PASS | PASS | `checks.txt` |

**P1, a custom tool: PASS in every mech run.** Pi (`run1-pi-mech/stream-events.txt`): a
`tool_start probe_echo args={"text": "hello-from-the-desk"}` and its `tool_end ... result=PROBE-ECHO-OK
hello-from-the-desk`. omp exposes a custom tool as a device: the model read `xd://probe_echo` (the
schema, "Execute by writing JSON to xd://probe_echo") and wrote `{"text":"hello-from-the-desk"}` to
it (`run6-omp-mech/stream-events.txt` 010-015); the extension's fire log holds the execution
(`probe_echo: 1`) and the report `PROBE-ECHO-OK hello-from-the-desk`. Controls: `NO-TOOL: bash,
read, edit, write` (pi) and `NO-TOOL: read, bash, edit, eval, glob, grep, task, wait, todo, write,
web_search, multi_tool_use.parallel` (omp).

**P2, skills.** Phrases from `prepare-runs1-5.json`:

| Skill | pi mech (run 1) | omp mech (run 6) |
|---|---|---|
| `probe-skill` | `Code phrase: PHRASE-PROBE-d0d3d5`, path `/sandbox/plugins/pi-probe/skills/probe-skill/SKILL.md` (the explicit `--skill`) | the same phrase, path `/tmp/pi-agent/skills/probe-skill/SKILL.md` (`$PI_CODING_AGENT_DIR/skills`) |
| `probe-skill-home` | `NO-SKILL` (not shipped to pi) | `NO-SKILL` (`/tmp/pi-home/.omp/agent/skills/`, not read) |
| `repo-skill` | `NO-SKILL` (`--no-skills`: no discovery) | `Code phrase: PHRASE-REPO-8561f7`, path `/sandbox/repo/.agents/skills/repo-skill/SKILL.md` |
| `nope-skill` | `NO-SKILL` | `NO-SKILL` |

Both controls: every skill `NO-SKILL`.

**P3, the extension's hooks: PASS in every mech run.** The fire log and its location
(`exec-reads.txt`, `firelog`):

- Pi: `/sandbox/.local/share/pi-probe/fires.jsonl` (HOME is `/sandbox`), 23 lines: `session_start
  1, tool_call 20, tool_result 1, probe_echo 1`. omp: `/tmp/pi-home/.local/share/pi-probe/fires.jsonl`
  (HOME is the runner's `/tmp/pi-home`), 45 lines in run 6: `session_start 3` (the desk and its
  two children load the extension too), `tool_call 38`, `tool_result 1`, `probe_echo 1`,
  `before_subagent_spawn 2`.
- **The boundary from the extension's own process** (`session_start`): pi `{"home": "/sandbox",
  "cwd": "/sandbox/repo", "argv0": "/usr/local/bin/node", "token_entries": 0,
  "env_keys_of_interest": ["CLAUDE_CONFIG_DIR", "CODEX_HOME", "HOME", "PI_CODING_AGENT",
  "PI_CODING_AGENT_DIR", "PI_OFFLINE", "PI_SKIP_VERSION_CHECK"], "fetch": "error TypeError EACCES
  connect EACCES 198.18.0.2:443 - Local (0.0.0.0:0)", "usr_write": "EACCES errno -13",
  "state_write": "ok /sandbox/.local/state/pi-probe"}`; omp `{"home": "/tmp/pi-home", ...,
  "argv0": "bun", "token_entries": 0, "env_keys_of_interest": ["CLAUDE_CONFIG_DIR", "CODEX_HOME",
  "HOME", "PI_CODING_AGENT_DIR", "PI_NO_TITLE"], "fetch": "error TypeError ConnectionRefused Unable
  to connect. Is the computer able to access the url?", "usr_write": "EACCES errno -13",
  "state_write": "ok /tmp/pi-home/.local/state/pi-probe"}`. Landlock and the network policy bind
  the in-process Node (pi) and Bun (omp) calls as they bind shell children.
- **The block.** The P3b bash call ended `isError=True result=[pi-probe] denied by the probe
  extension (EXT-DENY-ME)`, twice (the command and the model's heredoc quoting the marker); no
  `EXT-DENY-ME` output exists. P3a (`EXT-ALLOWED-OK`) ran.
- **The note.** The P3c result reads `## P3c\n@@ note\nEXT-NOTE-ME\n\n[pi-probe] note appended by
  the probe extension` (omp adds its `Wall time: 0.00 seconds` line before the note).
- **Controls:** no fire log at any candidate path, P3b ran (`## P3b\n@@ deny\nEXT-DENY-ME`).
- **pi-dispatch-guards on Pi** (`run1-pi-mech/exec-reads.txt`,
  `/sandbox/.local/share/pi/dispatch-guards-fires.jsonl`, 2 lines): `{"guard":
  "child-registration", "mode": "error", ..., "reason": "Error: Cannot find module
  '/sandbox/.pi/agent/npm/node_modules/pi-subagents/src/api/required-child-extensions.ts'"}`
  (the extension resolves pi-subagents at that fixed path; run 1 had no link there, run 2 links it
  but 0.78.0 ships `.js`, so the `.ts` file does not exist) and `{"guard": "model-fill", "mode":
  "patch", "tool": "subagent", ..., "reason": "model-fill:openai-codex/gpt-5.6-terra"}`: the guard
  patched the model into the subagent call as it does on the host.

**P4, worktrees: PASS.** Both flavours (`checks.txt`): `git worktree add --detach /tmp/pi-wt-1`
rc 0 and listed, remove rc 0, `/usr/local/pi-wt` → `fatal: could not create leading directories of
'/usr/local/pi-wt/.git': Permission denied` rc 128; nothing left after the run.

**P5, subagents.**

- **Pi, run 1 (pi-subagents 0.68.0): the tool is there, the spawn is not.** The `subagent` tool
  answered `action: list` with `User agents - probe-agent (user): ... Model:
  openai-codex/gpt-5.6-terra; Thinking: low` and the builtin agents, `action: models` with the
  session model, and `action: guide`; the model then called the tool with a `workflowScript`
  (async), got `Async workflow [...] The async run is detached`, and reported `P5 (i) failed to
  launch because the subagent runtime lacks `@earendil-works/pi-agent-core/node``
  (`run1-pi-mech/stream-events.txt` 080-094). No child file, no child session. The cause is a
  version gap: pi 1.1.0 ships `@earendil-works/pi-agent-core` 1.2.0 whose `exports` are `.` and
  `./package.json` only, while 0.68.0's runner aliases `@earendil-works/pi-agent-core/node` as
  required; pi-subagents 0.78.0 marks that alias optional ("hosts whose pi-agent-core declares no
  './node' export cannot import it"). The prompt's direct-form instruction (fields `agent`,
  `task`, `model` only) came after this run.
- **Pi, run 2 (0.78.0): no tool.** The model's tools were `read, bash, edit, write, probe_echo`
  (`NO-SUBAGENT-TOOL`), `harness.err` holds only the session-id warning, and the same tree loads
  without error in a plain container of the image (`pi --verbose -e <tree>/pi-subagents` reaches
  the provider call). Its `index.js` registers `subagent` unconditionally after a
  `registerSlashSubagentBridge`/`registerPromptTemplateDelegationBridge` setup; why the print-mode
  session did not list it is not isolated here.
- **omp, run 4: where agents live.** The first `task` call answered `Task ProbeFirst failed
  preflight: Unknown agent "probe-agent". Available: probe-agent-home, scout, reviewer,
  security-reviewer, task, sonic. Searched: ~/.omp/agent/agents`; the agent file under
  `$PI_CODING_AGENT_DIR/agents` is not read. The second call (`probe-agent-home`, not isolated)
  ran as `ProbeSecond` on `openai-codex/gpt-5.6-terra` (its session file
  `<parent session dir>/ProbeSecond.jsonl`, `assistant models=['openai-codex/gpt-5.6-terra']`),
  `curl` 7, `env` 0, cwd `/sandbox/repo`.
- **omp, run 6: PASS.** The report's agents line: `probe-agent-home, probe-agent, scout,
  reviewer, security-reviewer, task, sonic`. The `task` arguments (`stream-events.txt`):
  `{"context": "...", "tasks": [{"name": "ProbeOne", "agent": "probe-agent", "task": "...",
  "isolated": true, ...}]}` and `{"tasks": [{"name": "ProbeTwo", "agent": "probe-agent-home",
  ...}]}`. `ProbeOne` (isolated) ran in `/tmp/pi-home/.omp/wt/t81b435787/m` (its `pwd`, its bash
  calls' `cwd`, its session file's cwd), `ProbeTwo` in `/sandbox/repo`; both on
  `openai-codex/gpt-5.6-terra` by their session files
  (`/sandbox/pi-sessions/<ts>_<parent>/ProbeOne.jsonl`, `ProbeTwo.jsonl`); both `curl` 7, `env` 0,
  `NO-CODEWORD`. The extension's `before_subagent_spawn` fired for each:
  `{"type":"before_subagent_spawn","agent":"probe-agent","invocationKind":"task","patterns":["openai-codex/gpt-5.6-terra"],"spawnKey":"ProbeOne"}`.
  The isolation copy is omp's own worktree mechanism (`task.isolation.enabled` from the overlay),
  inside the policy's writable `/tmp`.
- **omp control: the `task` tool is on.** `SUBAGENT-TOOL: task` under the runner's exact flags
  (`--no-extensions` does not remove it); not called in the control.
- **omp tools-off, run 9: `--tools` closes it.** The control command plus
  `--tools=read,bash,edit,eval,glob,grep,wait,todo,write,web_search` (run 5's tools minus `task`),
  the control prompt: the report reads `NO-SUBAGENT-TOOL: read, bash, eval, glob, grep, wait, todo,
  write, web_search, edit, multi_tool_use.parallel`, no `task` call, no child file, P5 PASS as a
  control (`run9-omp-tools-off/checks.txt`); every other graded row PASS, P8 included, and the
  secret scans 0 with the control 1. The allowlist keeps the other ten tools as they were; `eval`
  is accepted as a tool name.
- **Code word:** 0 occurrences in every `subagent`/`task` argument and every child file; the
  positive control is the one occurrence in each desk's final message.

**P6, boundaries in the model's bash: PASS.** `env | grep -c CLAUDE_CODE_OAUTH_TOKEN` = `0` (a Pi
run attaches no provider, so there is no placeholder), `HOME=/sandbox PI_CODING_AGENT_DIR=/tmp/pi-agent`
(pi) and `HOME=/tmp/pi-home PI_CODING_AGENT_DIR=/tmp/pi-agent` (omp), `/usr/pi-mark` →
`PermissionError: [Errno 13] Permission denied`, the state write under `~/.local/state/pi-probe`
rc 0 (`/sandbox/...` for pi, `/tmp/pi-home/...` for omp).

**P7, state directories: PASS.** Pi: `/tmp/pi-agent` holds `auth.json` (digest `ece656a0…`,
the uploaded snapshot), `agents/probe-agent.md` and pi-subagents' `missions/` index; sessions
under `/sandbox/pi-sessions/<ts>_<id>.jsonl` plus a directory per session; `/tmp/pi-home` empty.
omp: `/tmp/pi-agent` holds `agent.db` (digest `04d818ff…`), `models.db`, `skill-descriptions.db`,
`custom-session-files/`, `agents/`, `skills/`; `/tmp/pi-home/.omp/wt/<id>/m` (the isolation copy),
`/tmp/pi-home/.bun/install/cache`, `/tmp/pi-home/.local/{share,state}/pi-probe`.

**P8, the runner's parser: PASS in every run.** `pi.parse_output` (pi) and `pi.parse_omp_output`
(omp) on the downloaded stream: `settled=True stop=stop error=None`, the requested session id
observed (pi), the observed id kept (omp).

## the operator's statiker-omp port (runs 7 and 8)

**Run 7, the extension by `-e`:** every P row PASS as in run 6 (`run7-omp-statiker/checks.txt`),
`harness.err` empty, and the catalog unchanged: the agents line reads `probe-agent-home,
probe-agent, scout, reviewer, security-reviewer, task, sonic`, the skills found are `probe-skill`
and `repo-skill`. So `-e /sandbox/plugins/statiker/omp/extension.ts` loads the extension's code
without error (as in a plain container of the image, where the same load reaches the provider
call), but brings none of the package's four role agents or its `statiker` skill: those are
package resources omp registers through `omp plugin link`, not through an extension file.

**Run 8, the package linked** (`pim-omp-stlink`, mode `statiker-link`, 86.2 s, rc 0: `omp plugin
link /sandbox/plugins/statiker/omp` by exec under omp's HOME before the harness answered `✔ Linked
statiker-omp from /sandbox/plugins/statiker/omp`, and the runner's `--no-extensions` was dropped so
the linked plugin is discovered): the catalog now carries the package's four role agents. The
report's agents line reads `probe-agent-home, probe-agent, statiker-attacker, statiker-implementer,
statiker-investigator, statiker-verifier, scout, reviewer, security-reviewer, task, sonic`; the
skills found are still `probe-skill` and `repo-skill` (the `--skills=probe-*,repo-*` filter of the
mech flags excludes the package's `statiker` skill by name). Every row but P5 PASS
(`run8-omp-statiker-link/checks.txt`); `exec-reads.txt` line 65 holds the link answer.

**Run 8, P5: FAIL on the kit's predicate, from the model's call shape.** omp refused both `task`
calls before any child started (`run8-omp-statiker-link/stream-events.txt` 096 and 106). The first
call sent an empty batch `context` and got ``Missing `context`. Provide the shared background for this
batch — goal, constraints, and any contract the tasks share.`` The second gave its item `"tools":
["bash"]`, which omp reads as eval-cell tool names, and got `Task execution failed: Unknown eval
tool(s): bash.` Both answers are omp's own: the strings are in the omp 18.8.3 binary, and neither
occurs in `~/plugins/statiker/omp`, so the linked package's gate did not produce them. No child ran:
one session file only, the four `sub-*` files absent, `before_subagent_spawn` 0 in the fire log; the
code word is clean (0 in the task arguments, 1 in the final message). Run 6 spawned children under the
same omp without the linked package, so run 8 says nothing against omp subagents; it shows no spawn
under the linked package either. Evidence landed with this paragraph (2026-10-15); the gateway log
stays out, summarized in `gateway-denied.txt` (18 DENIED lines).

So the path to the operator's statiker-omp port in a sandbox is the package link under omp's HOME with
discovery on, not an `-e` of its extension file.

## Token scan

Each run's `checks.txt` ends with two counts-only scans for the selected worker secrets (the
access token and account id `pi.selected_auth` returns); the downloads are also scrubbed with
`pi.redact_bytes` on the host before anything reads them. Every downloaded and evidence file: 0
hits, positive control 1, in every run. The in-sandbox scan read the secrets from the uploaded
snapshot (`auth.json` or the `agent.db` row) and found `{'access': 0, 'accountId': 0}` under
`/sandbox`, `/tmp` and `/home/sandbox` and in the git objects, with the controls found
(`aos-policy` 2, `pi-agent` 15, `PS1` 1).

## Gateway

Pi runs (`run1-pi-mech/gateway-denied.txt`): 2 ALLOWED lines (`chatgpt.com:443`), 3 DENIED (the
child-less `curl -> example.com:443` and its DNS). omp runs (`run6-omp-mech/gateway-denied.txt`):
16 ALLOWED, 28 DENIED: `curl`/`omp -> example.com:443` and their DNS, `omp -> 198.18.0.2:443`
(`transparent_tcp_mapping_denied`), and omp's startup model discovery for providers it is not
configured for (`catalog.stencil.so`, `coding-intl.dashscope.aliyuncs.com`, `api.commandcode.ai`,
`hyper.charm.land`), as the setup reference already notes.

## Deviations

1. **Run 1's subagent prompt** let the model choose pi-subagents' workflow form; the prompt now
   names the direct form (`agent`, `task`, `model`, nothing else). The version gap, not the form,
   stopped the spawn.
2. **Run 2's packages tree** is not the worker's: a scratch `npm install --legacy-peer-deps
   pi-subagents@0.78.0 pi-intercom` (35 MB; the peer packages are pi's own and aliased by its
   loader), uploaded in two parts (the kit's `upload_parts`, the runner's join-and-digest form).
3. **omp's agent location** was learned in run 4; the kit now installs `probe-agent` under both
   agent directories and run 6 is the graded one.
4. **P1 on omp** grades the device write plus the fire log, since omp exposes a custom tool as
   `xd://<name>` rather than as a tool call of that name.
5. **The guards' child registration** could only be served by a `~/.pi/agent/npm/node_modules`
   link, added before run 2; the `.ts` file it names exists in 0.68.0's layout only.
6. **Gateway logs** stay in the session scratchpad; `gateway-denied.txt` summarizes them
   (`scripts/openshell_probe/gateway_denied.py`).

## Findings

1. **An explicit `-e <path>` is the carrier on both flavours** under the runner's
   `--no-extensions`: tools, blocks, notes and logs all work, and omp's `before_subagent_spawn`
   reaches it. A runner setting that uploads extension directories and appends `-e` per path
   needs no change to the runner's other flags.
2. **Skills:** Pi takes `--skill <dir>` under `--no-skills`; omp has no such flag and reads
   `$PI_CODING_AGENT_DIR/skills` and the repository's `.agents/skills` with `--skills=<globs>` once
   `--no-skills` is dropped (not `$HOME/.omp/agent/skills`).
3. **Agents:** omp reads `$HOME/.omp/agent/agents` only (HOME is `/tmp/pi-home` in the runner's
   omp command); Pi's pi-subagents reads `$PI_CODING_AGENT_DIR/agents`.
4. **omp's native `task` tool is on under the runner's flags**, with bundled agents; an
   isolated task runs in `$HOME/.omp/wt/<id>/m` on the agent file's model, and the model's
   `patterns` reach `before_subagent_spawn`. `--tools` with every other tool of the control run
   closes it (run 9).
5. **Pi subagents are blocked by a version gap**: the worker's pi-subagents 0.68.0 was built for
   pi 0.85.1 and cannot spawn on 1.1.0; 0.78.0 loads but registered no tool in print mode. Until
   a pi-subagents release is qualified on the image's pi, a Pi run has no subagent mechanism.
6. **The extension's own process is bound by the policy** (fetch EACCES/ConnectionRefused,
   `/usr` EACCES, state writes allowed), so an in-process hook cannot reach what a shell child
   cannot.
7. **HOME differs per flavour** (`/sandbox` for pi, `/tmp/pi-home` for omp), and every
   `~`-relative state the extensions and guards keep follows it.

## What this does not show

- Why pi-subagents 0.78.0 registered no tool in the print-mode session (its load is clean in a
  plain container); a pi-subagents release qualified on pi 1.1.0.
- omp's `--hook` flag (its `-e` was used), and omp's `--plugin-dir <path>` (listed under "Plugin
  Options" in `omp --help`) as an alternative to `omp plugin link` for the statiker-omp package.
- Any Linux gateway; one Mac, one image build, one model pair.
- A whole statiker run on omp (the `statiker` mode loads the operator's extension and lists its catalog;
  see run 7).
