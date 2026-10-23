# OpenShell probe tooling: policy renderer and prototype job runner

Historical probe tooling for the initial OpenShell qualification:
it renders the proposed v2 `aos-policy.toml` into an OpenShell sandbox policy and
drives one job through the `openshell` CLI so that the negative-boundary suite and
the demo run against an AOS-rendered policy instead of a hand-written one. It is
not the production adapter (MVP step 2); `agent_os/` is untouched.

## Command sequence

Everything runs from the agent-os checkout with its virtualenv Python on a host
whose `openshell status` is Connected (the server or the Colima Mac).

```sh
R=scripts/openshell_probe
.venv/bin/python $R/render_policy.py path/to/aos-policy.toml --platform linux --bus-port 8010 \
    --provider-host api.anthropic.com --out /tmp/job-policy.yaml --print-exclusions
.venv/bin/python $R/run_job.py provision --repo path/to/source-repo --exclude .env --exclude 'secrets/**' --out /tmp/job.tgz
.venv/bin/python $R/run_job.py run --name aos-job1 --image aos-harness:probe-app --policy /tmp/job-policy.yaml \
    --tarball /tmp/job.tgz --harness claude --prompt /tmp/prompt.txt --provider aos-claude-oauth
.venv/bin/python $R/run_job.py effective --name aos-job1 --expect /tmp/job-policy.yaml
.venv/bin/python $R/run_job.py accept --name aos-job1 --command 'bash verify.sh'
.venv/bin/python $R/run_job.py extract --name aos-job1 --base origin/HEAD --out /tmp/out.bundle
.venv/bin/python $R/run_job.py cancel --name aos-job1
```

`--platform colima-mac` switches the bus alias to `host.docker.internal`. (This probe tool predates 2026-10-14: the production renderer, `agent_os.openshell_policy`, names `192.168.5.2` on colima-mac, because `host.docker.internal` does not resolve inside a workload; see [the setup record](../../docs/reference/openshell-backend-setup.md).) Pi needs
`--model anthropic/claude-haiku-4-5`; Codex needs `--codex-auth path/to/auth.json`.

## Measured facts the tooling encodes

All from the [probe record](../../docs/probes/openshell-2026-10-11.md):

- `binaries: [{path: "/**"}]` is accepted and matches every executable.
- Every network entry needs a port; wildcards need three labels; a leading `*.` is
  the only wildcard form.
- No rule may name the gateway port (17670) or the host alias besides the bus rule;
  the bus alias is `host.openshell.internal` on Linux and `host.docker.internal` on
  the Colima Mac.
- A sandbox policy must not carry an L4 rule for a host a provider credential binds
  to (`--provider-host`); the provider rule supplies it.
- `landlock.compatibility: hard_requirement`, a numeric non-root identity,
  `include_workdir: true` and the read-only baseline are the proven filesystem shape.
- `sandbox upload` fails above roughly 1500 to 2000 files, so the job copy travels as
  one tarball and is unpacked inside; the upload goes to `/sandbox/incoming/` and an
  atomic `mv` triggers the run.
- Harnesses run as the sandbox's main process with stdin closed and write their
  output, stderr and exit code to files under `/sandbox`.
- Acceptance runs as fresh `sandbox exec` calls; results leave as a `git bundle`
  under the working directory via `sandbox download`.
- The effective policy is read back with `policy get --full`; only `_provider_*`
  rules may appear beyond the rendered ones.

## Measured behaviours this tooling encodes (2026-10-11 bar runs)

- `provision` refuses an `exclude` entry that matches a tracked path: a tracked secret stays readable through `git show`, and a sanitized copy makes `git add -A` record its deletion (Pi did exactly that on the Mac).
- `run` stages every upload from a scratch directory outside any git work tree, because `openshell sandbox upload` turns a file inside a git tree into a gitignore-filtered tree upload.
- `wait_for_file` uses `test -e`; `/sandbox/incoming` is a directory.
- The main script sweeps every process of the workload uid (except PID 1 and itself) after the harness exits and before writing `harness.rc`, so a fresh acceptance exec starts from a clean process table. Agents kill `verify.sh` mid-run and leave Postgres and the app alive otherwise.
- Under OpenShell a loopback listen entry can outlive its process while rebinding still works; verification scripts must not probe ports with `connect()` to decide whether a server is stale.
- The repo policy cannot carry harness API hosts (Codex needs `chatgpt.com`, `auth.openai.com`, `ab.chatgpt.com`); the bar runs used an operator overlay TOML. The production adapter needs an operator-level harness-hosts setting.
