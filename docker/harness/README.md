# OpenShell harness image

The pinned workload image for the OpenShell route. One Dockerfile builds on arm64
(Mac, Colima) and amd64 (Linux server).

## Contents

| Component | Version |
| --- | --- |
| Base | `node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20` (node 24.21.0) |
| Claude Code (`@anthropic-ai/claude-code`) | 2.1.296 |
| Codex (`@openai/codex`) | 0.162.1 |
| Pi (`@earendil-works/pi-coding-agent`) | 1.1.0 |
| omp (release binary, sha256-checked per arch) | 18.8.3 |
| uv | 0.13.0 |
| Python (uv-managed) | 3.12 (the build baked into uv 0.13.0) |
| Playwright Chromium | 1.63.0 |
| Poetry (uv tool on the managed Python, `/usr/local/bin/poetry`) | 2.1.3 |
| apt: bash ca-certificates curl git ripgrep fd-find procps python3 python3-venv python3-pip jq openssh-client postgresql chromium fonts-liberation | Debian bookworm, pinned by the base digest only |

The resolved versions of everything, including the apt packages, are written to
`/etc/aos-harness/versions` at build time. The user is `sandbox` (uid 1000), working
directory `/sandbox`. `UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_PREFERENCE` and
`PLAYWRIGHT_BROWSERS_PATH` are set both as image ENV and in `/etc/profile.d/aos-harness.sh`,
because image ENV does not reach processes started inside an OpenShell sandbox.

`aos_agent.py` is copied to `/usr/local/bin/aos-agent`: the in-sandbox side of agents
that talk, one standard-library Python file with six command forms:
`mcp`, `inbox --hook`, `stop --hook`, `version`, `summary`, and `peek`. The MCP stdio server
retains `aos_ask`, `aos_reply`, `aos_inbox`, `aos_wait`, `aos_declare`, `aos_propose`
(which proposes a task in the run's own repository), and `aos_submit`, which submits an interactive run's report, candidate commit and child results for verification.
Its eleven tools also include `aos_inbox_wait`, `aos_family`, `aos_import`, and
`aos_export_candidate`, advertised and callable only under an interactive run file.
Headless runs advertise the six tools from `aos_ask` through `aos_propose`. The inbox command supplies
PostToolUse `additionalContext`; stop blocks once for acknowledged unanswered requests.
The helper reads `/sandbox/aos-run.json` and keeps the sole delivery ledger under
`/tmp/aos-agent`; the claim token is scrubbed from emitted results, hooks and diagnostics.

The image also installs `aos_pi.ts` at `/usr/local/lib/aos-agent/aos_pi.ts`, mode 0644.
Comms-enabled OMP loads it as the final `-e`, after operator extensions. It appends
intact helper frames to owner tool results, preserving ordinary content and metadata
even on delivery failure, and permits one run-level finalization continuation.
Automatic delivery is locally measured on **OMP 18.8.3 only** (2026-10-18), not native
Pi or Codex.
In an interactive run (`AOS_INTERACTIVE=1` in the pane's environment) the extension keeps delivering at tool-result boundaries and sends no follow-up user message.
The helper and adapter advertise protocol **2.2**. Readiness checks presence, compatible
version (same major, minor at least the required minor), six tool names headless, eleven interactive, and adapter
handler registration; it is not behavioral proof. The runner's interactive requirement
is raised to 2.2 by the runner change that follows.

`aos_wait(id, timeout_s)` (1 to 900 s, default 900) returns within its budget with one of
three outcomes: the reply frames once the request is answered; `no answer to {id} after {n}
s (state {state})`, not an error, when a reachable bus has no answer by the budget; or an
error `bus unreachable: …` (a refused connection, or `invocation deadline reached` when the
bus does not respond in time) or `bus {status}: …`. Each wait call asks the bus to hold for
at most 3 s and to end 2 s before the budget, so an unanswered wait on a healthy bus is not
cut by the helper's own deadline, and a wait superseded by a newer call or by EOF yields
`wait superseded by a newer call` within one such poll. An earlier caller-wide deadline still applies.

The blocking inbox wait (`aos_inbox_wait`) acknowledges nothing: rows are delivered again at the next tool
result or inbox call. In an interactive run the extension's idle arm sends at most
one content-free, agent-attributed nudge per idle period: "N AOS message(s) are
waiting; call mcp__aos_inbox". Its timer stops on `agent_start`, on a 409 from the
bus (and never restarts for that run), and when the run file is gone.

Delivery ordering is stdout flush → complete ledger evidence → best-effort bus ack.
Hook failures retain usable output, emit the fixed stderr signal
`AOS_AGENT_HOOK_FAILURE` with scrubbed diagnostics and still exit zero; the adapter adds
a fixed sanitized uncertainty notice, never raw subprocess diagnostics. A successful
stdout write or ledger append is not model receipt or exactly-once delivery.
Ledger writers use a nonblocking exclusive lock: contention fails visibly without
waiting or acknowledging, deliberately trading availability for bounded failure.
Under the lock, one write must be complete; failure rolls back to the owned original
EOF, and a pre-existing incomplete tail is refused unchanged. This protects cooperating
writers, not arbitrary shell writers sharing credentials, and provides no fsync/crash
durability guarantee. If stdout and stderr both fail, failure is unobservable under the
helper's exit-zero contract. `summary` is sanitized bookkeeping, not a receipt.

## Build

```sh
docker build -t aos-harness:mvp docker/harness
```

Run the same command on the Linux server; the build picks the architecture (BuildKit `TARGETARCH`, else `dpkg`), and amd64 selects
`omp-linux-x64`. Any other architecture fails the build.

The runner selects the image with `AOS_OPENSHELL_IMAGE=aos-harness:mvp`. The OpenShell
gateway must keep `image_pull_policy = "if_not_present"` (the image is local) and must be
restarted after a Docker runtime change.

## Bump and check

Change the ARG at the top of the Dockerfile (for omp, the version and both checksums),
rebuild, then:

```sh
docker run --rm aos-harness:mvp sh -lc 'cat /etc/aos-harness/versions; echo PW=$PLAYWRIGHT_BROWSERS_PATH; ls $PLAYWRIGHT_BROWSERS_PATH; id; pwd'
for c in claude codex pi omp uv psql chromium; do docker run --rm aos-harness:mvp $c --version; done
```

`lsof` is in the image because repository verification skills use it to find the process
that holds a port (aos-example's `app.sh doctor`); without it an agent tries to install it,
hits the denied package database and stops.

Poetry is in the image for repositories whose `pyproject.toml` pins `requires-python`
and its dependencies with Poetry's caret specifiers, which uv refuses to parse; their
checks install from `poetry.lock` with `poetry install`. The version matches the one that
generated the lock file the image was qualified with. Poetry keeps its cache and virtualenvs under the
sandbox user's home (`/home/sandbox/.cache/pypoetry`), which is writable in a sandbox.
