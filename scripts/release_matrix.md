# Release matrix driver

`scripts/release_matrix.py` runs the headless release matrix on one host:
per selected harness (claude, codex, pi, omp), the scenarios success, failed
verification, interruption and retry through OpenShell on a disposable bus.
It is probe tooling; records are evidence at their named revision, not a release approval.

```
uv run python scripts/release_matrix.py --host mac --fixture demo-app --harness claude \
    --work-dir ~/.aos/release-matrix/mac-claude-demo
uv run python scripts/release_matrix.py --host mac --harness codex --dry-run --work-dir /tmp/plan
```

## What it does

1. Checks prerequisites before starting anything and prints one `missing:` line per gap: the `aos`
   entry point, `docker`/`openshell`/`git`, the image (`docker image inspect`), the gateway
   (`openshell status` Connected), the Claude provider on the gateway, the Codex and Pi worker auth
   files (Pi auth must stay valid for the harness timeout plus 300 s), the fixture source.
2. Starts a bus with `uvicorn agent_os.api:create_app --factory` on `--bind`
   (default `127.0.0.1`) and `--port` (default 8012; 8010/8011 refused), with a
   random token in `<work>/bus-token`, isolated data, lease 120 s and sweep 15 s.
   A bus accepting that work directory's token is reused and left running.
3. Prepares the fixture repository once per work dir under `<work>/repos/<fixture>`:
   - `demo-app`: a copy of examples/demo-app, `git init -b main`, one commit. Positive brief TASK.md,
     acceptance the lines of acceptance.txt; the negative brief documents `GET /` in README.md and
     says the failing `bash verify.sh` is expected, so acceptance fails. (A brief that contradicts the
     prompt's "acceptance must pass" without a reason made Claude stop without committing.)
   - `aos-example`: a `--no-hardlinks` clone of ~/code/aos-example without `origin`, plus the
     untracked `.aos/services.toml` (`[postgres]`) and `.aos/evidence.toml` (`dir = "evidence"`).
     The negative brief documents the start-page route the same way and leaves notes/ alone.
4. Per cell: `aos add`, `aos ready`, then `aos run` with the harness environment (the `HARNESS_ENV`
   table at the top of the script; every inherited `AOS_*`/`BUS_*` variable and
   `NODE_EXTRA_CA_CERTS` are dropped first). The bus token reaches the CLI through the environment
   only. Polls the bus every 5 s and prints one timestamped line per state change.
5. Checks the outcome and sandbox names using the same bus-derived or explicit
   prefix as the runner, writes `<work>/matrix.json` after each cell, and writes
   the record at the end (`--record`, default `<work>/record.md`).

## Scenarios

| Scenario | What happens | Expected |
|---|---|---|
| `success` | positive brief | task `review`, verdict `pass`, result commit = tip of `ao/<id>` in the fixture repo |
| `failed-verification` | negative brief | task `failed`, verdict `fail` |
| `interruption` | positive brief; after 45 s of `running`, `aos cancel` | task `cancelled`, runner exits within 180 s |
| `retry` | `aos requeue <interruption task> --ready`, run again | task `review`, verdict `pass` on the second run |
| `runner-kill` (opt-in) | positive brief; after 45 s of `running`, SIGKILL of `aos run` | lease expiry blocks the task; `python -m agent_os.openshell_runner reconcile` deletes the sandboxes |

A bus cancel is terminal (`cancelled` has no outgoing transition in `agent_os/store.py`), so the
requeue of the cancelled task is refused. The driver records that refusal in the cell notes and then
runs the retry on a new task: interrupted by SIGTERM to `aos run` after 45 s of `running` (the runner
deletes its sandbox and posts `blocked`, "runner interrupted"), requeued with `aos requeue --ready`,
and run again to `review`/`pass`.

Every cell is judged once; a failure is recorded with its reason and the driver moves on. There are
no retries that hide failures.

## Options

- `--fixture demo-app aos-example`, `--harness claude codex pi omp`, `--scenario ...` select the
  matrix; `--only claude:success codex:retry` narrows it to cells.
- `--work-dir` is reusable: the bus data, token, fixture repository and `matrix.json` stay. A re-run
  replaces only the cells it runs, and the earlier attempts are kept under `superseded` and shown in
  the record as "(earlier attempt)".
- `--interrupt-after` (default 45 s of `running`) sets when cancel, SIGTERM or SIGKILL is applied.
- `--image` (default `aos-harness:mvp`), `--platform` (default from `--host`: `mac` -> `colima-mac`,
  `server` -> `linux`), `--harness-timeout` (default 1800 s).
- `--bind` selects the disposable bus interface. On a Linux Docker bridge, use its
  actual host address (often `172.17.0.1`) and set `--env AOS_OPENSHELL_BUS_HOST=...`
  to the same reachable address. Never borrow the production bus or bot.
- `--include DIR...` merges other work dirs' `matrix.json` into the record; `--record-only` just
  re-renders it (one record for several per-harness work dirs).
- `--dry-run` prints the cells and the names (never values) of the environment variables.

## Work dir layout

`bus-token` (mode 600), `bus-data/`, `repos/<fixture>/` (with `ao/<task>` branches), `briefs/`,
`openshell-state/` (the runner's state dir), `tasks/<id>.json` (final task JSON with runs),
`logs/bus.log`, `logs/<id>-r<n>.log` (one per `aos run`), `logs/<id>-reconcile.log`, `matrix.json`,
`record.md`.
