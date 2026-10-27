# Getting started

[Documentation index](README.md)

This guide starts a local bus, prepares an OpenShell sandbox host, and runs one
coding task through it. Every unattended run executes inside an OpenShell sandbox
under the repository's `aos-policy.toml`; there is no unsandboxed route.
Telegram, GitHub and Super Productivity are optional integrations described in
[configuration](reference/configuration.md) and [operations](reference/operations.md).

## Prerequisites

| Platform | Requirements |
|---|---|
| Linux (deployment target) | Ubuntu 24.04 with kernel 6.8 and `landlock` in `/sys/kernel/security/lsm`, Docker Engine 28 or newer (qualified: 29.5.3), about 3 GiB free RAM |
| macOS (development) | macOS 14.7 on Apple Silicon, Homebrew, Colima 0.9.0 or newer (qualified: 0.10.3 after `colima update`). Outside OpenShell's vendor support matrix; Docker Desktop is untested |
| Both | Python 3.11+, Git, OpenShell 0.1.3, a checkout of this repository |

The repository you want worked on must be a Git checkout with at least one commit,
stored under a common root directory (`AOS_REPO_ROOT`, default `~/code`), with its
directory name equal to the name you register with the bus.

## Install and start the bus

From the agent-os checkout:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
export BUS_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export BUS_DATA_DIR="$PWD/.aos/local-data"
export AOS_BIND=127.0.0.1
python -m agent_os serve
```

Keep that process running. This example binds **127.0.0.1:8010**. Without the
explicit `AOS_BIND`, the server defaults to `0.0.0.0`; do not expose it accidentally.
`AOS_PORT` selects another port. A Mac sandbox reaches the host bus at `192.168.5.2`
([setup record](reference/openshell-backend-setup.md)). Linux sandboxes need an
explicit reachable bind/published address; loopback access from the host is not proof
of sandbox access. `BUS_DATA_DIR` must be writable and defaults to `~/.aos/data`.
Export settings explicitly: the application does not load `.env` automatically.

In a second terminal, activate the same virtual environment and use the same token
as the running bus (do not generate a second one):

```sh
. .venv/bin/activate
export AOS_URL=http://127.0.0.1:8010
export AOS_TOKEN='<the BUS_TOKEN value from the first terminal>'
curl "$AOS_URL/health"
aos list
```

The health route needs no authentication; `aos list` checks authenticated access.
A fresh coding-only bus reports `ok: true` and monitors its internal `code.poll`
reconciliation worker. Already-merged task completion can recover without GitHub
or Telegram credentials. No upstream is required.
To check the bus from the host it runs on without a token, run
`curl http://127.0.0.1:8010/health`.

## Exercise task operations without a worker

```sh
brief_file="$(mktemp)"
printf '%s\n' 'Demonstrate task storage and readiness only; do not execute a worker.' > "$brief_file"
aos add --repo example-repo --title 'Bus smoke task' \
  --brief-file "$brief_file" --accept 'python -m pytest'
aos list
aos show AO-1
aos ready AO-1
aos show AO-1
aos cancel AO-1
rm "$brief_file"
```

Replace `AO-1` with the ID returned by `aos add`. Readiness requires a nonempty
brief and records operator authorization; it does not require explicit acceptance,
inspect a checkout or start a harness. Stop here for a bus-only installation.
A ready task remains idle until explicit worker dispatch.

Bus-only verification: a fresh non-editable `pip install` and the command above
were exercised outside the checkout with disposable HOME/data and a generated
token on macOS 14.7 arm64, Python 3.12.11. Health and authenticated CLI
create/list/show/ready passed. A separate asyncio-loop run with socket auditing
recorded zero outbound bus DNS/connect attempts and verified claim, heartbeat,
lease expiry, requeue and cancel. The resolved FastAPI 0.143.0, Uvicorn 0.54.0,
Pydantic 2.14.0 and HTTPX 0.28.1 are observations, not supported-version pins.
No server deployment, harness execution or sandboxed worker was exercised.

The CLI and direct runner default to `http://127.0.0.1:8010`; set `AOS_URL` for any other bus.

## Install the OpenShell gateway

Follow the [OpenShell setup record](reference/openshell-backend-setup.md) for your
platform. On Linux the gateway runs as the systemd user service
`openshell-gateway`; on the Mac it runs as a Homebrew service and needs the
`gateway.toml` shown there, pointing at Colima's Docker socket. `openshell status`
must report a connected, authenticated gateway before you continue. Restart the
gateway after any change to the Docker runtime.

## Build the harness image

The image at `docker/harness/Dockerfile` pins Claude Code, Codex, Pi, omp, uv with
Python 3.12, Playwright's Chromium, PostgreSQL 15 and Chromium on a digest-pinned
Node 24 base. Build it from the agent-os checkout on each worker host (on the Mac,
with Colima as the Docker context):

```sh
docker build -t aos-harness:mvp docker/harness
```

Images are local to the host; keep the gateway's `image_pull_policy` at
`if_not_present`.

## Mint worker credentials

Each harness gets one credential minted for the worker, never the operator's own
login or credential store. In short: Claude Code uses a `claude setup-token` held by
the OpenShell provider `aos-claude-oauth`; Codex uses a ChatGPT device-auth login
minted inside a sandbox, whose `auth.json` the runner uploads per run and downloads
again afterwards; Pi and omp use an exported `openai-codex` `auth.json`. The
commands are in [worker credentials](reference/openshell-backend-setup.md#worker-credentials-never-the-operators-own-stores);
the Pi export and its renewal are in the
[Pi adapter](pi-adapter.md#worker-auth-export-and-renewal). The Pi snapshot cannot
renew itself: re-export the worker login before it expires.

## Configure the runner

The runner reads these variables from the operator's environment, never from the
repository:

| Variable | Default | Meaning |
|---|---|---|
| `AOS_URL`, `AOS_TOKEN` | `http://127.0.0.1:8010`; token required | bus address and operator token |
| `AOS_HOST` | `mac` | `mac` or `server`; the host label tasks are registered for |
| `AOS_REPO_ROOT` | `~/code` | directory holding the source repositories |
| `AOS_OPENSHELL_PLATFORM` | required | `linux` or `colima-mac`; selects how the sandbox reaches the bus |
| `AOS_OPENSHELL_IMAGE` | required | the harness image, `aos-harness:mvp` |
| `AOS_OPENSHELL_PROVIDER_CLAUDE` | none | OpenShell provider for Claude Code, `aos-claude-oauth` |
| `AOS_OPENSHELL_PROVIDER_HOSTS` | `api.anthropic.com` | hosts a provider credential binds; a repository policy may not grant them |
| `AOS_OPENSHELL_CODEX_AUTH` | none, required for Codex | the worker Codex `auth.json` (mode 600) |
| `AOS_OPENSHELL_HARNESS_HOSTS_CODEX`, `_PI` | none | harness API hosts as a `host:port` list. Codex: `chatgpt.com:443,auth.openai.com:443,ab.chatgpt.com:443`. Pi: must include `chatgpt.com:443` |
| `AOS_OPENSHELL_PI_FLAVOUR` | `pi` | `pi` or `omp` |
| `AOS_OPENSHELL_PI_BIN` | the flavour name | binary in the image |
| `AOS_OPENSHELL_PI_PROVIDER` | `openai-codex` for `pi`; unset for `omp` | model provider |
| `AOS_OPENSHELL_CLAUDE_MODEL` | Claude Code's default | model for Claude runs, passed as `claude --model`, for example `claude-opus-5-5` |
| `AOS_OPENSHELL_PI_MODEL` | required for Pi | model name |
| `AOS_OPENSHELL_PI_AUTH` | required for Pi | the exported worker `auth.json` |
| `AOS_OPENSHELL_PI_ACCOUNT_ID` | none | optional pinned worker `accountId` |
| `AOS_OPENSHELL_CPU`, `AOS_OPENSHELL_MEMORY` | `2`, `2Gi` | sandbox resources |
| `AOS_OPENSHELL_STATE_DIR` | `~/.aos/openshell` | per-run state: rendered policy, prompt, ownership marker |
| `AOS_OPENSHELL_SANDBOX_PREFIX` | `a` + two hex digits derived from `AOS_URL`'s host and port | the start of every sandbox, state directory and per-run database name this runner creates; `reconcile` acts only on its own bus's markers. 2 or 3 lower-case letters or digits starting with a letter, never `aos` |
| `AOS_OPENSHELL_BUS_PORT` | the port of `AOS_URL` | bus port the sandbox policy allows |
| `AOS_OPENSHELL_BUS_HOST` | `192.168.5.2` on `colima-mac`, `host.openshell.internal` on `linux` | the host the sandbox's bus rule and the run file's bus URL name (an IPv4 literal or one of those two aliases); on a Linux host with a firewall use the bus's published address, e.g. `172.17.0.1` or its Tailscale binding |
| `AOS_HARNESS_TIMEOUT_SECONDS`, `AOS_ACCEPT_TIMEOUT_SECONDS` | `7200`, `900` | harness deadline and per-command acceptance timeout |
| `AOS_OPENSHELL_FRESH_ACCEPTANCE` | `1` | acceptance in a new, credential-free sandbox; `0` runs it in the harness's sandbox (Pi ignores `0`) |
| `AOS_OPENSHELL_BASELINE` | `0` | `1` runs the acceptance commands once BEFORE the agent, on the job tree in a fresh sandbox (`<name>-pre`), and the report compares each command's exit code before and after (fixed, regressed, still failing, unchanged); the verdict is unchanged; needs fresh acceptance; a per-run database is reset after the baseline |
| `AOS_OPENSHELL_COMMS` | `1` | Claude, Codex, omp and native pi runs get the aos messaging tools (ask another task or the operator, reply, wait, propose a task); on Linux set `AOS_OPENSHELL_BUS_HOST` to an address a sandbox reaches (the alias is dropped by a host firewall), or `0`; `pull` keeps the tools but disables automatic AOS delivery and the final-turn hold, preserving operator plugins |
| `AOS_GIT_NAME`, `AOS_GIT_EMAIL` | the host's git `user.name` and `user.email` for the repository | what an agent commits as (both or neither); with neither set anywhere, a run is refused before its claim |
| `AOS_OPENSHELL_CLAUDE_PLUGINS` | none | comma list of Claude plugin directories shipped into every Claude run (skills, hooks, subagents) |
| `AOS_OPENSHELL_CODEX_PLUGINS`, `AOS_OPENSHELL_PI_PLUGINS` | none | comma list of directories shipped into every Codex or Pi run: skills, a `hooks.json` (Codex), an extension (Pi) or agent roles; a `hooks.json` switches Codex hooks on, while subagents are on in every Codex and omp run, roles or not ([setup reference](reference/openshell-backend-setup.md)) |
| `AOS_BACKEND` | `openshell` | only backend for both run and integration checks; leave unset |

A Linux example for a Claude Code and Codex worker:

```sh
export AOS_URL=http://127.0.0.1:8010
export AOS_TOKEN='<the bus token>'
export AOS_HOST=server
export AOS_REPO_ROOT="$HOME/code"
export AOS_OPENSHELL_PLATFORM=linux
export AOS_OPENSHELL_IMAGE=aos-harness:mvp
export AOS_OPENSHELL_PROVIDER_CLAUDE=aos-claude-oauth
export AOS_OPENSHELL_CODEX_AUTH="$HOME/.aos/worker/codex-auth.json"
export AOS_OPENSHELL_HARNESS_HOSTS_CODEX=chatgpt.com:443,auth.openai.com:443,ab.chatgpt.com:443
```

On the Mac use `AOS_HOST=mac` and `AOS_OPENSHELL_PLATFORM=colima-mac`. The sandbox
bus route defaults to `192.168.5.2`; `host.docker.internal` is not a substitute.
Enabled messaging must pass an authenticated readiness probe inside the sandbox
before the harness starts. For Pi and omp settings, see the
[Pi adapter](pi-adapter.md#configuration).

## Add the repository policy

The runner reads `aos-policy.toml` from the repository's default branch, not from
a working tree or task branch. A missing file, a version 1 policy, or any unknown
key refuses the task before it is claimed; the error names what to change. The
version 2 shape:

```toml
[execution]
fail_closed = true

[filesystem]
exclude = [".env", "secrets/**"]

[network]
allow = ["pypi.org:443", "files.pythonhosted.org:443"]
```

| Key | Meaning |
|---|---|
| `execution.fail_closed` | required, must be `true` and the only key in `[execution]` |
| `filesystem.exclude` | repository-relative paths and globs left out of the sandbox copy. An entry matching a tracked file refuses the run |
| `filesystem.caches` | short cache names. Validated, but the runner does not mount caches yet |
| `network.allow` | `host:port` entries the sandbox may reach. The port is mandatory; a wildcard must be a leading `*.` with at least three labels. No scheme, path, gateway port or provider host |

`[filesystem]` and `[network]` are optional. Writable areas inside the sandbox are
the job checkout, `/tmp` and the sandbox home; system directories are read-only.
Harness API hosts are not part of the repository policy; the runner adds them from
operator settings. A bus network grant allows access but does not prove reachability.

## Optional operator files

Two git-ignored files in the source checkout extend a run. They belong to the
operator, not the repository, and are never read from a commit:

| File | Content | Effect |
|---|---|---|
| `.aos/services.toml` | `[postgres]` | the runner starts a per-run PostgreSQL container on the worker host and grants its address to the harness and acceptance sandboxes as `DATABASE_URL` |
| `.aos/evidence.toml` | `dir = "evidence"` | the runner copies that directory out of the sandbox before deleting it and references it in the result |

Both mechanisms are implemented. Their dated proof is not a substitute for the
[exact-candidate validation](release-validation.md) required before release.

## Create and run a task

Register the repository once, then add a task. Replace the placeholders; the brief
file holds the intended change and what counts as done:

```sh
aos repo set example-repo --host server
aos add --repo example-repo --title '<small change>' \
  --brief-file /absolute/path/to/brief.txt \
  --accept '<task acceptance command>' --harness claude --host server
```

`--harness` is `claude`, `codex` or `pi` (`pi` runs Pi or omp, chosen by
`AOS_OPENSHELL_PI_FLAVOUR`). Use the task ID that `aos add` prints; `AO-1` is an
example:

```sh
aos show AO-1
aos ready AO-1
aos run AO-1
aos show AO-1
```

Readiness requires a nonempty brief. Before claiming, `aos run` checks runner
settings, credentials, repository policy and `aos-workflow.toml` at the same
authoritative default commit. Explicit task checks override workflow `acceptance`,
which overrides workflow `test_command`; no checks means worker refusal. Manual
and dispatched runs use this same path, without a sync/cache. Claims freeze checks
per run rather than writing defaults into tasks. See [workflow configuration and
legacy registry migration](reference/configuration.md#repository-workflow-file).

The harness receives the task title, brief, resolved checks and repository prompt, with
the instruction to work only in the checkout, commit on the current branch, and
stop and report when a path or host is denied.

## What a run leaves

- **Branch:** the result commit is fetched into the source repository as `ao/AO-1`.
  The source checkout's working tree is not changed.
- **Status:** `review` when every acceptance command passed, `failed` when one
  failed (the report names it), `blocked` when the run could not finish (for
  example a refusal, an interruption or a sandbox that could not be confirmed
  deleted).
- **Report:** the run's report records that the sandbox policy read back from the
  gateway matched the rendering, the result commit, the hosts the sandbox contacted
  and the sandbox deletion; the
  acceptance output is stored beside it. `aos show AO-1` prints the status, branch,
  result commit and the start of the report; the full run record is in the API:

  ```sh
  curl -H "Authorization: Bearer $AOS_TOKEN" "$AOS_URL/tasks/AO-1"
  ```
- **Evidence:** with `.aos/evidence.toml`, the copied evidence directory is
  referenced in the result.

Every sandbox of the run is deleted at the end, which ends all its processes. If a
runner is killed mid-run, the next `aos run` cleans up its sandboxes first; you can
also run that cleanup directly:

```sh
python -m agent_os.openshell_runner reconcile
```

A reviewed result is recorded with `aos accept AO-1`; that does not merge the
branch into your checkout. A failed or blocked task can be edited and requeued; see
the [CLI reference](reference/cli.md).

## Fresh acceptance

By default the runner builds a new sandbox from the result commit, under the
repository policy only (no harness hosts, no credential), and runs the acceptance
commands there. That also discards processes the agent left behind, and the
per-run database is reset before the commands start. `AOS_OPENSHELL_FRESH_ACCEPTANCE=0`
runs acceptance in the sandbox the harness used instead; Pi and omp runs ignore it
and are always fresh.
With `AOS_OPENSHELL_BASELINE=1` the same commands run once before the agent in
a fresh `<name>-pre` sandbox; the database is reset after that run too, and the report
carries a `baseline:` line per command and a `baseline summary:` line.

## Try it with the example

[aos-example](https://github.com/happyhackerbird/aos-example) is a small notes web
app with PostgreSQL and a planted defect that its unit tests miss and its browser
check catches. Its default branch carries a v2 policy that allows only the package
index.

```sh
git clone https://github.com/happyhackerbird/aos-example "$AOS_REPO_ROOT/aos-example"
cd "$AOS_REPO_ROOT/aos-example"
mkdir -p .aos
printf '[postgres]\n' > .aos/services.toml
printf 'dir = "evidence"\n' > .aos/evidence.toml
```

Write a brief that describes the defect from its README (a new note does not
appear in the start page list once more than five notes exist), then:

```sh
aos repo set aos-example --host server
aos add --repo aos-example --title 'Show new notes on the start page' \
  --brief-file /absolute/path/to/brief.txt \
  --accept 'bash scripts/check.sh' --harness claude --host server
aos ready AO-1
aos run AO-1
```

`scripts/check.sh` runs the unit tests and the browser verification against the
run's database; the browser check writes screenshots and logs under `evidence/`.

## Development checks

```sh
sh scripts/test-isolated.sh
```

This runs the suite as UID 1000 in a verified private Linux PID namespace. Do not
run these process-management tests directly on a Linux host. See
[Contributing](../CONTRIBUTING.md) for the check environment. Real gateway,
harness, email and GitHub exercises are separate integration checks.
