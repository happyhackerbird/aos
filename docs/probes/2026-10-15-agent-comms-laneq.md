# Agent communication lane Q: the second live proof on the Mac (a proposed task's finish notice reaches its parent, a Stop block leaves the report intact, a review task reads the reviewed branch)

Date: 2026-10-15 (times UTC). Executor: session agent-os-ff (Opus 5.5) for the coordinator
agent-os-e7, brief `~/.aos/briefs/agent-comms-laneq.md` (sha256 690525e8c6d54c73…). Base
`origin/main` b38a197 (lane P merged bc52a56, lane V merged 22599af). Directives:
internal U7 proposals/finish notices, U2 S8 Stop hook and the lane B/P/V
orchestration rows (not distributed). Setup repeated
from [U4](2026-10-14-agent-comms-u4.md); kit `scripts/openshell_probe/comms_p1b/` (its `runs` and
`state-dir` subcommands were added here).

## Headline

All three parts passed, in four Claude runs (no retry), with the runner's own wiring and messaging
on by its default (nothing set):

- Part 1: task P (AO-1), started by `aos dispatch`, proposed AO-2 with `aos_propose`. AO-2 was
  created `ready` (the repository's `auto_ready_proposals` switch) with `proposed_by` AO-1, and the
  dispatcher started its run while P's was live. AO-2 ended `review` with `notes/q-child.txt` on
  `ao/AO-2`. In the same finish transaction the bus wrote the finish notice AO-2-i1 (a `request`
  from AO-2 to AO-1 naming status, branch, commit and a report excerpt). P's PostToolUse hook
  acknowledged it 25 s later, after a `sleep 30`. P wrote `AO-2 finished: review` to
  `notes/q-parent.txt`, committed and ended `review`. Every sandbox was named `a1a-…`.
- Part 3: task R (AO-3, `aos add --reviews AO-2`) read `ao/AO-2` in its checkout without checking
  it out. Its checkout held exactly `refs/heads/ao/AO-2`, `refs/heads/ao/AO-3` and `refs/heads/main`:
  not P's `ao/AO-1`, which was in the source clone when R's copy was built, and no `refs/remotes/`.
  `ao/AO-2` was untouched afterwards. Acceptance passed in the fresh sandbox, including
  `git rev-parse --verify main`.
- Part 2: task D (AO-4), launched by hand with `AOS_OPENSHELL_FRESH_ACCEPTANCE=0`, received an
  operator note it was told not to answer. The PostToolUse hook acknowledged and injected it after
  the first `sleep 45`. D did not reply, and the Stop hook blocked once (`stop_blocked` in the
  ledger the acceptance output carries). D's posted report is the restated full report, says why no
  reply was sent, and ends `REPORT-END`.
- The claim tokens of the four runs are 0 hits in every artifact, with positive controls found.
- Found beyond the brief: the finish notice, a `request`, made P's Stop hook block too. P answered
  it with `aos_reply`, and that reply is a row stored for a finished task that nobody will read (a
  design question, Gap 1). Both runs that hit a Stop block report `denied x2` on the network line,
  the two that did not report `x0`, and the denied hosts cannot be recovered once the sandbox is
  deleted (Gap 2).

## Setup, as run

- Worktree `agent-comms-laneq` at b38a197. Base check: `merge-base --is-ancestor b38a197 HEAD`
  exit 0, `log b38a197..HEAD` empty, `status --porcelain` empty. From the worktree,
  `uv run python -c "import agent_os; print(agent_os.__file__)"` printed
  `/Users/operator/.aos/worktrees/agent-os/agent-comms-laneq/agent_os/__init__.py` (the first `uv run`
  created the worktree's git-ignored `.venv`). `bus_prefix('http://127.0.0.1:8013')` = `a1a`.
- Image: `DOCKER_BUILDKIT=0 docker build -t aos-harness:laneq docker/harness` from the worktree.
  `aos-harness:laneq` = `077d95c4d19b` (its `/usr/local/bin/aos-agent` carries lane V's `reviews`;
  Claude Code 2.1.296), kept. `aos-harness:u4` (5da053c3df7b) and `aos-harness:mvp` (f127d68c236a)
  were not touched.
- Repository: `git clone --no-hardlinks ~/code/aos-example <scratch>/repos/aos-example`, then
  `git remote remove origin` (no remote at all afterwards), at b75f6a6 on `main`.
- Bus, from the worktree with the worktree's `.venv/bin/python -m agent_os serve`:
  `AOS_BIND=127.0.0.1 AOS_PORT=8013 BUS_DATA_DIR=<scratch>/bus-data BUS_TOKEN=<generated, 0600 file>
  TASK_SWEEP_INTERVAL_SECONDS=10`, `TELEGRAM_BOT_TOKEN` unset. It started without it: the log's
  fourth line is `Uvicorn running on http://127.0.0.1:8013`, `/health` 200, listening on
  `127.0.0.1:8013` only (`lsof`). 8010 and 8011 untouched throughout.
- `aos repo set aos-example --host mac --auto-ready-proposals on` answered
  `"auto_ready_proposals": true` (and `"deliver": "push"`, unused: `AOS_PUSH` unset, no remote).
- Shared environment for the CLI, the dispatcher and the runners (one sourced file; the bearer read
  from its file inside each launched shell; every launch `env -i` with PATH and HOME only,
  nohup-detached, the tool sandbox disabled): `AOS_URL=http://127.0.0.1:8013`, `AOS_HOST=mac`,
  `AOS_REPO_ROOT=<scratch>/repos`, `AOS_WORKTREE_ROOT=<scratch>/worktrees`,
  `AOS_OPENSHELL_STATE_DIR=<scratch>/openshell-state`, `AOS_OPENSHELL_PLATFORM=colima-mac`,
  `AOS_OPENSHELL_IMAGE=aos-harness:laneq`, `AOS_OPENSHELL_PROVIDER_CLAUDE=aos-claude-oauth`,
  `PYTHONUNBUFFERED=1`; for the dispatcher `AOS_DISPATCH_MAX_RUNS=2`,
  `AOS_DISPATCH_POLL_SECONDS=10`, `AOS_DISPATCH_LOG_DIR=<scratch>/dispatch-logs`. Harness `claude`,
  model unset (every commit carries `Co-Authored-By: Claude Sonnet 5.5`), `AOS_OPENSHELL_COMMS`
  unset (default 1), `AOS_PUSH` unset.
- Dispatcher: the worktree's `.venv/bin/aos dispatch`; first log line
  `dispatcher on MACs-MacBook-Pro.local (host mac): poll every 10 s, at most 2 live run(s)`. Its
  runners ran as `<python> -P -m agent_os.openshell_runner --token-fd 4 run AO-n` with
  `__PYVENV_LAUNCHER__=<worktree>/.venv/bin/python` and no `AOS_TOKEN` in their environment (`ps
  eww`). D's runner, launched by hand through the worktree's `aos run AO-4`, showed the same plus
  `AOS_OPENSHELL_FRESH_ACCEPTANCE=0`.
- A scratch watcher polled the bus (bearer) and `openshell sandbox list` every 5 s and logged each
  change; the sandbox names below are its lines.

### The briefs, as run

Task AO-1 (P), title "Q P: propose and wait for the notice", acceptance `test -s notes/q-parent.txt`:

```
Propose one task with aos_propose: title 'Q child: a note', brief 'Write the line hello-from-child to notes/q-child.txt and commit.', acceptance ['test -s notes/q-child.txt']. Then wait for the notice that the proposed task's run has finished: run `sleep 30` in Bash between checks, for at most 25 minutes; the notice reaches your inbox as a message from the proposed task. When it arrives, write the proposed task's id and the status the notice reports, on one line, to notes/q-parent.txt and commit.
```

Task AO-2 (C), created by P's `aos_propose`, title "Q child: a note", acceptance
`test -s notes/q-child.txt`, brief as stored: `Write the line hello-from-child to notes/q-child.txt
and commit.`

Task AO-3 (R), title "Q R: review the child", `--reviews AO-2`, acceptance in order
`test -s notes/q-review.txt`, `grep -qx hello-from-child notes/q-review.txt`,
`git rev-parse --verify main`:

```
Your checkout also carries the branch ao/AO-2 of the task you review. Without checking it out, write the content of notes/q-child.txt on that branch (git show ao/AO-2:notes/q-child.txt) to notes/q-review.txt, and the output of `git for-each-ref --format='%(refname)'` to notes/q-refs.txt. Commit both on the current branch.
```

Task AO-4 (D), title "Q D: a message that needs no reply", acceptance in order
`test -s notes/q-stop.txt`, `cat /tmp/aos-agent/*.jsonl`. The coordinator struck "or aos_inbox"
from the brief's prohibition before the run (the runner's messaging paragraph says "Call aos_inbox
before you commit", and two contradicting instructions would test the model's tie-breaking, not
the Stop hook):

```
Run `sleep 45` in Bash, three times in a row. The operator will send you a message meanwhile; it needs no reply, and you must not call aos_reply. After the third sleep, write the line done to notes/q-stop.txt and commit. End your work with a final report whose last line is exactly REPORT-END.
```

The operator note, posted 30 s after D's run was `running`: `POST /tasks/operator/intents
{"to_task_id": "AO-4", "body": "Operator note: no reply needed. Code word STOP-7413a8."}` (the code
word drawn at post time).

## Timeline

| Time (UTC) | Event (source) |
|---|---|
| 14:30:44 | dispatcher started (dispatch log) |
| 14:30:59 | AO-1 `ready` (watcher) |
| 14:31:04 | `AO-1: runner started, pid 65597` (dispatch log); 14:31:09 sandbox `a1a-ao-1-r1` (watcher) |
| 14:31:16 | `POST /tasks/AO-1/proposals` 200 (bus log); AO-2 created `ready`, `proposed_by` AO-1 (runs.jsonl `task:proposed`) |
| 14:31:24 | `AO-2: runner started, pid 66584` (dispatch log); 14:31:30 sandbox `a1a-ao-2-r1` |
| 14:31:50 | AO-2-r1 at acceptance; sandbox `a1a-ao-2-r1-acc` |
| 14:31:55 | AO-2-r1 `review`/pass; AO-2-i1 (AO-2 -> AO-1, request) `stored`; both AO-2 sandboxes gone |
| 14:32:20 | AO-2-i1 delivered via `inbox` and acknowledged (P's hook; row fields) |
| 14:32:36 | AO-1-i1 (AO-1 -> AO-2, reply_to AO-2-i1) `stored`; AO-2-i1 `answered` |
| 14:32:46 | AO-1-r1 at acceptance; sandbox `a1a-ao-1-r1-acc` |
| 14:32:52 | AO-1-r1 `review`/pass (run row); 14:32:56 both AO-1 sandboxes gone |
| 14:34:03 | AO-3 created (`reviews` AO-2); 14:34:07 readied |
| 14:34:15 | `AO-3: runner started, pid 74897`; 14:34:18 sandbox `a1a-ao-3-r1` |
| 14:34:44 | sandbox `a1a-ao-3-r1-acc` |
| 14:34:45 | AO-3-r1 `review`/pass; 14:34:49 both AO-3 sandboxes gone |
| 14:35:16 | dispatcher stopped by SIGTERM: `dispatcher stopping; 0 runner(s) keep running` |
| 14:35:46 | AO-4 readied; `aos run AO-4` launched by hand; `claimed AO-4-r1` |
| 14:35:48 | AO-4-r1 `running` (poster); 14:35:50 sandbox `a1a-ao-4-r1` |
| 14:35:54 | D's first ledger `hook` event (before the note existed) |
| 14:36:18 | operator-i1 posted (bearer), state `stored` |
| 14:36:39 | ledger `hook`, then `acked` operator-i1; the bus row delivered via `inbox` and acknowledged the same second |
| 14:37:27, 14:38:13, 14:38:18 | ledger `hook` events; then `stop_blocked` |
| 14:38:32 | AO-4-r1 `review`/pass, `acceptance 2/2 pass`; 14:38:34 sandbox gone |

## Part 1: a proposal, its run, and the finish notice reaching the parent

**Q1. The proposal exists with `proposed_by` = P and was created `ready`, with P's repo, harness and
tier: yes.** `aos show AO-2`: `repo: aos-example`, `tier: 1`, `harness: claude`, `host: mac`,
`proposed_by: AO-1` (P's row: aos-example, tier 1, claude, mac). The watcher's first line for AO-2
is `14:31:20Z task AO-2 ready proposed_by=AO-1`; the bus's runs.jsonl:
`{"outcome": "task:proposed", "receipt": {"task_id": "AO-1", "run_id": "AO-1-r1", "proposal_id":
"AO-2", "status": "ready", "event": "task:proposed"}, "needs_user": false}`.

**Q2. The dispatcher started C's run while P was live: yes.** Dispatch log:

```
2026-10-15T14:31:04Z AO-1: runner started, pid 65597, log <scratch>/dispatch-logs/AO-1-20261015T143104Z.log
2026-10-15T14:31:24Z AO-2: runner started, pid 66584, log <scratch>/dispatch-logs/AO-2-20261015T143124Z.log
2026-10-15T14:32:04Z AO-2: runner exited 0; task review (...)
2026-10-15T14:32:55Z AO-1: runner exited 0; task review (...)
```

Run rows: AO-1-r1 started 14:31:04Z, ended 14:32:52Z; AO-2-r1 started 14:31:25Z, ended 14:31:55Z.

**Q3. C ended review with `notes/q-child.txt` on `ao/AO-2`: yes.** `git show
ao/AO-2:notes/q-child.txt` in the clone prints `hello-from-child`; commit `060dbc1 Add
notes/q-child.txt note from child task AO-2`. AO-2-r1 `review`, verdict `pass`, acceptance
`$ test -s notes/q-child.txt … [exit 0] (fresh sandbox a1a-ao-2-r1-acc)`. Its prompt.txt carries
`This task was proposed by task AO-1, an agent, not by the operator.`

**Q4. The finish-notice row, from C to P, a request naming status, branch and commit: yes.**
Bearer `GET /tasks/AO-1/intents`: `id AO-2-i1, task_id AO-2, run_id AO-2-r1, kind request,
to_task_id AO-1, reply_to None, created_at 14:31:55Z` (the second AO-2-r1 ended). Body (598
characters, cut here at 200):

```
task AO-2 finished: review
branch ao/AO-2
commit 060dbc1ea99e94d31e6cabb9d4ee5ba82fc63f56
report: sandbox prefix a1a for bus 127.0.0.1:8013
policy main@b75f6a67d980 rendered for colima-mac; job copy 1
```

The report excerpt is the first 500 characters of the run's `report` field, which opens with the
runner's notes lines, so the agent's own text gets about 330 characters and is cut mid-sentence
(`… Because no run direct`).

**Q5. P received it, wrote C's id and status, committed, ended review: yes.** The row was delivered
via `inbox` and acknowledged at 14:32:20Z (bus log: `POST /runs/AO-1-r1/requests` then `POST
/intents/AO-2-i1/ack`). P's report: "How the notice arrived: it came as a hook message after a
`sleep 30`, not through `aos_inbox`, so the inbox stayed empty." `git show
ao/AO-1:notes/q-parent.txt` prints `AO-2 finished: review`; commit `27a2753 Record AO-2 notice
status in notes/q-parent.txt`; AO-1-r1 `review`, verdict `pass`, acceptance `$ test -s
notes/q-parent.txt … [exit 0] (fresh sandbox a1a-ao-1-r1-acc)`, harness 97 s.

**Q6. Every sandbox carried the prefix `a1a`, and the state directory holds only `a1a-` entries with
no marker left: yes.** Sandboxes seen during the runs (watcher): `a1a-ao-1-r1`, `a1a-ao-2-r1`,
`a1a-ao-2-r1-acc`, `a1a-ao-1-r1-acc`, `a1a-ao-3-r1`, `a1a-ao-3-r1-acc`, `a1a-ao-4-r1`; nothing else
appeared on the gateway. Each run's report opens with the notes line `sandbox prefix a1a for bus
127.0.0.1:8013`. `driver.py state-dir --prefix a1a` after all four runs, exit 0:

```
<scratch>/openshell-state: 4 entries
  a1a-ao-1-r1: prefix a1a yes; owner marker absent; files ['accept-policy.yaml', 'aos-mcp.json', 'claude-settings.json', 'policy.yaml', 'prompt.txt']
  a1a-ao-2-r1: prefix a1a yes; owner marker absent; files [... the same five]
  a1a-ao-3-r1: prefix a1a yes; owner marker absent; files [... the same five]
  a1a-ao-4-r1: prefix a1a yes; owner marker absent; files ['aos-mcp.json', 'claude-settings.json', 'policy.yaml', 'prompt.txt']
```

(D ran with acceptance in the job sandbox, so no `accept-policy.yaml`.)

### P's Stop block on the finish notice (beyond the brief)

The notice is a `request` with `reply_to` null, so once P's hook acknowledged it the Stop hook's
open set was not empty (aos_agent.py `_hook_stop`). P's report: "Reply to AO-2: the stop hook
flagged the notice (AO-2-i1) as unanswered. I answered it with `aos_reply`, and that reply was
accepted." The bus shows the reply 16 s after the acknowledgement: `AO-1-i1 AO-1 -> AO-2 stored
reply_to=AO-2-i1 | Notice received. Recorded "AO-2 finished: review" in notes/q-parent.txt on
ao/AO-1.`, and AO-2-i1 `answered` at 14:32:36Z (the bus log's second `POST /intents/AO-2-i1/ack` is
`aos_reply`'s own ack before it replies, aos_agent.py 284). P's posted report is a full final
report (the six points quoted above and below come from it), so it survived the block. AO-1-i1 stays
`stored`: AO-2 has no live run, and nothing reads it. P's in-sandbox ledger was not kept (P ran
with fresh acceptance and its sandbox was deleted), so the block is attested by P's report and the
reply's timing only.

## Part 3: a review task reads the reviewed branch (lane V live)

Before R ran: `ao/AO-2 recorded: 060dbc1ea99e94d31e6cabb9d4ee5ba82fc63f56`. The source clone then
held `refs/heads/ao/AO-1 27a2753`, `refs/heads/ao/AO-2 060dbc1`, `refs/heads/main b75f6a6`.

**V1. R's row carries `reviews` = C; an unknown id refuses: yes.** `aos show AO-3`: `reviews:
AO-2`, `proposed_by: None`. One call with an unknown id:

```
$ aos add --repo aos-example --title "Q R: unknown reviewed task" --reviews AO-99 --brief-file R.txt --accept true --harness claude --host mac
reviews: no task AO-99
rc=1
```

`aos list` afterwards showed AO-1 to AO-3 only (no task was created).

**V2. R's prompt carries the review line: yes.** `driver.py state-dir --grep "This task reviews"`:
`a1a-ao-3-r1 … prompt.txt: This task reviews task AO-2: its branch ao/AO-2 is in your checkout to
read; commit your own work on the current branch.`

**V3. `notes/q-refs.txt` lists exactly the three refs: yes.** `git show ao/AO-3:notes/q-refs.txt`:

```
refs/heads/ao/AO-2
refs/heads/ao/AO-3
refs/heads/main
```

No `ao/AO-1` (it was in the source clone since 14:32:52Z; R's copy was built at about 14:34:15Z),
no `ao/AO-4` (it did not exist yet), no `refs/remotes/`. `git show ao/AO-3:notes/q-review.txt`
prints `hello-from-child`.

**V4. `ao/AO-2` unchanged; `main..ao/AO-3` holds only R's commits: yes.** `ao/AO-2 now:
060dbc1ea99e94d31e6cabb9d4ee5ba82fc63f56` (= recorded). `git log main..ao/AO-3`: one commit,
`51499a6 Add review notes: child content and ref list`, parent `b75f6a6` (= main).

**V5. R ended review with acceptance passed in the fresh sandbox: yes.**

```
$ test -s notes/q-review.txt

[exit 0] (fresh sandbox a1a-ao-3-r1-acc)
$ grep -qx hello-from-child notes/q-review.txt

[exit 0] (fresh sandbox a1a-ao-3-r1-acc)
$ git rev-parse --verify main
b75f6a67d980c7c21aa3184dcb21dae2fd2d5abe
[exit 0] (fresh sandbox a1a-ao-3-r1-acc)
```

## Part 2: a Stop block with an open request, and the report after it

Run last, after Part 3, with the dispatcher stopped (14:35:16Z) and D launched by hand with
`AOS_OPENSHELL_FRESH_ACCEPTANCE=0`. One attempt; no retry was needed.

**S1. The operator row ends `acknowledged`, never `answered`: yes.** Ledger: `operator-i1 operator
-> AO-4 acknowledged reply_to=None created=2026-10-15T14:36:18Z answered=None | Operator note: no
reply needed. Code word STOP-7413a8.`; row fields `delivered_via inbox, delivered_at 14:36:39Z,
acked_at 14:36:39Z, answered_at None`. No row from AO-4 exists.

**S2. The posted `acceptance_output` carries the ledger with `"event":"acked"` for that row and
`"event":"stop_blocked"`: yes** (the ledger is compact JSON, aos_agent.py 161, so there is no space
after the colon; the coordinator accepted that reading). The second acceptance command's output as
posted:

```
$ cat /tmp/aos-agent/*.jsonl
{"event":"hook","at":1792074954}
{"event":"hook","at":1792074999}
{"event":"acked","id":"operator-i1","from":"operator","reply_to":null,"body":"Operator note: no reply needed. Code word STOP-7413a8."}
{"event":"hook","at":1792075047}
{"event":"hook","at":1792075093}
{"event":"hook","at":1792075098}
{"event":"stop_blocked"}

[exit 0]
```

The `hook` times are 14:35:54, 14:36:39, 14:37:27, 14:38:13 and 14:38:18 UTC. The note was posted at
14:36:18 and acked by the hook that fired at 14:36:39, when the first `sleep 45` ended (the hook
writes `hook`, then acks and writes `acked`; the bus row's `acked_at` is the same second). D's report
says it did not call `aos_inbox`, so the hook path acknowledged the note (the ruling asked which path
did).

**S3. The posted `report` contains REPORT-END and says why no reply was sent; `notes/q-stop.txt` is
on D's branch: yes.** The `report` field after the runner's notes lines:

```
claude final message:
Final report for AO-4.

I ran `sleep 45` three times in Bash, wrote `done` to `notes/q-stop.txt`, and committed it as b59f31f on `ao/AO-4`.

- **Operator message operator-i1:** it arrived during the first sleep. It said no reply was needed ("Code word STOP-7413a8"), and the task told me not to call `aos_reply`. I did not reply. The stop hook asks for either a reply or a reason, and this is the reason.
- **Acceptance:** `test -s notes/q-stop.txt` passes. `cat /tmp/aos-agent/*.jsonl` printed the hook events and the acknowledgement of operator-i1. I only looked at the last five lines of that output.
- **Commit identity:** the first commit attempt failed because git had no author identity. [...]
- **Verify skill:** [...]
- **Inbox:** I didn't call `aos_inbox` before committing.

REPORT-END
```

It is the restated report the lane B Stop reason asks for ("Then end with your full final report
again"): it opens "Final report for AO-4." and names the block. `git show ao/AO-4:notes/q-stop.txt`
prints `done`; commit `b59f31f Add notes/q-stop.txt`.

**S4. D's run ended review with acceptance passed: yes.** AO-4-r1 `review`, verdict `pass`, runner
output `acceptance 2/2 pass` / `result review`, harness 163 s, acceptance in the job sandbox
(no `-acc` sandbox, no `fresh sandbox` suffix on the output lines).

## The claim token appears nowhere: yes (0 hits, controls found)

`driver.py token-scan` read the four runs' claim tokens from the bus database (in memory; 43
characters each) and counted them in each task's bus rows (runs, ledger), `git log -p
main..ao/<task>` for the four branches, the clone's whole object store (354,411 bytes), every file
under the runner state directory and the dispatch log directory, the dispatcher's, D's runner's,
the bus's, the watcher's and the poster's logs, and the bus's runs.jsonl. Token hits: 0 in every
source (exit 0). Control `STOP-7413a8`: 2 in AO-4's run rows, 2 in AO-4's ledger, 2 in the watcher
log and 2 in the poster log, 0 in git, where it was never committed. A second scan with the control
`hello-from-child`, for the git sources: 1 in `git log -p main..ao/AO-2`, 1 in `main..ao/AO-3`, 1 in
the object store, tokens again 0. `harness_tail` was empty for all four runs (every harness exited
0).

## Network lines (from the reports)

AO-1-r1: `192.168.5.2:8013 x12, api.anthropic.com:443 x3, denied x2`; AO-2-r1: `x3, x3, x0`;
AO-3-r1: `x2, x3, x0`; AO-4-r1: `x6, x3, x2`. Every run: "effective policy matches the rendering".
The two runs with `denied x2` are the two that hit a Stop block; the runner records only a count
(`hosts_contacted` counts `transparent_tcp_policy_denied|transparent_tcp_mapping_denied` lines), and
`openshell logs a1a-ao-4-r1` after the run answers `sandbox not found`, so the denied hosts are
unknown. The correlation with the Stop block is inferred from two runs, unverified.

## Observations

- Git identity: all four agents report that their first commit failed for want of an author
  identity and committed with a one-off `-c user.name=… -c user.email=…`. No identity is set at
  fbb79d1 or b38a197 (`git grep user.name|GIT_AUTHOR` in the runner and the harness directory finds
  nothing at either), and the image has no `/etc/gitconfig` or `~/.gitconfig`, so this predates lane
  V (inferred, unverified: the U4 record does not say what its agents did).
- The agent frame tells the receiver to "Acknowledge with `aos ack <id>`" (agent_os/store.py 163);
  the image has no `aos` on PATH. The hook and the `aos` MCP tools acknowledge instead.
- The runner's messaging paragraph ends "Call aos_inbox before you commit." C did call it, R and D
  did not, and each said so in its report.
- `aos_reply` acknowledges the row before it replies (aos_agent.py 284), so a reply after a hook ack
  shows a second ack call on the bus.
- The whole lane took 7 min 33 s of wall time from P's ready (14:30:59Z) to D's result
  (14:38:32Z): 97, 20, 21 and 163 s of harness time.

## Limits

- P's in-sandbox ledger and the stream-json of every run were not kept (the runner deletes the
  sandbox; `sandbox download` cannot fetch the ledger), so P's Stop block rests on P's report and the
  reply's timing; D's rests on its own ledger, posted by acceptance.
- One run per part at the harness default model (Sonnet 5.5), no repetition; cost fields are not
  posted by the runner.
- Nothing went to Telegram (no bot configured); the operator note went through the bearer route.
- The two denied connections in P and D are counted, not named.

## G2 measurement

Authorized by the coordinator after booking (run 5 of 6): which hosts were behind `denied x2` in the
two Stop-blocked runs, and do the denials come before or after the block.

Setup: a fresh 127.0.0.1:8013 bus with a new generated token, and the kit's new `driver.py stopblock`,
not the runner. It is built from the runner's own pieces: `settings_from` with this lane's environment;
the default-branch policy through `render_for`, byte-identical to the kit's documented CLI rendering
(`kit_cli_rendering_identical_to_render_for: true`); `provision_job` with `main`; `main_script` with
comms on and the model unset; `claude_settings(True)`; `AOS_MCP_CONFIG`; `build_prompt` with the verify
line as the runner computes it for aos-example and no evidence line; the run file through `bus_url`,
mode 0600; the runner's create call (provider `aos-claude-oauth`, `DEFAULT_ENV`, 2 CPU, 2Gi); the name
from `sandbox_name` (`a1a-ao-1-r1` on the fresh bus); and the runner's upload order. The task used
Part 2's brief and acceptance, the note went out 30 s after the run start, and acceptance ran in the job
sandbox. The gateway log was read twice (after the harness, after acceptance) before the sandbox was
deleted. Differences from the runner: no heartbeat, no bundle or result post, and the ledger was read by
exec every 2 s (logged as `SSH:OPEN ALLOWED`, never as `NET`).

| Time (UTC) | Event (source) |
|---|---|
| 14:49:52.2 | harness started (`/sandbox/harness.started`) |
| 14:49:52.518 | `CONFIG:PUBLISHED Policy DNS mapped api.anthropic.com resolved=160.79.104.10 synthetic=198.18.0.2 ports=443` |
| **14:49:53.709, 14:49:53.711** | **DENIED ×2**: `/usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe(0) -> 198.18.0.2:443 [reason:transparent_tcp_mapping_denied]`, each after the supervisor's `Denied staged transparent connection destination=198.18.0.2:443 error=transparent destination mapping is unavailable: transparent TCP mapping is expired` |
| 14:49:54.294 / .322 | a new mapping for api.anthropic.com, then `claude.exe -> api.anthropic.com:443` ALLOWED; `policy_limits`, `settings` and `/v1/messages` follow |
| 14:50:20.7 | operator-i1 posted (code word STOP-0ad9a8) |
| 14:50:42 | ledger `hook`, then `acked` operator-i1 (bus row: delivered via `inbox`, `acked_at` 14:50:42Z, `answered_at` null) |
| 14:51:31, 14:52:18 | ledger `hook` events (the end of the second and third sleeps) |
| **14:52:20.665** | **DENIED ×1**: `NET:REFUSE [MED] DENIED 2dadbb04735d [reason:policy_dns_ineligible]`, with `Policy DNS staged unapproved name 2dadbb04735d`; no binary named |
| 14:52:27.8 | ledger `stop_blocked` first seen (2 s poll) |
| 14:52:31.0 | `/sandbox/harness.rc` written, rc 0 |

The gateway log holds exactly those three DENIED lines in both reads. The runner's `hosts_contacted`
pattern counts 2 of them (the two `transparent_tcp_mapping_denied`), with `claude.exe ->
api.anthropic.com:443` ×3 and `192.168.5.2:8013` ×4 allowed: the same shape as P's and D's network lines.
The reproduction matches Part 2: the hook acknowledged the note, no reply was sent, `stop_blocked` is in
the ledger, and the final message restates the report and ends `REPORT-END`.

Reading:

- The two counted denials come at the harness's start, before the block: 49 s before the
  acknowledgement and about 154 s before `stop_blocked`. They are Claude Code's first two connections to
  api.anthropic.com's synthetic address, refused because the policy DNS mapping had expired 1.2 s after it
  was published. The retry 0.6 s later went through on a new mapping. The Stop hook does not cause them;
  the Stop-block correlation across lane Q's four runs was not causal. Why C and R (and U4's three runs)
  counted 0 is not measured; a timing race between the mapping and the first connect is inferred,
  unverified.
- The third line is not counted by the runner. It falls 2.7 s after the third sleep ended, when the
  agent's first `git commit` failed for want of an identity (its report says so). A model-free check in a
  sleeping kit sandbox under the same policy: `hostname` printed `0f5d269ce242`; `git commit` with no
  identity exited 128 with `fatal: unable to auto-detect email address (got
  'sandbox@0f5d269ce242.(none)')`, and in the same millisecond the gateway logged `NET:REFUSE [MED]
  DENIED 0f5d269ce242 [reason:policy_dns_ineligible]`. So the line is git looking up the sandbox's own
  hostname for an auto-detected email (G3). The G2 run's name `2dadbb04735d` has the same shape; its
  sandbox is gone, so that it was the hostname is inferred.
- Limits: one run, so the startup race's frequency is unmeasured; claim token 0 hits in every G2 artifact
  (`token-scan`, control STOP-0ad9a8 found in the ledger, final message and logs).
