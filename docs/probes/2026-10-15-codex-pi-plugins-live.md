# Codex and Pi plugins on the OpenShell route: live runs (2026-10-15)

Proof for `AOS_OPENSHELL_CODEX_PLUGINS` (129b719) and `AOS_OPENSHELL_PI_PLUGINS` (a0dd72a), with the
closed defaults they open from: Codex `agents.enabled=false` (b10d932) and omp's `--tools` allowlist
(eefbee8). The sibling of [Claude plugins on the OpenShell route](2026-10-14-openshell-plugins-live.md);
the mechanisms come from [the Codex probe](2026-10-14-codex-mechanisms.md) and
[the Pi probe](2026-10-14-pi-mechanisms.md). Seven `aos run --backend openshell` runs on a scratch
clone of `~/code/aos-example`: two passes of one run per harness (Codex, pi, omp), and one omp run
(AO-7) for a question the fresh-context review raised. Evidence:
[`2026-10-15-codex-pi-plugins-live/`](2026-10-15-codex-pi-plugins-live/).

## Setup

| Item | Value |
|---|---|
| Runner | `.venv/bin/aos run --backend openshell <id>` from the agent-os worktree at a0dd72a (branch `codex-pi-plugins`), launched detached and outside the session's command sandbox under `env -i` (`run-live.sh`, `run-live2.sh`) |
| Bus | disposable, `127.0.0.1:8016`, data dir and 0600 token in the session scratchpad; stopped after each pass |
| Repository | a scratch clone of `~/code/aos-example` at `b75f6a6` (`AOS_REPO_ROOT` in the scratchpad), so the `ao/AO-n` branches never reached the operator's checkout; `AOS_PUSH` off |
| Image, platform | `aos-harness:comms`, `colima-mac`, gateway `https://localhost:17670` |
| Credentials | Codex: `~/.aos/worker/codex-home/auth.json`; pi and omp: `~/.aos/worker/pi-auth.json` (the release matrix's harness table) |
| Codex directory | `fixtures/codex-probe/`: the Codex probe's `hooks.json`, `hook.py` and `agents/probe-mini.toml` (`model = "gpt-5.6-terra"`), plus `skills/proof-skill/SKILL.md` with a fresh phrase (`phrases.json`) |
| Pi directory | `fixtures/pi-probe/`: the Pi probe's extension as `index.ts`, `skills/proof-skill/SKILL.md` with a fresh phrase, `agents/probe-agent.md` (`model: openai-codex/gpt-5.6-terra`) |
| Cross settings | the Codex run also had `AOS_OPENSHELL_PI_PLUGINS` set and the pi run `AOS_OPENSHELL_CODEX_PLUGINS`: each run read only its own |
| Tasks | measurement briefs (`briefs/`), acceptance `grep` commands on `PROOF.md` that name each phrase by its prefix only, so the prompt never carries the phrase |

## Pass 1 (AO-1 to AO-3): failed, no commits

All three ended `failed: no commits` (`tasks/AO-1.json` to `AO-3.json`), and none from a mechanism.

- Codex (AO-1) reached the skill (its phrase was the one line written) and the Stop hook
  (`STOP-HOOK-SEEN` heads the final message). The rest of the recording was refused by the
  probe's own hook. That hook denies every tool call whose input mentions `HOOK-DENY-ME`, and the
  brief asked the model to write the block text and the hook log, both of which carry the marker.
- pi (AO-2) and omp (AO-3) wrote `PROOF.md` and reported every acceptance check passing inside the
  sandbox (3 of 3, and 4 of 4 with the omp spawn line).
- All three commits failed with `fatal: unable to auto-detect email address` (the pi final message
  quotes it), and the brief's "do not retry" kept each model from setting an identity itself.
  The job copy is a fresh `git init` (lane V; before it a `git clone`), neither carries a
  `user.name`/`user.email`, and the image has no system or global git config (`docker run` of the
  image: `git config --system/--global --list` empty, no `/etc/gitconfig`). See Findings.

## Pass 2 (AO-4 to AO-6): review / pass on every harness

The briefs (`briefs/codex2.md`, `pi2.md`) give each recording step as an exact shell command, write
the marker as `HOOK_DENY_ME`/`EXT_DENY_ME` and copy the fire logs through
`sed 's/HOOK-DENY-M[E]/.../'` (the command text never holds the marker), and commit with
`git -c user.name=aos-proof -c user.email=aos-proof@localhost`.

| Run | Harness | Harness time | Result | Acceptance (fresh sandbox) | Notes line |
|---|---|---|---|---|---|
| AO-4 | Codex | 56 s | review / pass, `3bb7ac5` on `ao/AO-4` | 3/3 | `plugins: codex-probe [skills: proof-skill; hooks.json; agents: probe-mini.toml]` |
| AO-5 | pi 1.1.0 | 41 s | review / pass, `5a9c568` on `ao/AO-5` | 3/3 | `plugins: pi-probe [extension: index.ts; skills: proof-skill; agents: probe-agent.md]` and `plugins: agents not loaded on pi (...): pi-probe/agents/probe-agent.md` |
| AO-6 | omp 18.8.3 | 81 s | review / pass, `66cd190` on `ao/AO-6` | 4/4 | `plugins: pi-probe [extension: index.ts; skills: proof-skill; agents: probe-agent.md]` |

Each result commit holds `PROOF.md` alone, authored `aos-proof` (`proof/AO-4.txt` to `AO-6.txt`).
The pi and omp runs report `has every required flag`, so `--skill` (pi) and `--skills` (omp) passed
the capability check against the image's own `--help`.

**Codex (AO-4).** Everything below comes from `proof/AO-4.txt` and the run's report:

- Skill: `Code phrase: PHRASE-CODEX-29dd08`, the fresh phrase, read from `$CODEX_HOME/skills`.
- Hooks: the fire log holds SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, SubagentStart,
  SubagentStop and the PreToolUse `decision: deny` on the `echo` of the marker. The block reads
  `Command blocked by PreToolUse hook: [codex-probe] denied by the probe hook (HOOK_DENY_ME)`, and
  `STOP-HOOK-SEEN` heads the final message (the Stop block reached the model).
- Hooks fired with `features.plugins=false`, the runner's setting. The probe's runs all had it
  `true`, so this is new.
- Subagents: `collaborationspawn_agent` was called, and SubagentStart fired with `agent_type=probe-mini
  model=gpt-5.6-terra`, the role file's model; the desk ran on `gpt-6.1-sol`. The child's own
  PreToolUse/PostToolUse carry `gpt-5.6-terra`. Its curl ended `curl: (7) Failed to connect to
  example.com port 443`.
- Boundary from the hook's own process (SessionStart): `home /sandbox`, `token_entries 0`, curl 7,
  `/usr` write `errno 13`, state write ok under `/sandbox/.local/state`.
- The two `error` items the trust bypass emits did not stop the run, and the final message was
  read (the report's `codex final message`).

**pi (AO-5).**

- Extension: the custom tool answered `PROBE-ECHO-OK hello-from-the-desk`. The fire log shows
  `session_start` with the boundary (`home /sandbox`, `argv0 /usr/local/bin/node`, `token_entries 0`,
  fetch `EACCES`), one `probe_echo`, and a bash `tool_call` with `decision: block`. The kept
  session events hold `denied by the probe extension` 5 times.
- Skill: `Code phrase: PHRASE-PI-690d0f` through `--skill`.
- Subagents: `NO-SUBAGENT-TOOL: functions.read functions.bash functions.edit functions.write
  functions.probe_echo multi_tool_use.parallel`. The agent file shipped and was not loaded, as the
  notes line says.

**omp (AO-6).**

- Extension: `PROBE-ECHO-OK hello-from-the-desk` (a `probe_echo` tool call in the fire log). The
  block reads `[pi-probe] denied by the probe extension (EXT_DENY_ME)`, and `session_start` shows
  `home /tmp/pi-home`, `argv0 bun`, `token_entries 0` and fetch `ConnectionRefused`.
- Skill: `Code phrase: PHRASE-PI-690d0f`, copied into `$PI_CODING_AGENT_DIR/skills` and selected by
  `--skills=proof-skill`.
- Subagents: a `task` call (the allowlist was left out because the directory ships a role), then
  `before_subagent_spawn` with `agent: probe-agent`, `patterns: ["openai-codex/gpt-5.6-terra"]`,
  `spawnKey: ProofChild`. A second `session_start` shows the child loading the extension. The child
  answered `{"exit_code":7, ..., "model_id":"openai-codex/gpt-5.6-terra"}`. The role file was found
  in `/tmp/pi-home/.omp/agent/agents`, where the runner copied it; the Pi probe's run 4 showed omp
  reads roles nowhere else.

## AO-7: omp's `--tools` allowlist with an extension loaded

The fresh-context review of 1357f6d asked whether the allowlist that closes `task` (eefbee8) also
hides tools an extension registers. Run 9 of the Pi probe had no extension, and AO-6 shipped a role,
so it ran without the allowlist. AO-7 (`run-live3.sh`, runner at 1357f6d, the same pass-2 brief)
ships `fixtures/pi-ext/`: the extension and the skill, no agents. So the command carries
`--tools=read,bash,edit,eval,glob,grep,wait,todo,write,web_search`, `--skills=proof-skill` and
`-e /sandbox/plugins/pi-ext/index.ts`. Result: review / pass, `47b1069` on `ao/AO-7`, acceptance
4/4 in a fresh sandbox (`tasks/AO-7.json`, `proof/AO-7.txt`).

- `## tool` reads `PROBE-ECHO-OK hello-from-the-desk`, and the fire log holds a `tool_call` of
  `probe_echo`. The allowlist does not hide an extension's tool; omp offers it as a device, as in the
  Pi probe's run 6.
- `## subagent` reads `NO-SUBAGENT-TOOL: read, bash, eval, glob, grep, wait, todo, write, web_search,
  edit, multi_tool_use.parallel`. With an extension loaded, `task` stays closed.
- The skill phrase, the block (`[pi-probe] denied by the probe extension (EXT_DENY_ME)`) and the
  `session_start` boundary are as in AO-6.

Sandboxes: `openshell sandbox list` read `No sandboxes found.` before and after each pass
(`logs/sandbox-list-*.txt`). The bus token and the worker secrets appear in no evidence file (a
fixed-string scan for the token, and a pattern scan for key and JWT shapes and `claim_token`
values, before copying).

## Findings

1. **Every part of both settings works through `aos run`.** Codex skills, hooks (all events, a deny, a
   Stop block) and a role on its own model; pi extension and skill; omp extension, skill and a
   role through `task`. The boundary holds in each harness's hook or extension process and in the
   subagents.
2. **Codex hooks fire with `features.plugins=false`.** The runner needs only `features.hooks=true`
   and the trust bypass.
3. **The OpenShell job copy has no git identity.** Neither the runner (`provision_job` does a
   `git init`, before lane V a `git clone`) nor the image (`aos-harness:comms`: no system or global
   git config) sets `user.name`/`user.email`, so a harness commits only if the model sets an identity
   itself. Pass 1 lost all three results to this under a brief that forbade retries. This predates
   the plugin work and is outside its scope. A runner-side default identity, or a documented
   expectation in the prompt, is the operator's call (the 2026-10-14 ruling only says the "the operator"
   author is fine).

## What this does not show

- A whole statiker-class run on Codex or Pi. No statiker port loads through these settings:
  statiker-omp needs `omp plugin link` (Pi probe run 8), and there is no Codex port.
- The Linux gateway; any image but `aos-harness:comms`.
- The child sessions' own files for omp (the sandbox is deleted; the runner keeps the desk's
  scrubbed `session.jsonl` only). The child's model rests on the spawn patterns and its own answer.
- A Codex run with roles but no hooks (the `features.multi_agent=true -c agents.enabled=true` command
  without the bypass), and a pi directory without an extension. Both are covered by unit tests only.
- The setting on several worker processes at once, or with `AOS_OPENSHELL_FRESH_ACCEPTANCE=0`.
- The review fixes made after these runs ran in unit tests only: the symlink, unreadable and
  skill-name refusals, `-e` in the capability check, and the install line's new form (the copies
  grouped, their stderr kept for the report). The install line is also run in local bash by
  `test_plugin_install_line_behaviour`. The harness commands these runs used are unchanged.
