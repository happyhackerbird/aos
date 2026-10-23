# P1b kit: `aos-agent` inside a real OpenShell sandbox

Probe tooling for [the P1b record](../../../docs/probes/2026-10-14-agent-comms-p1b.md): one
real `claude -p` run per mode inside an OpenShell sandbox built from `aos-harness:comms`, with
the agent-comms wiring of the U3 directive (S2 run file, S3 MCP configuration, S4 settings, S5
command flags, S6 prompt paragraph) written by hand, against a disposable bus, with the operator
side scripted. Not the production adapter; `agent_os/` and `docker/` are untouched.

## Files

- `driver.py`: the whole run. Imports the runner's own building blocks
  (`harness_command`, `deadline_lines`, `wait_job_lines`, `SWEEP`, `build_prompt`,
  `CLAUDE_SETTINGS`, `compare_effective` from `agent_os.openshell_runner`) and composes the
  S2-S6 files on top. Creates, readies, claims and starts a task on the bus; creates the
  sandbox with the runner's `openshell sandbox create` call; runs the Q1 and Q2 checks by
  `sandbox exec`; uploads the run file (0600, from a private local copy unlinked at once), the
  MCP configuration, the settings and the prompt; releases the job tarball; plays the operator
  (answers the model's `aos_ask` after sending a first code word, sends a second code word once
  the first is acknowledged); waits for `/sandbox/harness.rc`; runs the Q6 scan inside the
  sandbox (the token is read from `/sandbox/aos-run.json` there and never travels on a command
  line; counts only); downloads the stream, stderr and rc, reads the ledger with an exec
  (`sandbox download` of `/tmp/aos-agent/<run>.jsonl` came back empty without an error on
  2026-10-14, so the three recorded runs have no ledger copy); saves the gateway log
  (`openshell logs`); scans the downloads; deletes the sandbox. `--mode control` writes the
  same files with `"disableAllHooks": true`. `--bus-host` is the host written into the run
  file: the rendered alias by default, or the IPv4 literal a policy rule names (what the Mac
  needs; see the record's Q2).
- `excerpt.py`: turns a downloaded `harness.out` (stream-json) into an ordered event list
  (`stream-events.txt`), an init/result summary and a copy of the ledger.
- `driver.py sandbox-up --name --image --policy` and `driver.py probe --name --label --url ...
  [--lookup NAME] [--port N] [--evidence DIR]` (added for
  [P1c](../../../docs/probes/2026-10-14-agent-comms-p1c.md)): a sleeping sandbox with no
  provider and no job, and `getent` plus `curl` from inside it with the gateway's lines naming
  the probed ports, for reach questions that need no Claude run. `cleanup --name` deletes it.
- `driver.py reach --name --label --bus --bus-port --token-file --base-commit --state
  [--bus-host HOST] [--platform colima-mac|linux] [--evidence DIR]` (added for the endpoint
  lane, 2026-10-14): in a sandbox from `sandbox-up`, a task claimed and started on the bus,
  one operator note with a code word posted to it, the run file written with the host the
  runner would use (`agent_os.openshell_policy.bus_host`, unless `--bus-host`) and uploaded
  0600, then `aos-agent`'s own `aos_inbox` call through the rendered `aos-bus` rule. It prints
  (and writes to `<evidence>/<label>.txt`) the tool answer with the token redacted, whether the
  code word came back, the note's state on the bus afterwards, and the gateway's lines naming
  the port. Exit 0 when the inbox call answered without an error.
- `driver.py ledger --bus --token-file [--cut N] TASK...` and `driver.py token-scan --bus
  --token-file --bus-db --control WORD [--git-repo CLONE] [--dir DIR]... [--file F]... TASK...`
  (added for [U4](../../../docs/probes/2026-10-14-agent-comms-u4.md), 2026-10-14): the first
  prints each task's runs and its intent ledger as the operator sees them through the bearer
  (bodies cut, the chat id as a flag only); the second reads every run's claim token from the
  bus database in memory and counts it, next to a control string, in the bus rows, the
  clone's `ao/<task>` history and object store, the runner state directory and the runner
  logs. Exit 1 on any token hit. Neither prints a token.
- `driver.py runs --bus --token-file [--cut N] TASK...` and `driver.py state-dir --dir --prefix
  [--grep TEXT]` (added for [lane Q](../../../docs/probes/2026-10-15-agent-comms-laneq.md),
  2026-10-15): the first prints each task's runs as the bus holds them (status, verdict, stage,
  branch, result commit, error, and the long fields `report`, `acceptance_output` and
  `cleanup_report` whole or cut); the second lists the runner state directory after the runs:
  each entry, whether it carries the bus prefix, whether an owner marker (`owner.json`) is left
  (a clean cleanup unlinks it and keeps the directory), its files, and the `prompt.txt` lines
  containing `--grep`. Exit 1 when an entry lacks the prefix or a marker is left.
- `driver.py stopblock --bus --token-file --repo --image --state --evidence [--delay 30] [--poll 2]`
  (added for lane Q's G2 measurement, 2026-10-15): Part 2's Stop block built from the runner's own
  pieces (settings, default-branch policy through `render_for`, `provision_job`, `main_script` with
  comms, `claude_settings`, `build_prompt`, the run file, the runner's create call and upload order),
  the operator note `--delay` s after the run start, the ledger polled and each line stamped when first
  seen, in-job acceptance, then the gateway log read before the sandbox is deleted, every DENIED line
  printed with its binary and destination. No result is posted; the token is redacted everywhere.

## Command sequence (as run on 2026-10-14, from the agent-os worktree)

```sh
S=<scratch>
uv run python -m agent_os.openshell_policy ~/code/aos-example/aos-policy.toml --platform colima-mac \
    --bus-port 8012 --provider-host api.anthropic.com --out $S/policy.yaml
uv run python scripts/openshell_probe/run_job.py provision --repo ~/code/aos-example --out $S/job.tgz
docker build -t aos-harness:comms docker/harness
BUS_TOKEN=... BUS_DATA_DIR=$S/bus8012-data uv run python -c \
    "import uvicorn; from agent_os.api import create_app; from agent_os.config import Settings; \
     uvicorn.run(create_app(Settings()), host='0.0.0.0', port=8012)" &
uv run python scripts/openshell_probe/comms_p1b/driver.py run --name p1b-main --mode main \
    --bus http://127.0.0.1:8012 --bus-port 8012 --token-file $S/bus.token --image aos-harness:comms \
    --policy $S/policy.yaml --tarball $S/job.tgz --base-commit <aos-example sha> \
    --state $S/state --downloads $S/downloads --evidence $S/evidence/main
uv run python scripts/openshell_probe/comms_p1b/excerpt.py $S/downloads/p1b-main $S/evidence/main
# the same with --name p1b-control --mode control
```

The three recorded runs rendered the bus rule by hand (run 1 the alias, which gave no reach;
runs 2 and 3 the IPv4 literal through a `services` endpoint). Since the endpoint lane
(2026-10-14) `render` names `192.168.5.2` on colima-mac itself, so a policy from
`python -m agent_os.openshell_policy ... --platform colima-mac` reaches the bus as is, and the
bus may listen on `127.0.0.1` (`AOS_BIND=127.0.0.1 AOS_PORT=8012 python -m agent_os serve`):
Lima's forwarding delivers the sandbox's connection to the Mac loopback (P1c). The bearer
stays in a 0600 file.

## Deviations from the runner's exact command

`--output-format json` is replaced by `--output-format stream-json --verbose
--include-hook-events` so that hook firing after an MCP call versus after a Bash call is
observable in the stream, and `--max-budget-usd 2` is appended (the probe's cost cap). The
S5 flags and everything else are the runner's.
