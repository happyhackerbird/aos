# Pi-family mechanism kit: extensions, skills, subagents and worktrees inside an OpenShell sandbox

Probe tooling for [the Pi-mechanisms record](../../../docs/probes/2026-10-14-pi-mechanisms.md):
one real `pi --print --mode json` or `omp --print --mode json` run per flavour and mode inside an
OpenShell sandbox built from `aos-harness:comms`, under the aos-example policy, with the probe's
fixtures shipped in: an extension of its own (`fixtures/probe-ext/index.ts`, the harness's hooks
are an extension's event handlers), skills with fresh code phrases, agent definitions carrying a
model of their own, an omp isolation overlay, the operator's `pi-dispatch-guards` checkout, a Pi packages
tree holding `pi-subagents`, and her statiker fork for the omp `statiker` mode. Not the production
adapter; `agent_os/` and `docker/` are untouched. Built beside the
[codex_mech](../codex_mech/README.md) and [statiker_mech](../statiker_mech/README.md) kits.

## Files

- `driver.py prepare --base-tarball B --out J --plugins-out P [--pi-npm-out N [--pi-npm-src DIR]]`:
  unpacks the job tarball `run_job.py provision` made, adds `.agents/skills/repo-skill/SKILL.md`
  and commits it, repacks; builds the plugins tarball (`pi-probe/`: the extension, the skills
  `probe-skill` and `probe-skill-home`, the agent files for pi and omp, `overlay-isolation.yml`, a
  `dispatch-guards.json`; `pi-dispatch-guards/` from `~/code/pi-dispatch-guards`; `statiker/` from
  `~/plugins/statiker` without tests, docs and probes) and, with `--pi-npm-out`, the Pi packages
  tarball from `--pi-npm-src` (default the worker's `~/.pi/agent/npm/node_modules`, pi-subagents
  0.68.0 built for pi 0.85.1; the record's second run used a scratch
  `npm install --legacy-peer-deps pi-subagents@0.78.0 pi-intercom` tree).
- `driver.py run --name pim-<flavour>-<mode> --flavour pi|omp --mode mech|control|statiker ...`:
  checks the connected local gateway, lists sandboxes, creates the sandbox with the runner's call
  (no provider for a Pi run), compares `policy get --full` strictly as the runner does for Pi,
  uploads and extracts the plugins (and for pi mech the packages) tarball, installs the fixtures
  where the flavour reads them (pi: `/tmp/pi-agent/agents/`, `/sandbox/.pi/agent/dispatch-guards.json`,
  and `/sandbox/.pi/agent/npm/node_modules` linked to the packages tree because pi-dispatch-guards
  resolves pi-subagents at that fixed path; omp: `/tmp/pi-agent/{agents,skills}/` and
  `/tmp/pi-home/.omp/agent/{agents,skills}/`, one agent and one skill per location, to measure
  which omp reads), uploads the worker auth exactly as the runner does (`pi.selected_auth`, the
  nonrenewable snapshot, `auth.json` for pi or `agent.db` for omp, 0600), uploads the prompt,
  reads the flavour's version and `--help` flags, releases the job, waits for `harness.rc`, reads
  the rows' in-sandbox facts by exec (`exec-reads.txt`: the subagents' files, the fire logs, the
  state dirs, worktrees, the agent dir, omp's home, every session file's assistant models, the
  auth digest, a counts-only scan for the snapshot's secrets inside the sandbox), downloads
  `harness.out`, `harness.err`, `harness.rc` and `probe-report.md` (scrubbed of the worker secrets
  on the host before anything reads them), saves `openshell logs`, deletes the sandbox, lists
  sandboxes again and runs `checks.py`. The main script is `openshell_runner.main_script` line for
  line with the probe's command in the harness slot (asserted against the runner's own before
  every run); a Pi run reads its prompt from `/sandbox/prompt.txt` on stdin.
- `driver.py cleanup --name pim-<...>`: deletes one of this kit's sandboxes.
- `checks.py`: the per-row checks P1-P8 over the print-mode JSONL, the exec reads and the report,
  writing `stream-events.txt` and `checks.txt`; P8 applies the runner's own parser
  (`agent_os.pi.FLAVOURS[flavour].parser`), so the contract verdict is the runner's; the secret
  scan counts the selected worker secrets in every download and evidence file with a planted-string
  positive control, and never prints a value. `checks.py --self-test` runs the rows over a planted
  fixture and its negative twin.
- `fixtures/probe-ext/index.ts`: `session_start` (fire-log line plus the boundary read from the
  extension's own process: an HTTPS `fetch`, a `/usr` write, the token entries in `process.env`, a
  state write under `~/.local/state`), `tool_call` (blocks a bash command mentioning
  `EXT-DENY-ME`), `tool_result` (notes a result mentioning `EXT-NOTE-ME`), `before_subagent_spawn`
  (omp), and the custom tool `probe_echo`. No imports beyond node.
- `prompts/mech.md` with `prompts/subagent-pi.md` / `subagent-omp.md` spliced in at
  `@SUBAGENT_STEP@`; `prompts/control.md`.

## Modes

| Flavour, mode | Command (on top of the runner's `pi_invocation`) |
|---|---|
| pi `mech` | `-e /sandbox/plugins/pi-probe/index.ts -e /sandbox/plugins/pi-npm/node_modules/pi-subagents -e /sandbox/plugins/pi-dispatch-guards --skill /sandbox/plugins/pi-probe/skills/probe-skill` appended; `--no-extensions --no-skills` kept (explicit paths work under them) |
| pi `control` | unchanged |
| omp `mech` | `--no-skills` dropped; `-e /sandbox/plugins/pi-probe/index.ts --config /sandbox/plugins/pi-probe/overlay-isolation.yml --skills=probe-*,repo-*` appended |
| omp `control` | unchanged |
| omp `statiker` | omp mech plus `-e /sandbox/plugins/statiker/omp/extension.ts` |
| omp `tools-off` | the control command plus `--tools=read,bash,edit,eval,glob,grep,wait,todo,write,web_search` (the control run's tools minus `task`); graded as a control. The runner passed that list itself from 4aa9633 until omp's `task` went back on by default; it passes none again without comms, so every mode builds its run 5-9 command |

## Command sequence (from the agent-os worktree; `$S` is the session scratchpad)

```sh
uv run python -m agent_os.openshell_policy ~/code/aos-example/aos-policy.toml --platform colima-mac \
    --bus-port 8015 --harness-host chatgpt.com:443 --out $S/pi/policy.yaml
uv run python scripts/openshell_probe/run_job.py provision --repo ~/code/aos-example --out $S/pi/job-base.tgz
uv run python scripts/openshell_probe/pi_mech/driver.py prepare --base-tarball $S/pi/job-base.tgz \
    --out $S/pi/job.tgz --plugins-out $S/pi/plugins.tgz --pi-npm-out $S/pi/pi-npm.tgz > $S/pi/prepare.json
uv run python scripts/openshell_probe/pi_mech/driver.py run --name pim-pi-mech --flavour pi --mode mech \
    --image aos-harness:comms --policy $S/pi/policy.yaml --tarball $S/pi/job.tgz --plugins-tarball $S/pi/plugins.tgz \
    --pi-npm-tarball $S/pi/pi-npm.tgz --prepare-summary $S/pi/prepare.json \
    --state $S/pi/state --downloads $S/pi/downloads --evidence $S/pi/evidence/pi-mech
# the same with --flavour pi --mode control, --flavour omp --mode mech, --flavour omp --mode control,
# --flavour omp --mode statiker
uv run python scripts/openshell_probe/pi_mech/checks.py --flavour pi --mode mech --downloads $S/pi/downloads/pim-pi-mech \
    --evidence $S/pi/evidence/pi-mech --codeword CODEWORD-<from run-summary.json> \
    --session-id <from run-summary.json> --prepare-summary $S/pi/prepare.json --evidence-scan
```

## Deviations from the runner's exact command

The appended flags per the mode table (and omp mech's dropped `--no-skills`); nothing else. The
harness timeout is 2700 s (the runner's default is 7200 s).
