# Pi adapter (OpenShell route)

[Documentation index](README.md) · [OpenShell backend setup](reference/openshell-backend-setup.md)

A task with `harness: pi` runs a Pi-family coding agent (Pi, or its fork omp) inside an
OpenShell sandbox through `python -m agent_os.openshell_runner run <task>` (or
`aos run <task> --backend openshell`). The code is `agent_os/pi.py` (flavours, event
contract, worker auth, redaction, bundle check) and `agent_os/openshell_runner.py`
(admission, lifecycle). There is no native-host Pi route: `agent_os/runner.py` keeps
refusing Pi, and no setting falls back to Claude or Codex.

**Status.** Built, offline-tested and live-qualified for both flavours on the Mac
OpenShell gateway on 2026-10-12: an authenticated `openai-codex`/`gpt-6-luna` repair of
the demo app passed fresh-sandbox acceptance (pytest plus PostgreSQL/Chromium
`verify.sh`) with Pi 1.1.0 and with omp 18.8.3, and the lifecycle, boundary and failure
paths are live-proven (see [Live qualification per flavour](#live-qualification-per-flavour)).
Linux has not been rerun for the Pi adapter.

## Configuration

| Variable | Meaning |
|---|---|
| `AOS_OPENSHELL_PI_FLAVOUR` | `pi` (default) or `omp` |
| `AOS_OPENSHELL_PI_BIN` | binary in the image; a plain command name or an absolute path (default: the flavour name) |
| `AOS_OPENSHELL_PI_PROVIDER` | provider (default `openai-codex` for `pi`; optional for `omp`, where `--provider` is passed only when set) |
| `AOS_OPENSHELL_PI_MODEL` | model, required; a `provider/model` prefix must name the same provider (Pi gets the bare model) |
| `AOS_OPENSHELL_PI_AUTH` | the worker's exported Pi-schema `auth.json`; this or `AOS_OPENSHELL_PI_LOGIN` is required |
| `AOS_OPENSHELL_PI_LOGIN` | the worker's persistent omp login directory on the host (mode 700, holding omp's `agent.db`), renewed before admission; excludes `AOS_OPENSHELL_PI_AUTH` |
| `AOS_OPENSHELL_PI_LOGIN_BIN` | the host omp that renews that login; a plain command name or an absolute path (default `omp`) |
| `AOS_OPENSHELL_PI_ACCOUNT_ID` | optional; when set the selected `accountId` must equal it |
| `AOS_OPENSHELL_HARNESS_HOSTS_PI` | must contain the provider's required grants: `chatgpt.com:443` for `openai-codex` (measured sufficient 2026-10-12) |
| `AOS_HARNESS_OUTPUT_CAP_MIB` | positive integer raw-output cap in MiB (default `512`); invalid values refuse startup |

Example (the qualification setting):

```sh
AOS_OPENSHELL_PI_MODEL=gpt-6-luna
AOS_OPENSHELL_PI_AUTH=~/.aos/worker/pi-auth.json
AOS_OPENSHELL_HARNESS_HOSTS_PI=chatgpt.com:443
```

With a persistent login, renewed on the host (the refresh goes from the host to
`auth.openai.com`, so the sandbox grant stays `chatgpt.com:443`):

```sh
AOS_OPENSHELL_PI_FLAVOUR=omp
AOS_OPENSHELL_PI_MODEL=openai-codex/gpt-6-luna
AOS_OPENSHELL_PI_LOGIN=~/.aos/worker/omp-login
AOS_OPENSHELL_HARNESS_HOSTS_PI=chatgpt.com:443
```

### Refusals before claim

The task stays `ready` and no run is created when any of these holds: missing model or
auth (neither `AOS_OPENSHELL_PI_AUTH` nor `AOS_OPENSHELL_PI_LOGIN`, or both); a provider other than `openai-codex` ("provider X has no supported auth path
yet": other credential stores are never copied); a model prefix naming another
provider; `AOS_OPENSHELL_PROVIDER_PI` set (Pi uses the selected worker auth, not an
OpenShell provider); a required harness grant missing; an auth file that is not a
regular file of at most 1 MiB, has no `openai-codex` OAuth entry, or is valid for less
than `AOS_HARNESS_TIMEOUT_SECONDS + 300` seconds; a persistent login whose
renewal fails or whose renewed token still does not cover the horizon (see below); an
`accountId` other than the pinned one; no v2 `aos-policy.toml` on the default branch; the gateway unreachable.

## Flavours and the flag table

The runner maps the options it needs onto each binary's flags. Headless prompts use
`/sandbox/prompt.txt` on stdin (exactly one stdin redirect). Interactive omp passes
its contents as one positional argument; every flag passed is required.

| Option | `pi` (measured on 1.1.0) | `omp` (from `omp --help`, 18.8.3) |
|---|---|---|
| print mode, JSON events | `--print --mode json` | `--print --mode json` |
| provider | `--provider <provider>` (always) | `--provider <provider>` only when configured (legacy flag) |
| model | `--model <model>` | `--model <model>` |
| session identity | `--session-id <uuid>` (generated per run) | none; the observed session header is the identity |
| session directory | `--session-dir /sandbox/pi-sessions` | `--session-dir /sandbox/pi-sessions` |
| no extensions / skills | `--no-extensions --no-skills` | `--no-extensions --no-skills` |
| no project context discovery | `--no-context-files --no-prompt-templates` | `--no-rules` |
| no MCP, themes, title | `--no-mcp --no-themes`; with messaging on `--no-mcp` is dropped and `-e builtin:mcp` loads Pi's built-in MCP support under `--no-extensions` | `--no-title`; omp has no `--no-mcp`: every omp run's `/tmp/pi-agent/config.yml` sets `mcp.enableProjectConfig: false` (no MCP file of the repository starts) and `mcp.startupTimeoutMs: 0` (measured 2026-10-16) |
| messaging (`AOS_OPENSHELL_COMMS=1`) | the `aos` server in `/tmp/pi-agent/mcp.json` (`"args": ["mcp"]`, `"exposure": "direct"`, `"timeout": 1000` seconds; the default exposure declares none of the tools to the model), `-e builtin:mcp`, `/sandbox/aos-run.json`, and its own prompt paragraph naming `mcp__aos__aos_ask` … `mcp__aos__aos_propose`; automatic tool-result append through image-owned `/usr/local/lib/aos-agent/aos_pi.ts`, the final `-e` after operator plugins, with the same successful-final-turn followUp as omp | the `aos` server in `/tmp/pi-agent/mcp.json` (`"timeout": 1000000`; omp cuts MCP calls at 30 s), a `--tools` allowlist retaining native tools including `task` and all six `mcp__aos_*` tools, `/sandbox/aos-run.json`, and its own prompt paragraph; independent owner-queue acquisition (`aos-agent queue` → serialized `aos-peer` batch aside → `aos-agent prepare --hook` on native `context` observation) through image-owned `/usr/local/lib/aos-agent/aos_pi.ts`, the final `-e` after operator plugins |
| interactive execution | unsupported | no `--print`/`--mode`; `--config /tmp/pi-agent/interactive.yml` immediately after `--approval-mode=yolo` disables the wizard; positional `"$(cat /sandbox/prompt.txt)"`; `--tools` adds `mcp__aos_submit`, `mcp__aos_inbox_wait`, `mcp__aos_family`, `mcp__aos_import` and `mcp__aos_export_candidate`; image-owned `-e /usr/local/lib/aos-agent/aos_pi.ts` remains last; its owner queue wakes idle OMP with the message content itself, delivers at safe turn boundaries while busy, and stops on a 409 and when the run file is gone |
| approvals | `--no-approve` | `--approval-mode=yolo` |
| native subagent tool | (none; Pi has no subagent tool without an extension) | `task` on, as Claude's Agent tool: no `--tools` allowlist, so omp's default set `read bash edit eval glob grep task wait todo web_search write` (measured 2026-10-16); a list without `task` would close it (measured 2026-10-14) |
| network startup checks | `--offline` | (none) |
| inner deadline | (the sandbox `timeout`) | `--max-time <AOS_HARNESS_TIMEOUT_SECONDS>` plus the sandbox `timeout` |
| agent directory | `PI_CODING_AGENT_DIR=/tmp/pi-agent` | `PI_CODING_AGENT_DIR=/tmp/pi-agent HOME=/tmp/pi-home` (measured: omp honours `PI_CODING_AGENT_DIR`; it writes natives and logs under `HOME/.omp`) |
| worker auth file | `/tmp/pi-agent/auth.json` (Pi schema) | `/tmp/pi-agent/agent.db` (omp's SQLite credential store; omp ignores `auth.json`) |
| event contract | settled: `agent_settled` required | no `agent_settled` in omp; the zero exit is the settlement |

Pi's `--no-context-files` suppresses automatic `AGENTS.md`/`CLAUDE.md`-style context, so
the task prompt is the only instruction input. omp has no equivalent: its `--no-rules`
does not stop root context files (measured 2026-11-02, omp 18.12.1 under the worker's
flags, planted root `AGENTS.md` and `CLAUDE.md` both reached the model, with a control),
so an omp worker loads both. See
[Instruction files](reference/configuration.md#instruction-files). Repository files
remain readable by the agent's tools either way; this is not a secrecy control.

With `AOS_OPENSHELL_PI_PLUGINS` set, both flavours add `-e <extension>` per shipped
extension. pi adds `--skill <dir>` per skill under `--no-skills`. omp replaces
`--no-skills` with `--skills=<names>` and copies shipped roles to where its `task` tool
finds them. The [setup reference](reference/openshell-backend-setup.md) has the details.

### OMP automatic delivery (locally measured, not deployed)

With messaging on, OMP's root owner session acquires peer messages through one
image-owned asynchronous helper path (`aos-agent queue`) into one owner-session queue,
recording `queued` (`source: "owner_queue"`) on acceptance while leaving `state='stored'`
until the native `context` event is observed. Queued messages are submitted as serialized
`customType: "aos-peer"` batch asides (`deliverAs: "aside"`): idle delivery wakes the
owner with the message content itself (no content-free nudge), and busy delivery lands at
supported safe boundaries (the next provider request after foreground work completes
uncancelled, or at `tool_result` where ordinary content, details and `isError` metadata
survive, including when delivery fails). When the native `context` event is observed for
an `aos-peer` batch, `aos-agent prepare --hook` records `prepared` (`source: "owner_queue"`).
On EOF or close before `context`, queued messages are never marked `prepared`; they remain
`stored` for redelivery or become `undelivered`/`receiver_closed` when the receiver closes.
Native Pi (`flavour: pi`) preserves its `tool_result` (`inbox --hook`) boundary append and
`turn_end` (`stop --hook`) finalization without starting the OMP owner-queue timer.

Native parent and child sessions instantiate separate extensions. Each instance consumes
its first `session_start` once: `sessionManager.getHeader()` must be a non-null,
non-array object with `parentSession` and `parentSessionId` absent or null before a nonblank `getSessionId()`
can become its owner. Child, missing/invalid-header and invalid-first-ID instances stay
disabled permanently, even on later starts; lifecycle and automatic handlers return before
spawning for disabled or nonowner sessions. Concurrent native OMP `task` child workers
are not offered `mcp__aos_*` tools and `task` refuses `tools: ["mcp__aos_*"]`, so at the
session/extension/MCP layer children neither consume the owner's mail nor hold or need
its credential; at the container OS layer, because `task` children run in the same
process/UID as the owner, child `bash` shares the sandbox filesystem (`/sandbox/aos-run.json`),
so root-session ownership is not OS-level shell credential isolation.

Both automatic and pull runs load the adapter. Root `session_start` opens the
authenticated exact-run receiver; `session_shutdown` closes it, including a valid
root shutdown before start. Pull sets `AOS_AGENT_AUTOMATIC=0`: lifecycle remains,
but delivery, finalization and nudge handlers are absent. Helper transport startup,
MCP negotiation/reconnect, EOF and native child lifecycle never establish ownership.
Starting the root does not certify that MCP negotiation has completed.

The adapter caches the original root's session file alongside its owner ID. A completed
`session_switch` retires only a different current ID whose `previousSessionFile` exactly
matches that cached file. It closes with the **old** owner and
`native_owner_replaced`, clears owned timers and permanently suppresses automatic
handlers, even if a later switch returns to the old ID. Cancellable-before-switch,
same-ID reload, missing identity and wrong/child previous files do nothing. It never
adopts the replacement session for this AOS run. Ordinary shutdown is a separate cause.

In a headless run, only an owner `turn_end` with assistant role, stop reason `stop` and an empty tool-results
array checks finalization. A block queues one follow-up to answer/explain unanswered
requests and restate the full report. The adapter passes its delivered flag as
`stop_hook_active` and spends the single run-level continuation before sending it.
If stop fails without usable text it retries once in that same handler; even a clean-empty
retry retains the first failure and yields a fixed sanitized failure continuation.
Usable reasons survive failures with the failure explanation appended. Child, tool,
error, abort and truncated turns cannot spend this opportunity. Late requests acknowledged
during the continuation get **no second continuation**; inbox is not automatically retried.

Interactive sessions have no automatic stop continuation; acknowledged requests must be
answered before submission. The Handoff section belongs inside the submitted report.

Interactive idle delivery: the coordinator may call `mcp__aos_inbox_wait` while
children work, and the owner-session queue wakes an idle OMP session directly with a
serialized `aos-peer` batch aside carrying the full message frames. Both paths report
rows as `prepared` (`inbox_wait_tool` or `owner_queue`), acknowledging nothing: a message
is prepared once and leaves the queue; the agent's reply is its acknowledgement.

Protocol **2.6** requires receiver lifecycle, retained-content recovery, honest
receipt reporting (`POST /intents/{id}/receipt`, including `source="owner_queue"`, no
automatic acknowledgement) and `context` handler registration from
helper, adapter, runner and backend for headless and interactive readiness.
The helper still offers six names
headless and eleven interactive: the six messaging names plus `aos_submit`,
`aos_inbox_wait`, `aos_family`, `aos_import` and `aos_export_candidate`.
Both automatic and pull check presence/version and adapter registration before release
(same major, minor at least the required minor), plus an authenticated requests response
with pending and retained ID-bearing row lists and overlaps, without mutating receiver
state. Older helpers/backends fail readiness. This proves compatibility, not delivery
behavior. Helper stdout is flushed
before complete ledger evidence and the best-effort `prepared` receipt; failure preserves
ordinary results and exposes a sanitized uncertainty notice, not raw diagnostics. A lost
receipt response can follow a committed receipt (the row is then delivered again and
counted twice); neither stdout nor ledger success proves model receipt or
exactly-once delivery. The post-exit, pre-deletion `aos-agent summary` is bounded to 4 KiB,
scrubbed bookkeeping; unavailable summary remains visible without discarding the run result.
The 2026-10-18 measurement also covered acceptance, complete-append/rollback and
fail-fast contention limits. This does not verify native Pi
or Codex automatic delivery.

Specific-answer `aos_wait(id, timeout_s)` also yields for unrelated incoming questions
and notices without abandoning the original question. Handle that work, then re-wait
using the **original request ID**. A co-observed answer and incoming work are both
returned, deduplicated by ID; notices retain their notice kind. Each valid result
starts with `aos_wait ` plus JSON: `request_id`, `outcome`, `target_state`,
`deadline_source`, nonnegative `remaining_s`, and `control` (`null`, `eof` or
`superseded`). Outcomes distinguish `answered`, `incoming`, `terminal`, `timeout`,
`eof`, `superseded`, `transport_error` and `local_error`; errors preserve collected
frames rather than hiding a known answer. A cut during transport leaves the server
outcome unknown.

The effective deadline is the earlier of the finite wait budget (1–900 s,
default 900) and an enclosing caller deadline. It bounds local scans, sleeps and
HTTP, with a 3 s per-poll-request cap; neither polling nor a later explicit call extends
an earlier caller deadline. Reads are non-atomic: target, pending and retained
observations are not a shared snapshot and do not promise later arrivals.
The existing requests route accepts at most 20 bounded `known_ids`, recovering full
exact-run-owned accepted rows without ACK/receipt mutation. Wait collection rescans
the ledger after reads and drains retired acknowledged history without sleeping,
returning at most 20 pending plus 20 recovered frames and target replies. Omitted
rows remain for another invocation; ledger history itself is not capped.

Wait output flush precedes ID-only local consumption markers and unchanged legacy
automatic ACKs. A marker records local output—not model receipt or explicit ack—and
does not hide an unacknowledged pending row. Output/bookkeeping failure leaves
possible duplicate delivery; marker failure skips the ACK block. SU3 owns the
receipt-evidence cutover, including migration of this discovery selector.
The 2026-11-02 disposable offline proof used real MCP/Uvicorn/API/Store plus generated
Pi 1.1.0 and Codex 0.162.1 commands: native tool-result payloads contained the full
incoming frame, then an answer on the same original ID. OMP 18.12.1 and Claude
2.1.296 additionally exercised common MCP/config/readiness. This does not certify
production connectivity, real inference or independent native receive.

The pi flavour is wired the same way (SU2: native MCP, the same extension last,
the same readiness checks). Its automatic delivery is not yet measured in production; the
integrated proof (SU4) owns that evidence.

### Capability check (not a version pin)

Inside the sandbox, after the effective-policy check and before the auth snapshot is
uploaded or the job released, the runner runs `<bin> --version` and `<bin> --help`. A
required flag missing from `--help` refuses the run (`blocked`), naming the missing
flags. The detected version is written to the report; a version outside the tested
list adds a warning line and the run proceeds. Tested with: **pi 1.1.0**, **omp 18.8.3**.

Before claim the runner runs the same check on the runner host's binary when one is
on `PATH`; because the image's binary is what runs, a host difference is printed as a
warning only.

### Event contract (fail closed)

Each flavour's stdout goes through its parser from the flavour table: `pi.parse_output`
for Pi, `pi.parse_omp_output` for omp. Success for Pi requires all of:

- a first record `{"type": "session", "id": <canonical UUID>}`, matching the requested
  id when one was requested; no repeated header;
- the latest `agent_end` whose last message is an assistant message with
  `stopReason: "stop"` and nonblank text (concatenated text blocks; the last 20000
  characters go to the report);
- no renewed activity after that end (`agent_start`, `turn_start`, `message_*`,
  `tool_execution_start`, `auto_retry_start`, `auto_compaction_start`); an earlier failed
  attempt may recover through a later successful end;
- a final `agent_settled` with `aborted: false`, and process exit 0.

omp 18.8.3 emits the same session header (its ids are UUIDv7, still canonical form) and
the same agent, turn, message and tool events, plus its own (`advisor_cost_changed`,
`notice`), but never `agent_settled` (measured live and in its print-mode source: print
mode returns after the prompt, its retries and the advisor drain, and exits 1 when the
last assistant message stopped with error or aborted). `pi.parse_omp_output` therefore
applies every rule above except the settled event: the zero exit is the settlement, and
the latest `agent_end` must not be followed by renewed activity; other events after it
are tolerated.

A model error, abort, `length`/`toolUse`/unknown stop reason, nonzero or missing exit,
timeout, cancellation, malformed or truncated JSON, invalid UTF-8, a stream other than
these events ("output did not match the Pi event contract") or output above
`AOS_HARNESS_OUTPUT_CAP_MIB` fails with a fixed, bounded diagnostic. An observed session id is kept on every
path; the requested id is never reported as observed.

The runner streams raw lines to the parser and scrubs each complete line before writing
`session.jsonl`, created with mode 600. No raw spool is written. Transport and disk writes
use chunks of at most 1 MiB. A fixed 32 MiB limit bounds each raw line held for JSON
parsing and redaction, including its newline when present. Exactly 32 MiB is accepted,
including an unterminated final line; one extra byte fails with
`pi: output line exceeds the 32 MiB limit`. Both read-backs stop at the first over-limit
line and terminate their read-back process. None of that line is retained or parsed;
the complete, scrubbed lines before it remain in the mode-600 session file.
Diagnostics keep the same keys: `truncated: true`, `raw_bytes` equal to bytes read,
and `oversized: false` unless the disk cap was also exceeded. A collection error takes
precedence, then a missing or nonzero harness exit, then the line-limit error.
The disk cap is unchanged and applies first when it is smaller than the line limit.
Final assistant text remains whole until callers redact it and cut the report tail.
The bytes-only parser API retains its legacy 8 MiB limit; collection does not use it.

Exactly the configured cap is accepted. One extra byte fails with
`output exceeds the <n> MiB cap` (a nonzero or missing harness exit keeps precedence).
The scrubbed prefix through the last complete line is retained; a cap-split line is
dropped to avoid exposing an incomplete secret. Diagnostics keep `raw_bytes` as bytes
read back (`cap + 1` on overflow), set `oversized: true` and `truncated: true`.
A read-back failure retains complete scrubbed lines already copied and marks
`truncated: true` when bytes arrived, with `collection_error` set; failure before the
first byte creates no session file. A parse failure does not stop evidence copying.

Read-back runs a direct absolute binary without a shell or login shell. Its remote
deadline is `120 + AOS_HARNESS_OUTPUT_CAP_MIB` seconds, allowing 1 MiB/s plus startup;
the host deadline adds 60 seconds of transport grace (692 seconds at the default cap).
Interactive Pi-family runs use the same copy for the newest session JSONL file, not
stdout, and only inspect its first line for the observed session id. Collection failures
remain diagnostic-only for interactive runs: a submitted run keeps its outcome, report,
result commit and acceptance. Disk-cap or line-limit overflow introduces an interactive
output failure. If overflow also encounters a collection error, that error takes precedence;
a collection failure alone remains diagnostic-only in interactive runs.

The collection regressions cover interactive line overflow followed by a collection
error, bounded progress across adjacent newlines, and raw-spool refusal through
Python file APIs, low-level writes and a child process's files in owned temporary
directories. These are implementer regression checks, not independent acceptance
or live OpenShell qualification.

## Worker auth: export and renewal

Production input is a worker credential, never the operator's personal Pi login.

1. Mint the worker's ChatGPT login inside a Linux sandbox (as for Codex:
   `codex -c cli_auth_credentials_store=file login --device-auth`) and download its
   `auth.json` to a mode-600 file under `~/.aos/worker/`.
2. Export it to Pi's schema in a private file named by `AOS_OPENSHELL_PI_AUTH`:
   `{"openai-codex": {"type": "oauth", "access": <tokens.access_token>, "refresh":
   <tokens.refresh_token>, "expires": <access token exp in ms>, "accountId":
   <tokens.account_id>}}`, mode 600.
3. Renew by letting the worker's own Codex lineage refresh (a Codex run, or a Codex login
   refresh in the setup sandbox), then export again. On this path the runner never
   refreshes tokens out of band and never writes the file back.

Per run the runner selects only the `openai-codex` entry, takes the effective expiry as
the earlier of the declared `expires` and the access token's JWT `exp` (decoded without
verification; a non-JWT token uses the declared expiry and the refusal says so),
requires validity for `AOS_HARNESS_TIMEOUT_SECONDS + 300` seconds before claim and again
immediately before the job is released, and uploads a snapshot whose refresh value is the
public placeholder `aos-nonrenewable-worker-snapshot`, so no renewable credential enters a
job. For Pi the snapshot is `/tmp/pi-agent/auth.json`; for omp it is
`/tmp/pi-agent/agent.db`, a SQLite file with one `auth_credentials` row (provider
`openai-codex`, type `oauth`, the same access/refresh/expires/accountId object), which
omp versions and completes on first open (both mode 600). A refusal for the horizon
means: export a freshly renewed access token (with a persistent login: the renewed token
itself is too short-lived for the run). `auth.openai.com:443` is not required:
with the nonrenewable snapshot neither flavour contacts it (measured 2026-10-12, run
AO-10-r1 granted `chatgpt.com:443` only), and it would only serve a refresh, which the
placeholder cannot perform.

The local expiry check cannot see a server-side revocation. A revoked worker login shows
up as a `failed` run whose session evidence holds `Encountered invalidated oauth token`
(Pi: assistant `stopReason: error` with exit 0; omp: `notice` "signed out automatically"
and exit 1); the remedy is a fresh worker login (step 1), not a renewal.

**Exposure.** The access token is a real bearer inside the sandbox. Job code can read it
and use it against the granted hosts while it is valid; this is accepted within the
operator-configured worker scope and is not credential secrecy from the job.

### Persistent worker login (renewed on the host)

Instead of an export, `AOS_OPENSHELL_PI_LOGIN` can name a persistent omp login
directory on the trusted host. The two settings exclude each other. The directory holds omp's own
credential store, `agent.db`, and renewal is omp's own refresh, so AOS has no second OAuth
client:

1. Once, interactively, as the runner user: `install -d -m 700 ~/.aos/worker/omp-login`,
   then `PI_CODING_AGENT_DIR=~/.aos/worker/omp-login omp login openai-codex` with the
   worker's ChatGPT identity (never the operator's personal one). Check it with
   `PI_CODING_AGENT_DIR=~/.aos/worker/omp-login omp token openai-codex --list`, which
   prints account identities, not tokens.
2. Before admission (before claim) the runner calls `pi.login_auth`. It requires the
   directory to be owned by the runner user, mode 700 and not a symlink. It takes an exclusive
   `flock` on `aos-renew.lock` in the directory and reads the single active `openai-codex`
   OAuth row of `agent.db` read-only. If that row's effective expiry does not cover
   `AOS_HARNESS_TIMEOUT_SECONDS + 300`, it runs `omp token openai-codex --force-refresh`
   with `PI_CODING_AGENT_DIR` set to the directory. That is the host omp named by
   `AOS_OPENSHELL_PI_LOGIN_BIN`, default `omp`, version 18.8.3 as in the image. The token
   omp prints goes to `/dev/null`. omp writes the new access token and the rotated refresh
   token into `agent.db` (coordinating through its own `auth_credential_refresh_leases`).
   The runner then reads the store again and applies the usual checks: validity for the
   horizon, an unchanged `accountId`, and `AOS_OPENSHELL_PI_ACCOUNT_ID` if it is set.
   AOS never writes the store.
3. Concurrent admissions wait up to 300 s for the lock. Each one then reads the store again,
   so only the first renews and the others reuse the renewed credential.
4. A failed renewal refuses the run before claim, so the task stays ready. This covers a
   revoked or expired refresh token, a refresh outage, a missing host omp, a timeout or
   lock contention. The refusal carries omp's exit status and a bounded diagnostic with
   known secrets and every long token-shaped string replaced by `[REDACTED]`. It gives the
   time until which omp holds the credential back (omp blocks a credential for about five
   minutes after a failed refresh, in `auth_credential_blocks`) and the two remedies: log in
   again (step 1), or check the host's access to `auth.openai.com:443`. Zero active rows,
   more than one active row (a dedicated worker login holds exactly one account) and a row
   omp disabled are refused with the login command.

The job still receives only the nonrenewable snapshot described above, built from the
renewed access token: the host's refresh token, rotated or not, never enters a job, and the
host store is neither mounted nor uploaded. Fresh acceptance receives no worker credential.
The real refresh token is among the selected secrets that the report redaction and the
result-bundle scan look for. Initial login and provider-forced reauthentication remain
operator steps. Ordinary access-token expiry needs no export.

What was measured where: on a scratch store in a job sandbox (no network, no real
login), omp 18.8.3 served a valid row of this layout through `omp token openai-codex`.
For an expired row or `--force-refresh` it attempted a refresh and, when that failed, exited 1
with `No active credential found for provider "openai-codex".` and a five-minute
`auth_credential_blocks` entry. Renewal, rotation, concurrency, revocation and outage
are tested with a fake omp that edits the store as omp does (`tests/test_pi.py`,
`tests/test_openshell_runner.py`). A real refresh against `auth.openai.com` is a server
validation step (the AO-24 change of 2026-10-27; its record is kept private).

## Lifecycle

1. Admission (above), then claim. Everything after claim runs inside one
   `try`/`finally`: any failure stops the heartbeat and posts `blocked` while the claim
   is owned.
   Claim/start leave receiver admission pending. The root native session opens it;
   process liveness and receiver eligibility are separate.
2. Ownership marker `owner.json` in the run state directory, then sandbox create. The
   main process waits (bounded) for the job and runs the harness under
   `timeout --kill-after=30 <harness timeout>` (bash watchdog if `timeout` is missing);
   the trailing sleep is bounded by harness + acceptance budget + 600 s.
3. Effective policy read back with `policy get --full` and compared structurally with
   PyYAML; Pi is strict (`Status: Effective`, `Source: sandbox`, no extra rules). Only
   then: prompt, capability check, auth snapshot, horizon recheck, job release.
4. After known native exit/submission, the runner first confirms authenticated
   receiver closure, before collection, quiescence or acceptance. An unconfirmed
   close is visible and prevents acceptance; error/startup/timeout paths close at
   their own boundary without replacing the primary failure, with a final conditional
   fallback before result/cleanup. Heartbeat remains live through acceptance.
   Then: stream stdout through the parser and retain scrubbed `session.jsonl`
   plus `pi-diagnostics.json` in `$AOS_OPENSHELL_STATE_DIR/<prefix>-<run>/`, bounded by
   `AOS_HARNESS_OUTPUT_CAP_MIB` as described above. The parse decides before any import
   or acceptance.
5. Result bundle checked before import: the bundle is unbundled into a temporary bare
   clone of the source, and every object its pack carries that the source does not already
   have (reachable from the result or not, since `git fetch` keeps the whole pack) is read
   with byte-safe git calls, and any object over 64 MiB or containing a selected secret
   (access, refresh, accountId; not the public placeholder) blocks the run. Nothing is
   checked out or executed. This literal scan is an accident guard, not protection
   against encoded exfiltration.
6. Acceptance always in a fresh sandbox under a separately rendered policy without
   harness hosts or credentials, strictly checked before the result tree is released.
7. Both sandboxes deleted and confirmed by a successful `sandbox list`; otherwise the
   run is `blocked` and the marker stays for `reconcile`. Before each create the runner
   lists sandboxes: a name that already exists (or a failed listing) refuses the run
   `blocked`, and that sandbox is neither tracked nor deleted by this run.

Incoming requests and finish notices bind to one accepted exact run. Submission,
close, cancellation and expiry preserve earlier delivery/ack/answer facts but terminate
future delivery. Unavailable sends are retained outcomes, not future-inbox promises;
CLI sends return 2 with the retained ID/reason. Replies distinguish the original answer
from whether a reply was admitted to that original asking run.

The irreversible, column-absence migration closes pre-existing receivers as
`legacy_unqualified` without owner/opened facts. Historical messages remain legacy and
unbound; unfinished peer work becomes terminal `legacy_unbound`. Operator questions
remain answerable, but replies cannot reroute to a newer asking run. Only disposable
local migration and offline native lifecycle have been exercised for this change;
the historical live qualifications below do not qualify a production migration.
The 2026-11-01 offline lifecycle proof additionally used OMP 18.12.1 and Pi 1.1.0
with the changed image adapter/helper and real Store/API. SU2 wait interruption and
SU3 receipt-evidence replacement are not part of this change.


A SIGTERM ends the run as `blocked` "runner interrupted", including one that arrives
while Pi output is being collected; from the start of cleanup until the result is
posted further SIGTERMs are ignored, and the previous handler is restored afterwards.

Posted `report`, `error` and `acceptance_output` have the selected secrets replaced
(longest first) before any cut, and after every cut (and on text cut upstream, such as
acceptance `tail -c 3000`) a leading or trailing fragment of at least 8 characters that
matches the end or start of a secret is replaced too. The report always ends with the critical lines (effective policy,
session id and evidence paths, result commit, sandbox deletion), appended after the
free-form part is capped. The result carries the observed session id.

On cancellation or lease loss the runner does not overwrite the bus state; it prints
the session line and keeps the evidence files locally. `session.jsonl` is inspectable
event evidence, not a resumable Pi history.

**Caveat (existing OpenShell convention).** The result is force-fetched into
`refs/heads/ao/<task>` of the source repository; if that branch is checked out there,
the fetch fails, the run is `blocked` and the source checkout is left unchanged.

## Live qualification per flavour

Run on a real gateway with a disposable repository. Each item records
versions, raw output size, session id, commit, source ownership and cleanup.

| Item | pi (1.1.0) | omp (18.8.3) |
|---|---|---|
| authenticated coding task repairs the demo-app fixture, fresh acceptance passes (pytest plus PostgreSQL/Chromium `verify.sh`) | **pass** (AO-9-r1, after a first attempt on a revoked login, AO-1-r1, failed closed) | **pass** (AO-11-r1; AO-8-r1 on the revoked login failed closed) |
| sentinel: outside writes, protected reads, egress, socket, sibling sandbox denied; repo grant as positive control | **pass** with a lifecycle stub in place of the model (AO-4-r1); the bus positive control fails on Colima, see below | flavour-independent (same runner, policy and image); not repeated |
| intentional failed acceptance; cancellation with cleanup; reconcile after `kill -9` | **pass** with the stub (AO-5-r1, AO-6-r1, AO-7-r1) | flavour-independent; not repeated |
| `auth.openai.com:443` still needed with the nonrenewable snapshot | **no** (AO-10-r1 with `chatgpt.com:443` only: `review`/`pass`; no contact in AO-9-r1 either); dropped from the required grants | **no** (AO-11-r1 ran with `chatgpt.com:443` only) |
| prompt read from stdin with `--print` | measured (2026-10-12 probes and AO-1-r1) | **measured**: the user message equals the prompt file (AO-8-r1, 657 characters) |
| auth placement | `/tmp/pi-agent/auth.json` measured | **measured**: `PI_CODING_AGENT_DIR` honoured; credentials only from `agent.db` there ("No API key found … or create /tmp/pi-agent/agent.db" with only `auth.json`; with the `agent.db` row the token was sent and refused as revoked) |
| JSON events | Pi contract, `agent_settled` (probes, AO-9-r1) | **measured**: Pi-shaped events without `agent_settled`; the successful AO-11-r1 stream ends at `agent_end` with exit 0; own parser `parse_omp_output` |

### Executed 2026-10-12 (Mac, Colima, gateway `openshell` 0.1.3)

Setup: a disposable bus (`agent_os.api` from the `pi-adapter` worktree on
`127.0.0.1:8041`, lease 120 s, sweep 15 s), `AOS_REPO_ROOT` holding a copy of the
demo-app fixture (`main` at `249d396`, untracked `.env` and `secrets/`), runner
`python -m agent_os.openshell_runner run <task>` with `AOS_OPENSHELL_PLATFORM=colima-mac`,
`AOS_OPENSHELL_PI_MODEL=gpt-6-luna` (omp: `openai-codex/gpt-6-luna`),
`AOS_OPENSHELL_HARNESS_HOSTS_PI=chatgpt.com:443,auth.openai.com:443` (AO-10-r1 and AO-11-r1:
`chatgpt.com:443` only), the worker's Codex login exported to a mode-600 Pi-schema file
(deleted afterwards). The first runs met a login revoked server-side; the operator
re-minted it by device auth at 18:42, and AO-9-r1 onwards used the new one. Images: `aos-harness:probe-app`
(`sha256:976f22da4f28…`, Pi 1.1.0), `aos-harness:probe-app-omp` (`sha256:847c403b8323…`,
adds omp 18.8.3 `omp-linux-arm64`, release SHA-256 `170852b7…832d8ca5` checked at build),
`aos-harness:pi-live-stub` (`sha256:42864194de02…`, adds the lifecycle stub below).

| Run | What ran | Outcome |
|---|---|---|
| AO-9-r1 | Pi 1.1.0, demo repair | `review`/`pass`: Pi exited 0 after 46 s (7 tool calls), `stopReason: stop`, settled; result `c4387f0 fix total` on `ao/AO-9` (`app.py`: `return sum(prices)`, bundle 427 B); fresh acceptance in `aos-ao-9-r1-acc`: pytest `2 passed`, `verify.sh` `VERIFY PASS` (PostgreSQL and headless Chromium in the sandbox); session `7f1361e8-7451-4dfb-97fa-c6633deeec3e` requested and observed; 144027 bytes of events retained as `$AOS_OPENSHELL_STATE_DIR/aos-ao-9-r1/session.jsonl` (no token shapes); network `chatgpt.com x1` (one WebSocket), pypi and files.pythonhosted.org, 32 denials all Chromium's Google update hosts; sandboxes deleted in 0.7 s and 3.9 s |
| AO-10-r1 | Pi 1.1.0, minimal task (add `NOTE.md`), `AOS_OPENSHELL_HARNESS_HOSTS_PI=chatgpt.com:443` with the required-grant constant relaxed for the measurement | `review`/`pass`, result `5ee9fd8`; rendered harness rule `chatgpt.com:443` only; the supervisor log has no `auth.openai.com` entry; session `a43ee823-dd77-408c-8f12-9a2cc03c8b83`, 88133 bytes; both sandboxes deleted |
| AO-11-r1 | omp 18.8.3, demo repair, image `aos-harness:probe-app-omp`, `chatgpt.com:443` only | `review`/`pass`: omp exited 0 after 121 s (19 tool calls), last event `agent_end`, no `agent_settled`; result `9194f2e fix total` on `ao/AO-11` (`app.py` fix plus a README edit, bundle 616 B); fresh acceptance pytest `2 passed`, `VERIFY PASS`; session `01a13107-e65e-745b-b955-94ed4c033242` observed (none requested); 470898 bytes of events (5.6 % of the cap); network `chatgpt.com x2`, pypi, 41 denials (Chromium update hosts and omp's model discovery for unconfigured providers); sandboxes deleted in 0.6 s and 0.4 s |
| AO-1-r1 | Pi 1.1.0, demo repair (revoked login) | `failed`: `pi: assistant stopped with error`; Pi exited 0 after 5 s with `stopReason: error`, "Encountered invalidated oauth token for user" (401 `token_revoked`), then `agent_settled` (not aborted): the zero-exit model error failed closed. Session `47ad1f6e-6f7f-4560-84c9-f57ec2f700b5` requested and observed; 28398 bytes of events retained scrubbed; network `chatgpt.com x1, denied x1`; no import; sandbox deleted in 0.2 s |
| (renewal) | the documented renewal, one `codex exec -m gpt-6-luna` with the worker `auth.json` in a disposable sandbox | refresh refused: `refresh_token_invalidated` ("Your session has ended. Please log in again"); the worker file was left unchanged |
| AO-8-r1 | omp 18.8.3 through the adapter, demo repair (revoked login) | `failed`: `pi: harness exited 1`; credential read from the uploaded `agent.db` and refused as revoked (omp `notice` "signed out automatically", accountId scrubbed to `[REDACTED]` in the retained events); session `01a130eb-e138-7632-9abe-62cef5b9614c` observed (none requested); 4862 bytes; capability check passed against the real omp `--help`; network `chatgpt.com x2, denied x9` (omp's model discovery for unconfigured providers); sandbox deleted in 0.3 s |
| AO-2-r1, AO-3-r1, AO-4-r1 | sentinel task through the stub, probes from the main sandbox (committed as `PROBE.md`) and as fresh-acceptance commands | AO-4-r1 `review`/`pass`, 17/17, result `d1ea610` on `ao/AO-4`, session `e7c418a8-2404-4dee-9522-384cc65b3811`. Denied in both sandboxes: reads of `/var/lib/dpkg/status` (world-readable, outside the read list), `/etc/shadow`, `/root`, `/.openshell`; writes to `/var/tmp` (mode 1777, outside the write list), `/usr/local/bin`, `/etc`; Python and curl to `example.com:443`; raw IP `1.1.1.1:443`; `pypi.org:80`; gateway port `host.docker.internal:17670`; no docker socket; the sibling sandbox `aos-pl-sib` (serving a secret on 8777) by container id and sandbox name (`transparent_tcp_policy_denied`); `.env`/`secrets/` absent from the job. Positive controls: `pypi.org:443` in both; `chatgpt.com:443` in the main sandbox only (denied in the acceptance sandbox: harness grants are not rendered there); the auth snapshot readable inside the job (the accepted exposure). AO-2-r1 and AO-3-r1 failed only on the bus control (below) |
| AO-5-r1 | stub commits a non-fix | `failed`, verdict `fail`, result `80a5a48` on `ao/AO-5`; fresh acceptance: pytest `2 failed` (total 500); both sandboxes deleted |
| AO-6-r1 | stub sleeps; bus cancel after the harness started | the heartbeat saw the cancel and the runner ended 59 s later: sandbox deleted, marker removed, bus kept `cancelled` (no report, `ended_at` unchanged), session `9ee263de-883b-457b-b6c0-f95581427a6b` printed and kept in `pi-diagnostics.json` (`returncode` null, "harness did not exit") |
| AO-7-r1 | stub sleeps; `kill -9` of the runner mid-harness | sandbox and `owner.json` left behind; `reconcile` while the run was `running`: 0 deleted; after the lease expired (`blocked`): deleted `aos-ao-7-r1` only, the unmarked `aos-pl-sib` untouched, marker removed. It left `job.tgz` in the state directory; fixed: `reconcile` now removes the run's scratch files (job and result tarballs, bundle, auth snapshots) as the runner's own cleanup does |

Source ownership after all runs: `main`, `HEAD`, the working tree (including the
untracked `.env` and `secrets/`) byte-identical; only `refs/heads/ao/AO-2` … `ao/AO-5`
and `ao/AO-9` … `ao/AO-11` were added. Every sandbox the qualification created was deleted.

The lifecycle stub (`aos-pi-stub`, qualification only, not shipped) is a Python script
that delegates `--version`/`--help` to the real `pi` (so the capability check is real),
reads the prompt from stdin, runs the probes or the scripted change in the job checkout,
commits, and prints a Pi-shaped event stream with the requested session id. It exercises
the runner, policy, bus and acceptance paths without a model; it is no evidence about
the model or the authenticated path.

**Historical bus positive control (2026-10-12).** The Mac probe's
`host.docker.internal` alias did not resolve
(`policy_dns_trusted_gateway_unavailable`), so that rendered bus rule did not
provide connectivity. This failed alias probe does not describe the current
messaging contract. Messaging-enabled and pull-only runs now require an
authenticated request from inside the sandbox before the harness is released.
On Colima the default sandbox bus host is `192.168.5.2`; use
`AOS_OPENSHELL_BUS_HOST` for a different reachable route. A bus reachable only
from the host is insufficient. Only headless comms-off runs omit this readiness
requirement; see [configuration](reference/configuration.md).

Measured before this build (2026-10-12 probes, outside the runner): Pi 1.1.0 with
`openai-codex`/`gpt-6-luna` authenticated and settled inside OpenShell, also with the
placeholder refresh value; a multi-turn repair produced 127624 bytes of stdout, far
below the 8 MiB cap; `policy get --full` quoted a wildcard host (`"*.example.com"`) and
PyYAML parsed it.

No `live-probe item:` lines remain: every omp item in the table above is measured.
