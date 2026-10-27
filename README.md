# Agent OS

Hand a coding task to Claude Code or Codex and walk away. Agent OS (AOS) runs the
agent unattended inside a sandbox that reaches only what your repository's committed
policy allows, reruns your acceptance checks itself in a second sandbox with no
credentials, and brings the result back as a branch with the evidence attached. You
read evidence instead of repeating the work. Nothing merges until you say so.

AOS is early software for one developer with one self-hosted, single-process bus.
This is release `v0.1.0`. The [validation record](docs/release-validation.md)
lists the checks run on this release and their qualifications, separately from
historical proof.

## Why

A coding agent saying "done" is one event. Its changes passing your checks is a
second. You accepting the result is a third. Most setups collapse all three into the
agent's own report. AOS keeps them apart and records each one, so for every result you
can answer: which task ran, where its changes live, which checks actually ran, and
what you accepted.

This is what review looks like. The lines below are copied from a run on the public
example repository (2026-10-13); today's `aos review` prints a few more, such as
PASS/FAIL totals and evidence flags:

```
run AO-1-r1: status=review
evidence dir: ~/.aos/evidence/AO-1-r1
acceptance: bash scripts/check.sh -> exit 0
result acceptance/20261013T061129Z/result.txt:
  PASS total count grew by one
  PASS new note is first in the list
  PASS new note is visible in the list
  PASS row inserted in the notes table
screenshot: acceptance/20261013T061129Z/01-start-page.png
screenshot: acceptance/20261013T061129Z/02-after-save.png
```

Those PASS lines come from AOS rerunning the checks in a fresh sandbox after the agent
was gone, against a throwaway PostgreSQL and a headless browser. The agent's own test
output is kept too, but the verdict does not rest on it. A change that only looks
right fails the same way:

```
run AO-2-r1: status=failed
acceptance: bash scripts/check.sh -> exit 1
result acceptance/20261013T061139Z/result.txt:
  PASS total count grew by one
  FAIL new note is first in the list
  FAIL new note is visible in the list
  PASS row inserted in the notes table
```

## How a task runs

1. You queue a task: a brief and the commands that prove it done. A repository can
   also commit default checks in `aos-workflow.toml`.
2. Mark it ready. `aos run` resolves the repository's committed policy and workflow
   before claiming; missing or invalid prerequisites refuse the run. A repository
   without a policy is refused before any work starts.
3. The harness works on a job copy of the repository's default branch inside
   OpenShell, using a dedicated worker login. Your own checkout is never touched.
   The committed `aos-policy.toml` decides which repository files are left out and
   which network hosts the agent can reach, in addition to the bus, per-run services
   and the operator's harness endpoints. The runner reads the effective policy back
   from the sandbox and refuses the run if it differs.
4. Acceptance runs again in a fresh, credential-free sandbox by default, optionally
   with a throwaway PostgreSQL and a headless browser for application checks. Test
   output and screenshots are copied out, and both sandboxes are deleted.
5. The result lands on branch `ao/<task>`. `aos review` shows the evidence. You
   accept, reject or requeue it; a requeued task continues from its branch, with the
   previous attempt's report in the prompt.
6. Before you merge, `aos integrate` checks the merged candidate in a fresh sandbox.

`aos dispatch` starts ready tasks within host/repository capacity limits. Cancellation,
timeouts and interrupted runners retain recorded results and cleanup state;
`reconcile` retries cleanup of owned OpenShell resources. Read the validation
qualifications rather than assuming every termination path has clean live proof.

## Try it

[aos-example](https://github.com/happyhackerbird/aos-example) is a small notes app
(Flask, PostgreSQL, pytest, a Playwright browser check) with one planted defect that
only the browser check catches. It is the fastest way to see what a run returns.

Requires Python 3.11+. Start a local bus from this checkout:

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

A bus alone does not run agents. On the worker host:

1. Install Docker and the OpenShell 0.1.3 gateway
   ([setup record](docs/reference/openshell-backend-setup.md)).
2. Build the harness image and point the runner at it:

   ```sh
   docker build -t aos-harness:mvp docker/harness
   export AOS_OPENSHELL_IMAGE=aos-harness:mvp
   ```

3. Mint one worker credential per harness you use, never your own login
   ([worker credentials](docs/reference/openshell-backend-setup.md#worker-credentials-never-the-operators-own-stores)).
4. Commit an `aos-policy.toml` to the repository's default branch:

   ```toml
   [execution]
   fail_closed = true

   [network]
   allow = ["pypi.org:443", "files.pythonhosted.org:443"]
   ```

5. Queue the task, mark it ready, run it, review it:

   ```sh
   aos add --repo aos-example --title 'Show new notes on the start page' \
     --brief-file brief.txt --accept 'bash scripts/check.sh'
   aos ready AO-1
   aos run AO-1
   aos review AO-1
   aos accept AO-1
   ```

[Getting started](docs/getting-started.md) walks through every step, including the
runner environment. The bundled Compose service publishes only `127.0.0.1:8010`.
Remote access and sandbox-to-bus reachability are separate, explicit configuration
choices; an operator CLI reaching the bus does not prove a sandbox can reach it.

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

- No automatic merge, and nothing merges without an explicit operator step.
- A sandbox can still damage its own job copy. Review and acceptance remain necessary.
- File exclusions do not erase secrets already committed to Git history.
- The bus is not multi-tenant, and its bearer tokens must not be exposed to an
  untrusted network. See [SECURITY.md](SECURITY.md) for reporting and trust boundaries.

## Documentation

- [Getting started](docs/getting-started.md): bus, sandbox setup and a first task.
- [Architecture](docs/architecture.md): components, lifecycle and boundaries.
- [CLI](docs/reference/cli.md), [configuration](docs/reference/configuration.md)
  and [operations](docs/reference/operations.md): current behavior.
- [OpenShell setup](docs/reference/openshell-backend-setup.md): gateway, image and
  worker credentials.
- [Pi adapter](docs/pi-adapter.md): Pi and omp flavours and their auth export.
- [Validation and release gates](docs/release-validation.md)
- [Documentation index](docs/README.md)
- [Contributing and isolated checks](CONTRIBUTING.md)

## License

Apache-2.0 ([LICENSE](LICENSE)).

For development, install `python -m pip install -e '.[dev]'`, then run the suite in
its isolated container with `sh scripts/test-isolated.sh` (needs Docker). Do not run
the process-management tests directly on a Linux host; see
[Contributing](CONTRIBUTING.md).
