# Agent OS

Agent OS (AOS) runs coding tasks in OpenShell sandboxes and returns a branch with
independently collected acceptance evidence. A task finishing, its checks passing,
and the operator accepting it are separate recorded events. Nothing merges without
an explicit operator action.

AOS is early software for one developer with one self-hosted, single-process bus.
This is release `v0.1.0`. The [validation record](docs/release-validation.md)
lists the checks run on this release and their qualifications, separately from
historical proof.

## How a task runs

1. Queue a brief and acceptance commands, or commit defaults in `aos-workflow.toml`.
2. Mark it ready. `aos run` resolves the repository's committed policy and workflow
   before claiming; missing or invalid prerequisites refuse the run.
3. The harness works on a job copy inside OpenShell, using a dedicated worker login.
   The runner checks the effective sandbox policy before releasing the job.
4. Acceptance runs again in a fresh, credential-free sandbox by default. Optional
   per-run PostgreSQL and browser evidence support application checks.
5. The result returns on `ao/<task>`, with the check outputs available to review.
   Accept, reject, or requeue it. `aos integrate` checks the merged candidate in a
   fresh sandbox before an operator merges it.

`aos dispatch` starts ready tasks within host/repository capacity limits. Cancellation,
timeouts and interrupted runners retain recorded results and cleanup state;
`reconcile` retries cleanup of owned OpenShell resources. Read the validation
qualifications rather than assuming every termination path has clean live proof.

## Start a local bus

Requires Python 3.11+. From this checkout:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
export BUS_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export BUS_DATA_DIR="$PWD/.aos/local-data"
export AOS_BIND=127.0.0.1
python -m agent_os serve
```

In another terminal, activate the environment and set `AOS_TOKEN` to that same
`BUS_TOKEN` value. `aos list` uses `http://127.0.0.1:8010` by default; set `AOS_URL`
explicitly for another bus. `.env` is not automatically loaded by the application.
Telegram, GitHub and Super Productivity are optional.

A bus alone does not run agents. The worker also needs Docker, OpenShell 0.1.3, a
built harness image, dedicated worker credentials and a committed v2
`aos-policy.toml` in each task repository. Follow [Getting started](docs/getting-started.md)
for those steps. [aos-example](https://github.com/happyhackerbird/aos-example) is a
small notes app with PostgreSQL and browser acceptance checks.

The bundled Compose service publishes only `127.0.0.1:8010`. Remote access and
sandbox-to-bus reachability are separate, explicit configuration choices; an
operator CLI reaching the bus does not prove a sandbox can reach it.

## Release scope

The release target is headless **Claude Code and Codex** on the OpenShell Docker
backend, with task operations, committed repository policies/workflows, independent
acceptance, evidence review, retry, merge checks and dispatch. Both `aos run` and
`aos integrate` refuse the retired host backend. Runnable task tiers are 0–2.
A support claim is gated on the exact-candidate checks in the validation record.

Linux with Ubuntu 24.04 is the deployment target. Apple Silicon macOS with Colima
is the development target and is outside OpenShell's vendor support matrix.
Docker Desktop and other Linux distributions are unqualified.

## Experimental

These features have narrower or historical evidence, not the supported release contract:

- Pi and omp harnesses, including finite interactive omp sessions and `aos attach`.
- Agent messaging, Telegram replies, automatic delivery and pull-only controls.
  `AOS_OPENSHELL_COMMS=pull` retains messaging tools but disables automatic delivery;
  `0` disables messaging. Enabled modes require authenticated sandbox-to-bus readiness.
- Operator-supplied plugins, skills, hooks and subagents.
- Before/after baseline acceptance, task families, candidate import/export and
  model-written interaction reviews.
- Branch pushing, GitHub PR delivery and merge automation initiated by an operator.
- Super Productivity's older single-file Dropbox sync format as a task source.

Worker-login renewal and the limitations of each harness are documented in the
[setup reference](docs/reference/openshell-backend-setup.md) and
[Pi/omp adapter](docs/pi-adapter.md).

## Boundaries

A sandbox can still damage its own job copy. Review and acceptance remain necessary.
File exclusions do not erase secrets already committed to Git history. The bus is
not multi-tenant, and its bearer tokens must not be exposed to an untrusted network.
See [SECURITY.md](SECURITY.md) for reporting and trust boundaries.

## Documentation

- [Getting started](docs/getting-started.md)
- [Architecture](docs/architecture.md)
- [CLI](docs/reference/cli.md), [configuration](docs/reference/configuration.md),
  [operations](docs/reference/operations.md)
- [OpenShell setup](docs/reference/openshell-backend-setup.md)
- [Validation and release gates](docs/release-validation.md)
- [Documentation index](docs/README.md)
- [Contributing and isolated checks](CONTRIBUTING.md)

## License

Apache-2.0 ([LICENSE](LICENSE)).
