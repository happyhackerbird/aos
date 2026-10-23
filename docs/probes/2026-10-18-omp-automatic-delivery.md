# OMP automatic inbound AOS delivery: local production-image evidence

Historical internal directive: `2026-10-18-omp-automatic-delivery` (not distributed) ·
[Pi adapter](../pi-adapter.md) · [Image](../../docker/harness/README.md) ·
[Earlier mechanism probes](2026-10-17-multi-harness-delivery.md)

Measured by the desk on 2026-10-19. The rebuilt installed adapter passed all 14 local
acceptance cases after the native-child ownership repair, and the run's independent
verification then passed at `fdb1c1e81973376359b99131343906d231a1bd64`
(internal run record `2026-10-18-omp-automatic-delivery`, V1; not distributed). A later
`parentSessionId` hardening was measured the same way
([follow-up](#follow-up-parentsessionid-marks-a-child-too)).
No server deployment or deployed-runner lifecycle is claimed.

## Artifact and execution identity

| Item | Measured value / source |
|---|---|
| Implementation source | `ce8ef2c3ca29f9ee47b0a77e8ec01dd4b9050ea2`; aggregate and per-case `implementation_dirty_status` empty |
| Image tag | `aos-harness:omp-delivery-20261019-owner` |
| Image ID | `sha256:f298025e14abc6b2405b8746f227bcd1a6fc6a8e8f3ae9fd3fd55479530e0ac1` |
| Runtime | `omp/18.8.3`, sandbox uid 1000; helper 0755, adapter 0644 |
| Installed adapter | `/usr/local/lib/aos-agent/aos_pi.ts`, final `-e` after operator extensions |
| Helper/adapter protocol | `2.0`; readiness observed helper version, six MCP tools, adapter registration and extension order |
| Proof root (`P` below) | `~/.local/state/statiker/handoffs/agent-os-098d2065/2026-10-18-omp-automatic-delivery/production-proof` on the measuring Mac: an owner-only copy of the `/tmp/aos-omp-production-proof.M0uUYi` tree, which does not survive a reboot |
| Evidence root (`E` below) | `P/acceptance-owner` (written to `/tmp/aos-omp-production-proof.M0uUYi/acceptance-owner`) |
| Aggregate | `E/report.json`: all 14 named cases true, `unexercised_cases: []`; 204.15 s, exit 0 (desk execution binding / tracker F96) |
| Build/readiness outputs | `P/image-build.txt`, `P/image-readiness.txt` (tracker F95); build succeeded in 45.85 s |

Commands executed by the desk from the integrated checkout, as run; the readiness script
is `P/image-readiness.py` and every `/tmp` output is copied under `P`:

```sh
env DOCKER_BUILDKIT=0 docker build -t aos-harness:omp-delivery-20261019-owner docker/harness
uv run --extra dev python /tmp/aos-omp-production-proof.M0uUYi/image-readiness.py /Users/operator/code/agent-os aos-harness:omp-delivery-20261019-owner
uv run --extra dev python scripts/openshell_probe/mhm_live/driver.py --image aos-harness:omp-delivery-20261019-owner --out /tmp/aos-omp-production-proof.M0uUYi/acceptance-owner --case all
uv run --extra dev pytest -q
```

The driver uses actual image OMP and the installed helper/adapter, production invocation
construction and a disposable real `agent_os.store.Store`. Each owned container has
network `none` and no host mounts. A private loopback HTTP relay forwards bus/model
requests through Docker exec stdio to the host. Only the receiver's disposable run claim
enters `/sandbox/aos-run.json` (observed 0600 outside the job tree); no host worker auth,
Store database or host listener enters the container. The model key is a nonsecret fixture.
This is a local Docker/Store proof, not a deployed OpenShell gateway run.

`arrival/report.json` records this invocation (composition adds its operator extension
before the final AOS extension; negative removes only the latter):

```sh
PI_CODING_AGENT_DIR=/tmp/pi-agent HOME=/tmp/pi-home omp --print --mode json --provider proof --model proof-model --session-dir /sandbox/pi-sessions --no-extensions --no-skills --no-rules --no-title --max-time 300 --approval-mode=yolo --tools=read,bash,edit,eval,glob,grep,wait,todo,write,web_search,task,mcp__aos_ask,mcp__aos_reply,mcp__aos_inbox,mcp__aos_wait,mcp__aos_declare,mcp__aos_propose -e /usr/local/lib/aos-agent/aos_pi.ts
```

All cases except `behavior` use scripted provider decisions: they prove placement,
composition, native execution and bounds, not spontaneous real-model behavior.
`behavior/model-history.json` records status-200 host-authenticated gateway responses
from **`openai-codex/gpt-6-astra`**, despite the in-container alias `proof/proof-model`.

## Acceptance 1–8

MET means the directive's local acceptance was exercised at the reach stated here,
not deployment or independent release verification. Evidence paths are relative to `E`.
Every case retains `report.json`, `model-history.json`, `bus-events.json`, `injections.json`,
OMP stdout/stderr and native sessions; comms-on cases also retain helper summaries/ledgers
when created. Per-case `unexercised` lists still name desk-owned regression suites and
out-of-scope deployment/native-harness/idle/credential-exclusion work. The desk's separate
post-repair pytest output below supplies regression evidence; these lists were not erased.

| Acceptance | Status | Observation and evidence |
|---|---|---|
| 1. Automatic arrival | MET | `arrival/report.json`: operator `operator-i1` and peer `AO-2-i1` posted after the ordinary bash tool-start marker, not seeded in prompt/provider responses; exact intact frames in tool-role content of the next owner provider request after the observed pull. Operator/peer identity and authority warnings retained. See `arrival/injections.json`, `bus-events.json`, `model-history.json`, helper ledger. The observed request index is not a timing contract. |
| 2. No manual polling | MET | `arrival/report.json` has `forbidden: []` and positive ordinary bash/reply calls. Captured model/native call scan found no MCP inbox/wait or model-authored shell helper invocation. Same detector passed labelled synthetic forbidden-call controls (controls are not runtime calls). Helper counts alone were not the test. |
| 3. Real-model behavior | MET | `behavior/model-history.json`: real `gpt-6-astra` runs the ordinary script, then calls `mcp__aos_reply` for `operator-i1` with exact `ANSWER-f48e843475b1454daf3e57b4c32e8000`. `behavior/report.json`: correlated reply `AO-1-i1`, `reply_to: operator-i1`, stored reply body exact; original request progresses stored → acknowledged → answered. `behavior_artifact_exact: false`: proof is the reply, not a file-artifact claim. |
| 4. Adapter-disabled negative | MET | `negative/report.json`: comms true, six tools and claim retained, only final adapter extension removed; no pull, fresh marker unseen and request remains `stored`. This is distinct from comms-off. |
| 5. Native subagent coexistence | MET | `subagent/report.json` and native sessions: actual task/wait/yield success, child-only boundary leaves `operator-i1` stored and child requests marker-free; later owner pull delivers exact frame and acknowledges. Parent `01a153f8-1d39-734b-843f-8b9627839006`; child `01a153f8-1fef-75f9-88ab-43592040ea3b`, `parentSession` links to parent's session file. Installed direct smoke separately proves successful child-final callback no-spawn; not a second native successful-final run. |
| 6. Finalization, late and failure | MET | `finalization/report.json`: one distinct continuation, two final responses; Q1 `operator-i1` answered by `AO-1-i1`, late Q2 `operator-i2` acknowledged but unanswered with no second continuation and one ledger `stop_blocked`. `stop-failure/report.json` / `proof-fault-events.jsonl`: failed stop and same-handler retry yield one visible sanitized final-check failure without relying on another tool boundary; repeated final adds none. |
| 7. Failures and composition | MET | `outage`, `timeout`, `malformed`, `maximum`, `lost-ack`, `token`, `composition` reports and logs: all true; precise fixture scope below. Real helper/Store retained for acknowledgement state transitions; no exactly-once inference. |
| 8. Isolation and regression | MET | `off/report.json`: no messaging tool surface, adapter wiring, prompt or run/MCP files before/after OMP; native tools retained. Genuinely fresh separate uid-1000 container observes no run credential, MCP config, sessions or helper state, prints `PROOF-FRESH-ACCEPTANCE`, network none/no mounts. Fresh and job removal rc 0. All case cleanup assertions pass. Post-repair integrated pytest: 2154 passed, no skips reported, includes affected tests (F97 / `P/integrated-tests.txt`). This is not deployed-runner lifecycle proof. |

### Failure and composition reach

- **Outage:** actual helper contacts unavailable bus (`outage-drop` event). Ordinary
  `PROOF-ORDINARY-1` survives beside fixed sanitized delivery uncertainty; raw fixture
  diagnostics do not enter model/native output. Request is not falsely reported delivered.
- **Timeout:** labelled executable intercepts only `inbox`, sleeps 18 s; production
  adapter's 15000 ms spawn cap terminates it. OMP elapsed 23.4526 s includes ordinary
  tool/runtime time, not a claimed HTTP latency. Ordinary result and sanitized failure
  survive. Other commands delegate to saved installed helper.
- **Malformed:** same narrowly labelled inbox fault produces non-JSON stdout and raw
  stderr while exiting zero. Adapter exposes sanitized failure, not raw diagnostics,
  preserving ordinary output. This is process-result injection, not a real Store fault.
- **Stop failure:** labelled stop command exits 23 with raw stderr; logs show at least
  two stop invocations, one sanitized failure continuation and two final responses.
  Other helper commands stay real. See `stop-failure/proof-fault-events.jsonl`.
- **Maximum legal batch:** real Store supplies 20 peer requests `AO-2-i1` through
  `AO-2-i20`, each 8000 non-BMP characters. Full frames compare intact in the next
  owner tool-role request; all 20 have complete ledger acknowledgement records and
  acknowledged bus state. This is not a small ASCII stand-in (`maximum/report.json`,
  `model-history.json`, bus events and helper ledger).
- **Lost acknowledgement response:** real Store commits ack, relay then drops its
  response (`ack-response-dropped-after-commit`). Row is acknowledged, ledger evidence
  retained and one finalization continuation occurs. No duplicate observed in this
  fixture is not a universal no-redelivery guarantee (`lost-ack/bus-events.json`).
- **Token-bearing response:** fixture includes receiver claim in a returned body;
  helper replaces it with `***` before evidence sanitization. Raw stdout, stderr,
  sessions and ledger scans all report `token_present: false`; model tool context
  contains the positive `TOKEN-FIXTURE`/scrub marker (`token/report.json`). No credential
  value is reproduced here.
- **Replacing operator extension:** `/tmp/proof-replace.ts` precedes final AOS `-e`;
  its replacement marker and intact AOS frame coexist in model tool content
  (`composition/report.json`, invocation and model history).

`arrival/adapter-direct-smoke.stdout.json` explicitly labels its reach as installed
adapter with **synthetic subprocess results**, not live delivery. Its 13 named checks
pass: child tool no-op, metadata, blocks, invalid boundaries, separate child tool/final
no-op, child later-start closure, single follow-up, second-final active flag, invalid
first identity closure, unavailable/nonobject header closure and spawn contract.
The process contract is `/usr/local/bin/aos-agent`, 15000 ms and 33554432-byte buffer.

## Ownership failure, diagnosis and production repair

The original production replay was **13/14**, not success:
`P/acceptance-all/report.json` identifies source
`0de4b23277b0e05706b8dda6d393c0243dceb49d`, image
`sha256:675d0a1cb98dc06d7799e91ea25edc19bae39664176e857860a50af9d1861e00`.
Only `subagent` failed: the child tool result contained the fresh operator frame and
its Store row was already acknowledged before owner delivery. Native task completion
was successful; it did not establish owner-only delivery.

`ownership-diagnostic/subagent/owner-observation.jsonl` under the same proof root
records distinct parent/child extension instance IDs in the same process. Each got its
own first `session_start`; the child header's `parentSession` names the root session.
A first-ID-only rule therefore incorrectly adopted the child. The
`root-ownership-candidate/subagent/report.json` pass used `-e /tmp/owner-candidate.ts`:
that scratch wrapper and A5's source-candidate probes are **design repair evidence**,
not installed production acceptance.

The repaired installed adapter consumes each instance's first start once, requires
`getHeader()` to return a non-null non-array object with `parentSession` and
`parentSessionId` absent/null, then adopts only a nonblank ID. Missing/invalid headers, child headers or invalid IDs
permanently disable automatic handlers, including later starts. Separate-instance
smoke and the rebuilt `acceptance-owner` native run establish the repaired reach.
Automatic scope does not isolate credentials: a child can still explicitly use shared
MCP tools or shell access to consume the run inbox.

## Follow-up: parentSessionId marks a child too

The repaired adapter treated only `parentSession` as a child link, while this driver's
subagent assertions already accept `parentSessionId`. A header carrying only the latter
would have made a native child the owner again. Source
`e888d47fe5289f2b423cfaa870ac9e2872dd77e0` disables the instance when either field is
non-null, and the installed smoke gains `childIdHeaderClosed` (14 named checks). omp
18.8.3 was observed setting `parentSession`; no runtime emitting only `parentSessionId`
has been observed, so this guards a field rename rather than a measured case.

| Item | Measured value / source |
|---|---|
| Red | new driver against the previous image `sha256:f298025e…`, `--case arrival`: 19 of 20 assertions pass, the adapter smoke fails on `childIdHeaderClosed` alone (`P/followup-parentid/red-arrival`) |
| Image | `aos-harness:omp-delivery-20261019-parentid`, `sha256:065579c552d843c3f0cff86549ca4025a57b0d783ed0ddb2340148f585901e31`, `omp/18.8.3`; built in 86 s (`P/followup-parentid/image-build.txt`) |
| Readiness | `P/followup-parentid/image-readiness.txt`: adapter last after the operator extension, readiness passed, comms-off omits the adapter |
| Acceptance | `P/followup-parentid/acceptance/report.json`: all 14 cases true, `unexercised_cases: []`, empty `implementation_dirty_status`, 245 s; `subagent` 20/20 assertions |
| Regression | full suite at `e888d47`: 2175 passed in 488.84 s (`P/followup-parentid/integrated-tests.txt`). The first attempt died by SIGALRM inside `test_nested_deadline_does_not_extend_hook_budget` (`integrated-tests-sigalrm.txt`), a race that predates this change: see the next paragraph |

`invocation_deadline` re-arms the enclosing timer with as little as 1 µs when a nested
deadline expires, then the enclosing exit disarms it and restores `SIG_DFL`. On the macOS
host a timer already firing can still be delivered after that restore and kill the process:
`P/followup-parentid/nested_alarm.py` ran 300 single-process nested trials per interpreter
and lost 8 (3.11), 17 (3.12) and 15 (3.13) to SIGALRM; the same 300 trials inside the
Linux image lost none. It makes the host suite flaky; the production container was not
observed affected.

```sh
env DOCKER_BUILDKIT=0 docker build -t aos-harness:omp-delivery-20261019-parentid docker/harness
uv run --extra dev python "$P/image-readiness.py" "$PWD" aos-harness:omp-delivery-20261019-parentid
uv run --extra dev python scripts/openshell_probe/mhm_live/driver.py --image aos-harness:omp-delivery-20261019-parentid --out "$P/followup-parentid/acceptance" --case all
```

## Complete-append and delivery uncertainty

Production `docker/harness/aos_agent.py` keeps one per-run JSON-lines ledger. Ordering
is stdout flush → **all complete ledger evidence** → best-effort bus ack. A successful
stdout write is neither ledger/bus completion nor model receipt. Hook failure emits
fixed stderr `AOS_AGENT_HOOK_FAILURE` plus scrubbed diagnostics, exits zero, emits no
fallback stdout or extra `stop_blocked`, and preserves usable emitted frames. The
adapter preserves ordinary content/metadata and appends fixed sanitized uncertainty.
Clean empty output is a no-op; an ack warning beside valid output is not failed frame
production. Shared MCP post-output bookkeeping errors produce a scrubbed diagnostic,
no second RPC response or automatic ack retry, and do not escape the serve loop.

The append opens the existing file with `O_RDWR|O_CREAT|O_APPEND` and 0600 mode,
acquires `LOCK_EX|LOCK_NB`, captures EOF and refuses an existing non-newline tail
without mutation. One `os.write` must return the full encoded length; on write/count
failure it truncates to the owned EOF before propagating, with rollback failure also
propagated. Closing releases ownership. Contention fails visibly with no waiting,
sleep or retry: pre-output MCP returns an ordinary tool error, post-output retains
its frame/diagnostic, hook signals failure, and no ack is authorized. A later independent
invocation may recover after release. This deliberately sacrifices availability for
bounded failure. Protection covers cooperating writers only, not arbitrary shared-shell
writers; it is **not fsync or crash durability**. An incomplete tail after failed rollback
is refused on the next append.

Mechanism measurements are distinct from the 14-case model-placement replay:
tracker F55 records actual Store/real file-size-limit scratch recovery (frame retained,
fixed marker, row stored, zero acked records, parseable remaining lines; later unconstrained
pull acknowledges and final stop blocks; pre-existing incomplete tail unchanged).
F64's real held-lock scratch probe reports pre-output error 0.0005 s, post-output
following ping 0.0027 s with frame retained/no ack and visible diagnostic, hook marker
0.0005 s, uncontended one-second wait 1.0104 s. A5 F88 records production-helper
lock/short-write/hook/MCP/lost-ack/deadline execution, while its native adapter was still
a source candidate. These are mechanism evidence, not installed-model receipt.
The post-repair full suite includes the real Store short-write/recovery, incomplete-tail,
committed-row, held-lock fail-fast and MCP single-response/continued-service regressions.

The adapter spends its single continuation before sending follow-up. Only owner assistant
`stop` with empty tool results qualifies; child/tool/error/abort/truncation cannot spend it.
Stop failure without usable text retries once in the same handler and retains the first
failure even if retry is clean-empty; usable reason or fixed failure fallback requests
answers/explanations plus the full final report. `stop_hook_active` carries delivered
state. Late requests get no second continuation. There is no automatic inbox retry.

## Regression history and limits

- Initial affected run: **1 failed, 400 passed in 193.61 s** (tracker F81); failure was obsolete exact extension-suffix expectation in
  `test_pi_plugins_invocation_per_flavour`. Unused invocation/copied assertion was
  removed, not repinned; behavioral tests retained. Binding does not report skips
  for this run; none are invented here.
- Earlier full run: **2154 passed in 321.21 s** at
  `a26f2e446efd4f4e694d20bb84b0ed0a4f6e1b4e` (F86), before
  root-ownership production repair; not current acceptance proof.
- Current full run: **2154 passed in 347.03 s (0:05:47), exit 0, no skips reported**
  at `ce8ef2c3ca29f9ee47b0a77e8ec01dd4b9050ea2` (F97 / `P/integrated-tests.txt`). Includes affected helper/runner/Pi tests; no separate repaired
  affected-only count is asserted. This documentation leg ran no build/test/lint/formatter.

Readiness only proves presence, compatible major-equal/minor-at-least protocol, six
names and registration; the bounded sanitized summary is bookkeeping, not a receipt.
The runner redacts the claim in retained sessions and bounds post-exit summary to 4 KiB
before deletion. Fresh acceptance has no messaging credential or adapter wiring;
image file presence alone is not loading it.

Independent verification (run record V1) passed at `fdb1c1e81973376359b99131343906d231a1bd64`:
it reran all 14 cases from a fresh isolated copy and inspected, but did not rerun, the
2154-test output. No server deployment,
deployed-runner lifecycle, native Pi/Codex automatic-delivery verification, Herdr,
idle wake-up, child credential exclusion, crash durability, universal model-receipt or
exactly-once guarantee is claimed. If stdout and stderr both fail, the helper's exit-zero
contract makes that simultaneous failure unobservable; neither readiness nor the bridge
can recover evidence unavailable on both channels.
