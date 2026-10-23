# Statiker-mechanism kit: statiker and dispatch-guards inside an OpenShell sandbox

Probe tooling for [the statiker-mechanisms record](../../../docs/probes/2026-10-14-statiker-mechanisms.md):
one real `claude -p` run per mode inside an OpenShell sandbox built from `aos-harness:comms`,
under the aos-example policy, with the statiker plugin (0.2.104) and the dispatch-guards plugin
shipped into the sandbox. Each run measures whether a mechanism a statiker run uses on Claude
Code works there (the Skill tool, statiker's record and git scripts, `git worktree add`, a
subagent with an explicit model, the plugins' hooks, `codex exec`) and whether the policy's
boundaries still hold through it. Not the production adapter; `agent_os/` and `docker/` are
untouched. Built beside the [P1b kit](../comms_p1b/README.md), whose helpers it copies.

## Files

- `driver.py prepare --base-tarball B --out J --plugins-out P [--project-skill]`: unpacks the job
  tarball `run_job.py provision` made, adds `.clippy/runs/2026-10-05-repo-policy-step1d.md` (a
  byte copy of agent-os's closed record, the fixture for `lint`, `closure` and `filter`) and
  `.clippy/runs/2026-10-14-probe-statiker.md` (a tracker the Stop guard's parser classifies as
  firing: `Status: in-progress`, no `Mode:` line, last A-line `[BIT]`, no `[PENDING]`; the
  comment above `PROBE_TRACKER_TEXT` cites the guard's lines), with `--project-skill` also
  `.claude/skills/statiker/SKILL.md`, commits them in the job repo (the record tools read
  committed state: `statiker_record.py filter` runs `git show <sha>:<tracker>`), runs the Stop
  guard once on this host against the rebuilt tree, and repacks. It also builds the plugins
  tarball: `statiker/` = the installed plugin directory whole, `dispatch-guards/` =
  `~/code/dispatch-guards/plugin` without `__pycache__`.
- `driver.py run --name stkm-<mode> --mode <mode> ...`: checks that `openshell status` shows the
  connected local gateway (`https://localhost:17670`), lists sandboxes, creates the sandbox with
  the runner's call (`openshell_runner.py` 1228-1232: provider `aos-claude-oauth`, the two
  `DEFAULT_ENV` entries, `--cpu 2 --memory 2Gi --detach --no-tty`), compares `policy get --full`
  with the rendering (`compare_effective`, legacy mode), uploads and extracts the plugins
  tarball to `/sandbox/plugins`, uploads the Codex `auth.json` from a 0600 private copy that is
  unlinked at once and `chmod 600`s it inside (the runner's 1274-1280 form), uploads the
  settings and the prompt, releases the job, waits for `harness.rc`, reads the rows' in-sandbox
  facts by `sandbox exec` (`exec-reads.txt`, including a counts-only scan for the Codex token
  values inside the sandbox), downloads `harness.out`, `harness.err`, `harness.rc` and
  `probe-report.md`, saves `openshell logs`, deletes the sandbox, lists sandboxes again, and runs
  `checks.py`. The main script is `openshell_runner.main_script` line for line with the probe's
  command in the harness slot (the driver asserts the identity with the runner's own command
  before every run).
- `driver.py cleanup --name stkm-<mode>`: deletes one of this kit's sandboxes.
- `checks.py`: the per-row checks (R1-R7 for `plugin`, `settings-hooks` and `control`; S1-S4 for
  `stopguard`) over the stream, the exec reads and the downloads, writing `stream-events.txt` and
  `checks.txt`; the secret scan counts the worker's Claude token and the Codex auth file's token
  values in every download and evidence file, with a planted-string positive control in the same
  invocation, and never prints a value. `checks.py --self-test [--scratch DIR]` runs the checks
  over a planted stream fixture (a Skill load, a hook deny, a planted token) and its negative
  twin.
- `prompts/plugin.md`: the desk prompt for `plugin` and `settings-hooks` (`@CODEWORD@` replaced
  by a fresh `CODEWORD-<6 hex>` per run); `prompts/control.md`: the same prompt with one more
  Skill call in R1 (`verify`, an existing non-plugin skill), because the plugin prompt's three
  Skill calls only ever get "Unknown skill" without `--plugin-dir` and never reach the Skill
  permission (the record's run 3); `prompts/stopguard.md`: the one-step task for `stopguard`.
- `checks.py --evidence-scan`: after the checks, scan the evidence directory too (what the driver
  does at the end of a run), so a run's `checks.txt` can be regenerated from its downloads with
  the current predicates.

## Modes

| Mode | Settings (on top of the runner's `CLAUDE_SETTINGS`) | Claude flags added |
|---|---|---|
| `plugin` | `Skill(*)` allowed, `disableAllHooks: false`, no `hooks` key | `--plugin-dir /sandbox/plugins/statiker --plugin-dir /sandbox/plugins/dispatch-guards` |
| `settings-hooks` (fallback) | `Skill(*)`, hooks on, `hooks` = dispatch-guards' `hooks.json` with `${CLAUDE_PLUGIN_ROOT}` spelled out; job carries `.claude/skills/statiker/SKILL.md` | none |
| `control` | unchanged (`disableAllHooks: true`, no Skill) | none |
| `stopguard` | as `plugin`, plus a `Stop` hook `python3 /sandbox/plugins/statiker/hooks/statiker_stop_guard.py` (timeout 10) | the two `--plugin-dir` flags; `--max-turns 12 --max-budget-usd 3` |

## Command sequence (from the agent-os worktree; `$S` is the session scratchpad)

```sh
uv run python -m agent_os.openshell_policy ~/code/aos-example/aos-policy.toml --platform colima-mac \
    --bus-port 8013 --provider-host api.anthropic.com --harness-host chatgpt.com:443 \
    --harness-host auth.openai.com:443 --harness-host ab.chatgpt.com:443 --out $S/policy.yaml
uv run python scripts/openshell_probe/run_job.py provision --repo ~/code/aos-example --out $S/job-base.tgz
uv run python scripts/openshell_probe/statiker_mech/driver.py prepare --base-tarball $S/job-base.tgz \
    --out $S/job.tgz --plugins-out $S/plugins.tgz
uv run python scripts/openshell_probe/statiker_mech/driver.py run --name stkm-plugin --mode plugin \
    --image aos-harness:comms --policy $S/policy.yaml --tarball $S/job.tgz --plugins-tarball $S/plugins.tgz \
    --codex-auth ~/.aos/worker/codex-home/auth.json --state $S/state --downloads $S/downloads \
    --evidence $S/evidence/plugin
# the same with --name stkm-control --mode control, and
# --name stkm-stopguard --mode stopguard --max-turns 12 --budget-usd 3;
# settings-hooks only when plugin does not expose the skill or the hooks: prepare --project-skill
# into a second tarball, then --name stkm-settings-hooks --mode settings-hooks.
uv run python scripts/openshell_probe/statiker_mech/checks.py --mode plugin \
    --downloads $S/downloads/stkm-plugin --evidence $S/evidence/plugin --codeword CODEWORD-<from run-summary.json> \
    --evidence-scan   # regenerate a run's checks with the current predicates
```

As run on 2026-10-14: plugin twice (the first run's exec reads looked under `/home/sandbox`; the
sandbox's `HOME` is `/sandbox`, fixed in the kit before the second), control twice (the second
with `prompts/control.md`), stopguard once; settings-hooks not needed. Each run's checks were
regenerated at the end with the final predicates (`--evidence-scan`).

## Deviations from the runner's exact command

`--output-format json` is replaced by `--output-format stream-json --verbose
--include-hook-events` (as in P1b), and `--max-budget-usd` and `--max-turns` are appended
(defaults 8 and 120). The `plugin` and `stopguard` modes add the two `--plugin-dir` flags after
`--settings`. Everything else in the command, the main script and the create call is the
runner's. The harness timeout defaults to 2700 s (the runner's default is 7200 s).
