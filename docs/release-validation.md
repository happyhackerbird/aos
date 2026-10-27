# Release validation

## Gate state

Release `v0.1.0` is the exported tree of the release commit that contains this page.
Its `RELEASE-MANIFEST.json` names that commit, the export rules and the transformed
payload. The runtime qualified below is source `3021bed`, composed on 2026-10-23 from
the release branch and main `714bd74`. Later commits change documentation, test
comments and fixture names, the release-matrix fixture list and the export scope
only: every `agent_os/` file, `docker/harness/Dockerfile`, `aos_agent.py` and
`aos_pi.ts`, the root `Dockerfile`, `pyproject.toml` and `uv.lock` equal `3021bed`
byte for byte. The operator authorized publication and production rollout on
2026-10-24. This page records measured artifacts, not an all-green verdict.

| Gate | Evidence / state |
|---|---|
| Interactive Herdr server proof | Completed on 2026-10-22 with declared coverage limits (record not published); interactive omp stays experimental |
| Candidate regression | Composed source suite: 2250 passed, 0 skipped (macOS host). The staged export's isolated suite is in the release notes |
| Linux Claude/Codex matrix | 20/20 scenario PASS |
| Mac Claude/Codex matrix | 18/20 scenario PASS; two retry cells failed on scenario timing (below), accepted as a qualification |
| Experimental Pi / omp matrices | Not run on this candidate: the worker token had expired; the operator accepted the release without them |
| Readiness and merged acceptance | Both platforms; details below |
| Export scope, links and identifier checks | Byte rules, scope rules and local file links passed on the export of the release commit |
| Private vulnerability reporting | Enabled during publication, after the repository became public; this page predates that step |

## Release candidate evidence (runtime `3021bed`, 2026-10-24)

| Check | Pin / environment | Actual result |
|---|---|---|
| Full source suite | Composed `8a2bffd`, macOS host | 2250 passed, 0 skipped |
| Linux Claude/Codex matrix | Runtime `3021bed`, image `aos-harness:release-da47c25`, both fixtures, five scenarios | 20/20 scenario PASS |
| Mac Claude/Codex matrix | Same runtime and image inputs; Colima aarch64 | 18/20 scenario PASS |

The per-scenario Linux and Mac records (each scenario, the image ID and versions, the
configured models, readiness, merged acceptance and the claim-token log scan) stay
with the private release evidence. Both hosts ran the candidate from a non-editable install whose
31 package files matched the source by bytes; each image's helper matched the
candidate's `docker/harness/aos_agent.py` by SHA-256.

**Mac demo-app retry cells (Claude and Codex) failed:** the first run finished the
fix and its result commit was recorded before the scenario's SIGTERM, sent after 45
seconds of running, arrived. The requeued run continued from that commit, added
none, and the runner's continuing-run rule (since 2026-10-18, also in the previous
candidate) ended it `failed: no commits on top of the previous attempt`. The slower
aos-example retry cells on the same host and every Linux retry cell passed. This is
a limit of the scenario's fixed interrupt time, not a candidate regression; the
cells were not rerun. The previous candidate's Mac `ENOTFOUND` failure did not recur:
Mac aos-example Claude success passed.

On each platform, healthy automatic and pull-mode runs reached review with the
prompt written. A dead sandbox bus port in both modes was refused before the prompt
(`messaging readiness bus: authenticated sandbox probe failed`), no transient run
credential remained, and host bus health stayed 200. Genuine aos-example merged
acceptance passed in a fresh sandbox through `aos integrate`: 10 pytest checks on Mac
and 9 on Linux (each agent's own fix commit added different tests to the fixture's
8), four browser/database checks, both commands exit 0. No observed claim token
appeared in any runner or integration log.

Scenario PASS means the matrix's named predicates, not absence of every diagnostic.
The exact Codex model ID was not retained.

## Previous candidate (runtime `9fe5d74`, 2026-10-21)

These are distinct measured artifacts, **not an all-green release verdict**:

| Check | Pin / environment | Actual result |
|---|---|---|
| Full source suite | `7515686`, private Linux PID namespace, UID 1000 | 2118 passed, 1 skipped, 1 warning; no exclusions |
| Full transformed export suite | `70eea22`, private Linux PID namespace, UID 1000 | 2077 passed, 1 skipped, 1 warning; no exclusions |
| Export boundary checks | `70eea22` implementation, Mac and isolated Linux | 43 passed on each platform |
| Linux Claude/Codex matrix | Runtime `9fe5d74`, both fixtures, five scenarios | 20/20 scenario PASS |
| Mac Claude/Codex matrix | Same runtime; Colima aarch64 | 19/20 scenario PASS; one failure below |
| Experimental Pi / omp matrices | Same runtime; both fixtures and five scenarios | Each platform: 10/10 Pi and 10/10 omp scenario PASS |

The suite skip was the case-insensitive filesystem check on a case-sensitive
filesystem; the warning was the Starlette/httpx deprecation. The three historical
exclusions below were not carried forward. The later export-boundary run covers
two new umask cases; it is not a claim of a rerun of the entire source suite.
These are qualification checkpoints; the release notes pin the manifest and the
staged rerun of the artifact containing these evidence pages.

The per-scenario Linux and Mac records (each scenario, image ID/version metadata,
configured models, readiness, fresh merged acceptance and cleanup) stay with the
private release evidence. A file-level comparison recorded all 38 runtime/package/image/driver inputs in the `70eea22` export matching
`9fe5d74`; this does not assert identical documentation, tests or export
tooling. Among 12 demo-app fixture files, only the README's introductory prose/
navigation changed; task, application and acceptance files are byte-identical.
The installed Linux wheel's 31 package files were also compared by bytes.

**Mac's aos-example Claude success case failed:** `AO-12-r1` ended with
`harness exit 1: no stderr`, no verdict and no result commit. Its retained final
message reports API reachability/DNS error `ENOTFOUND`; the underlying cause remains
unresolved. It was not rerun merely to obtain green. The earlier interrupted
`AO-1-r1` attempt to correct omitted explicit AOS git identity is also retained.
Neither attempt is erased by the selected matrix.

On each platform, healthy automatic and pull-mode runs reached the job. Bad
sandbox bus routes in both modes refused before a prompt was written while host
bus health stayed 200; the owned sandboxes were removed. Genuine aos-example
merged acceptance passed in fresh sandboxes: each recorded 10 pytest checks and
four browser/database checks. Owned matrix resources and bus listeners were
stopped. Linux's production dispatcher was unchanged. Mac's existing port 8010
reported `ok=true` before/after; port 8011 reported `ok=false` before/after despite
HTTP 200. This is not an all-endpoints-healthy claim.

Scenario PASS means the matrix's named predicates, not absence of every diagnostic.
Cancelled tasks remain terminal: retry cases use a separate SIGTERM-blocked task
and its second run. The exact Codex model ID was not retained; its CLI default is
not inferred from its version. The Linux SSH interruption/recovery is not the
interactive Herdr server proof.

## Historical follow-up baseline

The 2026-10-21 follow-up at `f564fc5d57a16440c1b1b6fb715a63f3bf63d02e`
recorded **2,170 passed, 2 skipped, 3 deselected, 1 warning** in 589.05 seconds,
as UID 1000 in a measured private Linux PID namespace. The three exclusions were:

- `test_source_untouched_strengthened_moved_origin_and_task_worktree`
- `test_extension_protocol_and_interactive_guard`
- `test_timeout_kills_the_process_group_grandchild_gone`

Those are historical exclusions, not permission to omit them from the candidate.
A separate isolated process-group check recorded 25 passed, no skips/failures.
Keep namespace isolation even after the process-safety fix.

The same follow-up recorded 24 automatic/pull-no-poll/pull-explicit control cells,
eight real Telegram round trips, eight corrected unanswered 120-second waits, and
actual-image Claude parent/child payload checks. These cover experimental messaging;
they do not establish the headless release matrix or the interactive Herdr server
proof at a newer revision.

Qualifications retained from that evidence:

- Mac cancellation removed the sandbox and accepted no later result, but attempted
  late progress/result posts were rejected with HTTP 409. This is not clean runner
  cancellation proof.
- Two Mac phone replies arrived 10.064 and 13.423 seconds after their advertised
  deadlines, though inside the live waits. Round trips passed; those deadline checks did not.
- Linux Telegram evidence has outgoing notices and correlated bus replies, not raw
  incoming Telegram update/message identifiers.
- Linux control traces end at the accepted commit. MCP counters do not rule out
  later native-shell polling. Mac traces have wider coverage.
- Unchanged Linux tool/API/ring/off cells retain older source provenance. Not every
  cell ran the newest helper image.
- Failed/interrupted attempts and two OOM exits remain part of the evidence; they
  were not rewritten as passes. Managed Claude OAuth was checked by shape, not by
  comparing an extracted secret literal.

Production was still `d5eac52` at that handback; it is the rollback reference for the
`v0.1.0` rollout. The preparation work itself did not alter that
rollback reference or authorize replacing the production bus, runner or images.

## Required candidate proof

Run the complete regression suite using [the isolated procedure](../CONTRIBUTING.md).
Then exercise the changed behavior itself: authenticated readiness from a real
sandbox (reachable and refused routes, including pull-only), local CLI/bus flow,
OpenShell merged acceptance, and the supported harness matrix on Linux and macOS.
Record actual skips, exclusions and failures; never substitute a dry-run plan.

The interactive Herdr server proof separately requires an interactive server parent dispatched through normal
intake, real headless children, terminal directions/questions, idle delivery,
Herdr and actual SSH disconnect/reconnect survival, exact child imports, combined
acceptance and the required cancellation/timeout/stale/conflict/outage cases.
A Mac smoke or unit suite cannot close that server gate.

## Public evidence scope

Dated probe records, their evidence files and the probe kits are not part of the
public tree; the guides state the measured facts they rely on, with their dates.
Internal plans, directives, execution logs, raw runtime logs and private follow-up
archives are excluded as well. Passing a named-pattern
scan is not proof that unknown secrets cannot exist: the final artifact still needs
content review before it crosses the publication boundary.
