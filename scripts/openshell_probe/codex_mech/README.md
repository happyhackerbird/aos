# Codex-mechanism kit: skills, hooks, subagents and worktrees inside an OpenShell sandbox

Probe tooling for [the Codex-mechanisms record](../../../docs/probes/2026-10-14-codex-mechanisms.md):
one real `codex exec --json` run per mode inside an OpenShell sandbox built from `aos-harness:comms`,
under the aos-example policy, with the probe's fixtures (three skills, a hooks file and its hook
script, a custom agent role, a plugin with a local marketplace) shipped into the sandbox. Each run
measures whether a mechanism a statiker run would use on Codex works there and whether the policy's
boundaries hold through it. Not the production adapter; `agent_os/` and `docker/` are untouched.
Built beside the [statiker-mechanism kit](../statiker_mech/README.md), whose helpers it copies.

## Files

- `driver.py prepare --base-tarball B --out J --plugins-out P`: unpacks the job tarball
  `run_job.py provision` made, adds `.agents/skills/repo-skill/SKILL.md` and commits it, repacks;
  builds the plugins tarball `codex-probe/` (`hook.py`, `hooks.json`, `agents/probe-mini.toml`,
  `skills/{user-skill,home-skill}/SKILL.md`, `plugin-probe/` with `.codex-plugin/plugin.json` and
  `skills/plugin-skill`, and `marketplace.json` naming it with a path relative to the marketplace
  root). Every skill carries a fresh code phrase, printed in the JSON summary; a run that reports
  the phrase read the file.
- `driver.py run --name cxm-<mode> --mode <mode> ...`: checks the connected local gateway
  (`https://localhost:17670`), lists sandboxes, creates the sandbox with the runner's call
  (`openshell_runner.py`: no provider for Codex, the two `DEFAULT_ENV` entries, `--cpu 2 --memory
  2Gi --detach --no-tty`), compares `policy get --full` with the rendering, uploads and extracts
  the plugins tarball to `/sandbox/plugins/codex-probe` and installs the fixtures where Codex
  reads them (`/sandbox/.agents/skills/user-skill`, `/sandbox/codex-home/skills/home-skill`,
  `/sandbox/codex-home/hooks.json`, `/sandbox/codex-home/agents/probe-mini.toml`,
  `/sandbox/.agents/plugins/marketplace.json` plus `codex plugin marketplace add` / `plugin add`
  / `plugin list`, output recorded), uploads the Codex `auth.json` from a 0600 private copy that is
  unlinked at once and `chmod 600`s it inside (the runner's form), uploads the prompt, releases the
  job, waits for `harness.rc`, in `mech` runs one more `codex exec --worktree` by exec (where the
  managed worktree lands), reads the rows' in-sandbox facts by exec (`exec-reads.txt`: the fire
  log, the subagents' files, the rollouts' `session_meta` and `turn_context` models, the
  `CODEX_HOME` tree, the auth digest, a counts-only scan for the Codex token values inside the
  sandbox), downloads `harness.out`, `harness.err`, `harness.rc` and `probe-report.md`, saves
  `openshell logs`, deletes the sandbox, lists sandboxes again and runs `checks.py`. The main
  script is `openshell_runner.main_script` line for line with the probe's command in the harness
  slot (asserted against the runner's own command before every run).
- `driver.py cleanup --name cxm-<mode>`: deletes one of this kit's sandboxes.
- `checks.py`: the per-row checks K1-K7 over the exec JSON stream, the exec reads and the report,
  writing `stream-events.txt` and `checks.txt`; the secret scan counts the Codex auth file's token
  values (and the worker's Claude token, in case a provider is attached) in every download and
  evidence file with a planted-string positive control, and never prints a value.
  `checks.py --self-test` runs the rows over a planted fixture and its negative twin.
- `fixtures/hook.py`: the hook command for every event (fire log at
  `~/.local/share/codex-probe/fires.jsonl`, the boundary read at SessionStart, the PreToolUse deny
  of `HOOK-DENY-ME`, a Stop block exactly once); `fixtures/hooks.json`; `fixtures/agents/probe-mini.toml`
  (`model = "gpt-5.6-terra"`); `fixtures/skills/SKILL.md.tmpl`.
- `prompts/mech.md` (= `control.md`): the K1-K5 steps and the final code word; `prompts/notrust.md`:
  the hooks steps only.

## Modes

| Mode | Codex command (on top of the runner's `harness_command("codex")`) |
|---|---|
| `mech` | `features.hooks/multi_agent/plugins=true`, `--dangerously-bypass-hook-trust`; after the harness one `codex exec --worktree -m gpt-5.6-terra` by exec |
| `control` | everything off: `features.hooks/multi_agent/plugins=false` and `agents.enabled=false`, fixtures present. The runner passed exactly that from b10d932 until subagents went back on by default; the kit now switches subagents off itself |
| `notrust` | `features.*=true` without the trust bypass; hooks steps only |
| `agents-off` | the control command: the record's runs 3 and 5 measured `agents.enabled=false` as an addition to the then runner's command, which left `spawn_agent` on |

## Command sequence (from the agent-os worktree; `$S` is the session scratchpad)

```sh
uv run python -m agent_os.openshell_policy ~/code/aos-example/aos-policy.toml --platform colima-mac \
    --bus-port 8015 --harness-host chatgpt.com:443 --harness-host auth.openai.com:443 \
    --harness-host ab.chatgpt.com:443 --out $S/cx/policy.yaml
uv run python scripts/openshell_probe/run_job.py provision --repo ~/code/aos-example --out $S/cx/job-base.tgz
uv run python scripts/openshell_probe/codex_mech/driver.py prepare --base-tarball $S/cx/job-base.tgz \
    --out $S/cx/job.tgz --plugins-out $S/cx/plugins.tgz > $S/cx/prepare.json
uv run python scripts/openshell_probe/codex_mech/driver.py run --name cxm-mech --mode mech \
    --image aos-harness:comms --policy $S/cx/policy.yaml --tarball $S/cx/job.tgz --plugins-tarball $S/cx/plugins.tgz \
    --prepare-summary $S/cx/prepare.json --codex-auth ~/.aos/worker/codex-home/auth.json \
    --state $S/cx/state --downloads $S/cx/downloads --evidence $S/cx/evidence/mech
# the same with --name cxm-control --mode control, --name cxm-notrust --mode notrust,
# --name cxm-agents-off --mode agents-off
uv run python scripts/openshell_probe/codex_mech/checks.py --mode mech --downloads $S/cx/downloads/cxm-mech \
    --evidence $S/cx/evidence/mech --codeword CODEWORD-<from run-summary.json> \
    --prepare-summary $S/cx/prepare.json --evidence-scan   # regenerate a run's checks
```

As run on 2026-10-14/15: mech three times (run 1's post-harness exec batch lost every read after a
python heredoc, fixed before run 2; run 3 adds the plugin install with a root-relative source
path), control, notrust and agents-off once each. Each run's checks were regenerated at the end
with the final predicates (`--evidence-scan`).

## Deviations from the runner's exact command

The feature flags and the trust bypass per the mode table; nothing else. The prompt is the
runner's positional `"$(cat /sandbox/prompt.txt)"`, stdin `/dev/null`, the harness timeout 2700 s
(the runner's default is 7200 s).
