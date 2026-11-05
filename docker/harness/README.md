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
| omp (release binary, sha256-checked per arch) | 18.12.1 |
| uv | 0.13.0 |
| Python (uv-managed) | 3.12 (the build baked into uv 0.13.0) |
| Playwright Chromium | 1.63.0 |
| Poetry (uv tool on the managed Python, `/usr/local/bin/poetry`) | 2.1.3 |
| Devin for Terminal (release tarball, sha256-checked per arch, `/usr/local/lib/devin`) | 3000.11.92 |
| apt: bash ca-certificates curl git ripgrep fd-find procps python3 python3-venv python3-pip jq openssh-client postgresql chromium fonts-liberation | Debian bookworm, pinned by the base digest only |

The resolved versions of everything, including the apt packages, are written to
`/etc/aos-harness/versions` at build time. The user is `sandbox` (uid 1000), working
directory `/sandbox`. `UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_PREFERENCE` and
`PLAYWRIGHT_BROWSERS_PATH` are set both as image ENV and in `/etc/profile.d/aos-harness.sh`,
because image ENV does not reach processes started inside an OpenShell sandbox.

`aos_agent.py` is copied to `/usr/local/bin/aos-agent`: the in-sandbox side of agents
that talk, one standard-library Python file with ten command forms:
`mcp`, `inbox --hook`, `stop --hook`, `prepare --hook`, `receiver-start --hook`, `receiver-end --hook`,
`version`, `summary`, `peek`, and `queue`. The MCP stdio server
retains `aos_ask`, `aos_reply`, `aos_inbox`, `aos_wait`, `aos_declare`, `aos_propose`
(which proposes a task in the run's own repository), and `aos_submit`, which submits an interactive run's report, candidate commit and child results for verification.
Its eleven tools also include `aos_inbox_wait`, `aos_family`, `aos_import`, and
`aos_export_candidate`, advertised and callable only under an interactive run file.
Headless runs advertise the six tools from `aos_ask` through `aos_propose`. The inbox command supplies
PostToolUse `additionalContext`; stop blocks once for delivered unanswered requests.
The helper reads `/sandbox/aos-run.json` and keeps the sole delivery ledger under
`/tmp/aos-agent`; the claim token is scrubbed from emitted results, hooks and diagnostics.

The image also installs `aos_pi.ts` at `/usr/local/lib/aos-agent/aos_pi.ts`, mode 0644.
Comms-enabled Pi and OMP load it as the final `-e`, after operator extensions, including in pull mode.
In automatic mode, native Pi appends
intact helper frames to owner tool results, preserving ordinary content and metadata
even on delivery failure, and permits one run-level finalization continuation.
In OMP automatic mode (SU4), the root owner session acquires peer messages through the
asynchronous helper fetch command `aos-agent queue` into one owner-session queue,
submits serialized batches as `customType: "aos-peer"` asides (`deliverAs: "aside"`),
wakes idle OMP with the message content itself, delivers busy messages at supported safe
boundaries (the next provider request after foreground work completes uncancelled, or at
`tool_result`), and reports `prepared` (`source: "owner_queue"`) through
`aos-agent prepare --hook` only when the native `context` event is observed.
In an interactive run (`AOS_INTERACTIVE=1` in the pane's environment) the extension sends no finalization follow-up user message.
The helper, adapter and runner minimum are protocol **2.6**. Readiness checks presence,
compatible version (same major, minor at least the required minor), six tool names
headless, eleven interactive, adapter registration in automatic and pull modes (including
the `context` handler in automatic mode), and
the authenticated inbox's pending/retained row lists and overlaps. Older helpers or
backends without retained-content recovery or owner-queue support fail readiness.
These controls do not open a receiver and are not behavioral proof or a claim that MCP
negotiation has completed when the root session starts.

Messaging-on Devin runs set `subagents_enabled` false in the generated user
`config.json`, isolating the owner instead of inferring root/child identity from
hooks. Both automatic and pull modes install `SessionStart`/`SessionEnd` hooks
calling `aos-agent receiver-start --hook` and `receiver-end --hook`, each with
timeout 3; the payload's `session_id` owns the exact run, and an end reason of
`other` closes it as `native_end`. Automatic mode retains `PostToolUse`/`Stop`.
Plugin directories containing skills or `agents/*.md` are refused before claim
when messaging is requested, in automatic and pull modes, before any repository
downgrade. Any entry at the start commit's `.devin/skills`, `.devin/agents`,
`.agents/skills`, `.agents/agents` or `.windsurf/skills` disables messaging for
that run: these sources can start a child sharing the root's session id despite
`subagents_enabled` false. Presence alone is checked, not frontmatter; directories,
files and symlinks all count. Symlink or non-directory parents `.devin`, `.agents`
and `.windsurf` also disable messaging. Thus messaging-on runs take no plugin
skills or agents and no repository skills or agent profiles from those five
root sources. Comms-off runs still admit and install plugin skills and agents.

The check reads the start commit, including a continuing run's tip. Skills or
profiles written during a run and nested `.devin` directories below the root
are not covered. `.claude/skills` and `.cursor/skills` are excluded because
`read_config_from` disables their imports. The agent can rewrite its own user
config, the same boundary the hooks already have. Live behaviour on pinned
Devin 3000.11.92, including runtime-created sources, must be measured by the
coordinator after this repair and is not claimed here; tool presence is not
proof of peer delivery or independent idle receive.

`aos_wait(id, timeout_s)` (1 to 900 s, default 900) returns an answer, unrelated
incoming questions/notices, or both. Returning incoming work does not answer, cancel
or retarget the original request: handle the incoming work and call `aos_wait` again
with the **same original ID**. Notices keep their notice frame and need no answer.
Each valid result begins with `aos_wait ` and a JSON object containing `request_id`,
`outcome`, `target_state`, `deadline_source`, nonnegative `remaining_s` and `control`.
Outcomes are `answered`, `incoming`, `terminal`, `timeout`, `eof`, `superseded`,
`transport_error` and `local_error`; the last two use MCP `isError=true`. The text
following that line preserves complete collected frames, even on a companion-read
failure. A local transport cut means the operation was incomplete and the server's
outcome is unknown, not that the server necessarily failed.

One effective monotonic deadline bounds HTTP, ledger scans and local sleeps: the
earlier of this call's budget and an enclosing caller deadline. Target reads use
zero hold; each polling HTTP operation is capped at 3 s, with empty healthy cycles polling
on a 3 s cadence. Polling never grants a fresh deadline. A new explicit invocation
may request a new finite budget, but cannot extend an earlier caller deadline.
EOF and a newer queued call are distinct controls; metadata calls remain responsive.

The existing authenticated requests route also accepts at most 20 `known_ids`
(nonempty strings, at most 128 characters each) and returns `retained_requests`.
That pure read recovers complete exact-run-owned accepted request/notice rows from
Store, without receipt or settlement mutation; ordinary pending reads retain
their existing settlement behavior. Waits discover prepared IDs in the per-run
ledger, drain retired history without poll sleeps, and re-scan after companion
reads to catch concurrent hooks. A collection includes at most 20 pending and
20 recovered frames, plus target replies, deduplicated by ID. Omitted work stays
available for another invocation. Reads are **not an atomic cross-endpoint snapshot**;
arrivals after the last observation are not promised.

Specific-wait output is flushed before ID-only `wait_returned` markers and then
the `prepared` receipt reports. These markers suppress retained replay, not
still-pending bus rows, and prove local wait output only—not model context, durable
receipt or acknowledgement. Failed output/bookkeeping can duplicate delivery; a marker
failure skips that invocation's receipt block. Ledger records remain bounded to 512
bytes, but total pre-existing history is not capped; scans share the effective deadline.

## Honest receipts (2026-11-02, SU3)

A message's history is a set of separate facts in `intent_receipts`, each with its
actor, source, exact receiver run, first timestamp, repeat count and reason:
`stored` (the bus wrote the row), `queued` (a harness queue accepted it: the operator
channel's accepted post, or the OMP owner queue via `owner_queue`), `prepared` (a helper
wrote the frame into the model's context, by `inbox_tool`, `wait_tool`, `inbox_wait_tool`,
`hook`, `owner_queue` or the host `cli`), `included` (provider inclusion; no producer yet, accepted from nobody),
`acknowledged` (the agent's own explicit act: an answer, or the host `aos ack`),
`answered`, and `undelivered` (the terminal reason: admission, receiver close). The
first report of a fact keeps its evidence; a repeat only counts. Every automatic path
— inbox tool, PostToolUse hook, specific wait, interactive inbox wait — reports
`prepared` through `POST /intents/{id}/receipt` and acknowledges nothing; the helper
has no acknowledgement call. A row is prepared once and leaves the queue, as a Claude
Code user message is shown once at a turn boundary; there is no redelivery clock and
no attempt limit. A prepared request the agent never answers is the stop hook's one
in-run nudge and becomes `undelivered`/`receiver_closed` when its run closes. The
summary state `delivered` no longer exists; `prepared` replaces it, and `acknowledged`
is reached only by an explicit act. Rows written before this cutover are migrated once
with actor `legacy` (`acknowledged`/`automatic`, `prepared`/<old via>), never as the
agent's act. The CLI, the operator notice and the generated review material all render
the same `receipts=[…]` line from `agent_os.store.format_receipts`, with `[legacy]`
marking migrated facts and `absent` naming facts nobody recorded. None of this is
proof of model comprehension or exactly-once delivery.

Disposable offline qualification (2026-11-02) exercised actual helper MCP with
Uvicorn/API/Store: mutual questions yielded before the original budgets, full-body
hook recovery, notice/answer ties, original-ID continuation, deadlines, terminal
targets, EOF/newer-call distinction and labeled transport faults. Generated native
Pi 1.1.0 and Codex 0.162.1 commands returned full incoming frames and then the original
answers through loopback provider fixtures; OMP 18.12.1 and Claude 2.1.296 exercised
common MCP/config/readiness compatibility. This is not production, cloud/gateway,
independent native receive or explicit-ack qualification.

The blocking inbox wait (`aos_inbox_wait`) prepares the rows it returns, like every
other delivering path, and acknowledges nothing. In OMP automatic mode (SU4, 2026-11-03),
the owner session's queue timer (`aos-agent queue`) acquires pending rows, records
`queued` (`source: "owner_queue"`), and submits serialized batches as `aos-peer` asides
that wake idle OMP with the message content itself and land at the next turn boundary
when busy; `aos-agent prepare --hook` records `prepared` (`source: "owner_queue"`) only
when the native `context` event is observed. On EOF or close before `context`, queued
messages remain `stored` (or are marked `undelivered`/`receiver_closed` when the receiver
closes), never falsely marked `prepared` on synchronous send return or session file
persistence. Its timer stops on a 409 from the bus (and never restarts for that run)
and when the run file is gone.

Delivery ordering is stdout flush → complete ledger evidence → best-effort `prepared` receipt.
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

Change the ARG at the top of the Dockerfile (for omp, the version, immutable `OMP_BUILD`
locator and both checksums), rebuild, then:

```sh
docker run --rm aos-harness:mvp sh -lc 'cat /etc/aos-harness/versions; echo PW=$PLAYWRIGHT_BROWSERS_PATH; ls $PLAYWRIGHT_BROWSERS_PATH; id; pwd'
for c in claude codex pi omp devin uv psql chromium; do docker run --rm aos-harness:mvp $c --version; done
```

A Devin bump takes its version and both checksums from
`https://static.devin.ai/cli/<version>/manifest.json`, then repeats the checks of the 2026-11-02 Devin probe (its record is kept private): the adapter relies on
its plain-text print output, its hang when `server.codeium.com` is denied and its exit-0 stop.

`lsof` is in the image because repository verification skills use it to find the process
that holds a port (aos-example's `app.sh doctor`); without it an agent tries to install it,
hits the denied package database and stops.

Poetry is in the image for repositories whose `pyproject.toml` pins `requires-python`
and its dependencies with Poetry's caret specifiers, which uv refuses to parse; their
checks install from `poetry.lock` with `poetry install`. The version matches the one that
generated the lock file the image was qualified with. Poetry keeps its cache and virtualenvs under the
sandbox user's home (`/home/sandbox/.cache/pypoetry`), which is writable in a sandbox.

## OMP 18.12.1 upgrade qualification (2026-11-01)

The version, build locator `20261017-231446-291e055` and Linux checksums come from
the publisher's OMP 18.12.1 Homebrew formula. Supplied arm64 and x64 executables
matched those checksums and their ELF architectures. The Dockerfile uses that
build's `build.stencil.so` URLs, verifies the selected checksum and requires
`omp --version` to equal the pinned version.

The offline arm64 candidate `aos-harness:probe-omp18121-01a1983b`
(`sha256:eb5294aabd32fab09cc7eecd3e9f9d37883cdfbcf8aefc7a8dd7c2f6ce0c3556`)
was derived from cached release image
`sha256:8e8495fe8dd824f2585f1e3048417b3d0280ed7a642c80aa039615ae882e89f6`,
replacing OMP and its version-manifest entry only. Installed helper and adapter
bytes matched this checkout. This is **not a fresh Dockerfile rebuild** or a
production rollout; download availability and amd64 execution were not measured.

Network-disabled, unprivileged containers with a deterministic native provider
exercised the actual executable, adapter, helper and disposable AOS Store/FastAPI:

- Foreground work completed before a busy aside reached the next provider request;
  an idle aside woke the runtime without an ordinary user prompt.
- A native `wait` returned `Wait interrupted by message.` while its background
  work continued to completion. This differs from the pinned 18.8.3 measurement.
- The existing adapter preserved ordinary tool output and authenticated peer framing.
  Its idle timer sent a content-free nudge; the subsequent actual MCP inbox call
  delivered the frame. A parented native session did not consume owner messages.
- The current generated print-mode invocation completed, and the unmodified
  `parse_omp_output` returned a settled successful result. Helper inbox-wait and
  peek left requests unacknowledged; later boundary delivery acknowledged them
  (pre-SU3 automatic acknowledgement; today the same paths report `prepared`).

The upgrade does **not** fix delivery: a distinct same-tick idle burst still rejected
its second send asynchronously, and EOF persisted a queued aside without including it
in a provider request while cancelling foreground work. At that probe the AOS adapter fetched
busy messages only at tool-result boundaries (the SU4 queue poll below changed this). Ledger/bus receipts and native
API returns are not proof of model receipt or exactly-once delivery.

Real-provider/OAuth behavior, concurrent native task-worker ownership and the full
OpenShell lifecycle remain unqualified. `agent_os.pi`'s tested-version list is
intentionally unchanged, so its untested-version warning remains. Those upgrade
probes predate the exact-run receiver lifecycle below; independent reception,
model-receipt semantics and Claude peer transport remain separate concerns.

## Exact-run receiver lifecycle (2026-11-01, disposable offline qualification)

A claimed/started run is initially receive-pending. The recorded root native session,
not helper startup, `initialize`, `tools/list`, EOF or a native child, opens receiving.
Claude/Codex install `SessionStart` and `SessionEnd` lifecycle commands in automatic
and pull modes. Pi/OMP install root start/shutdown/replacement handlers in both modes;
`AOS_AGENT_AUTOMATIC=0` disables automatic delivery, finalization and idle nudges only.
Off has neither MCP nor lifecycle wiring. `Stop` is not `SessionEnd`.

Admission records one exact recipient run. A valid send without a running, receiving,
unsubmitted, unexpired recipient is retained as unavailable, with its reason and time;
it is never promised to a future incarnation. Submission closes admission while the
run and heartbeat remain live for acceptance. Native exit, completed owner replacement,
runner errors/timeouts, final result, cancellation and lease expiry cannot reopen it.
Existing receipt facts survive closure as an `undelivered`/`receiver_closed` fact is added; none is proof of model receipt.
Task-addressed sends with zero or multiple live candidates retain `no_live_receiver`
or `ambiguous_receiver`, without inventing an exact target. Replies use only the
original asking run: absent/blank identity is `original_run_unknown`, a missing row
is `receiver_missing`, and a different task's row is `recipient_mismatch`; a known
attempted ID remains visible even when unavailable, with no replacement-run fallback.
For a matching selected run, elapsed lease wins (`lease_expired`), then closing/closed,
submitted or non-live status (`receiver_closed`), then non-running/non-receiving
(`receiver_not_ready`). Lease equality remains eligible. Accepted rows have no terminal
reason/time; operator-directed questions need no exact receiver. An original answer
is recorded even when its reply destination is unavailable. These causes describe
new admission outcomes, not a rewrite of the receiver's first close cause or older
messages' immutable terminal reasons; older generic unavailable rows are not reclassified.
The reason repair was exercised with disposable Store/API, real helper subprocesses
and CLI output; it does not constitute a new four-native lifecycle qualification.

The one-shot SQLite migration marks old runs closed (`legacy_unqualified`) without
inventing native ownership. Historical peer requests/notices remain unbound legacy
rows, unfinished work becomes terminal `legacy_unbound`, and operator questions remain
answerable. Reopening the database never binds these rows to a later run. Migration
has been exercised only in disposable databases, not production.

Owned arm64 offline runs used the immutable cached substrate with the changed helper
and adapter, real Store/FastAPI/helper subprocesses, Claude 2.1.296, Codex 0.162.1,
Pi 1.1.0 and OMP 18.12.1. Generated ordinary commands exercised automatic/pull/off,
supported native children, Pi/OMP reconnect and nonmutating readiness controls.
Actual OMP `/new` and Claude `/clear`, second ordinary tool turns and normal TUI exit
left the original receiver closed. Claude used only a loopback Bedrock-gateway fixture
and disposable bearer: no AWS auth, real inference or direct-provider connectivity
qualification. Runner acceptance/error/startup/timeout paths used real HTTP/Store with
the existing simulated OpenShell transport, not a live gateway qualification.
Unrelated-incoming wait interruption (SU2) and honest receipts (SU3) are qualified in their
own sections; arbitrary partition/SIGKILL guarantees and deployment are not certified here.

