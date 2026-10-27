# CLI reference

[Documentation index](../README.md) · [Configuration](configuration.md)

The installed `aos` command talks to the bus. Set `AOS_URL` and `AOS_TOKEN` for
operator commands. New runner sessions use `AOS_RUN_CREDENTIAL`; the worktree's
`.aos/run.json` contains an identity/pointer, with legacy inline records supported.
Commands that launch a harness or integration check re-exec with a clean initial
environment, passing the operator token through a private pipe before closing it.
Use `aos --help` and `aos <command> --help` for parser flags.

## Command map

| Commands | Purpose |
|---|---|
| `add`, `edit`, `list`, `show` | Create and inspect task specifications; `show` includes the family block |
| `ready`, `unready`, `cancel`, `requeue`, `accept` | Operator task transitions |
| `review`, `reject` | Read a run's captured evidence; refuse the newest review run ([Reviewing a run](operations.md#reviewing-a-run)) |
| `repo list`, `repo set`, `repo rm` | Repository defaults; `set` requires `--host`; `--deliver push\|pr` and `--auto-ready-proposals on\|off` keep their stored value when omitted; test commands belong in committed `aos-workflow.toml` |
| `run` | Run a ready task in OpenShell; there is no `--dry-run` |
| `attach` | Attach the terminal to an interactive run's session; `--takeover` detaches another client |
| `family` | Show a task's children: attempt, status, authorization or capacity wait, result links; `--follow` reprints every `--interval` seconds until the parent is terminal |
| `dispatch` | Start a runner for each ready task this host may take, every poll; `--once` polls once ([Dispatch](#dispatch)) |
| `own -- <command>` | Register and launch an exclusively run-owned process; requires the runner's environment, no bus token |
| `cleanup` | Sweep terminal-run compute across local tasks using the host registry and bus state |
| `integrate`, `merge` | Check the combined result, then merge the PR |
| `ask`, `requests`, `ack` | Pull-based agent requests and replies; `aos ask --operator` sends the operator's message or reply |
| `scope`, `scopes`, `depends`, `block`, `assess`, `retract`, `intents` | Declarations, dependencies and overlap findings |
| `interactions` | Model-written interaction review of two review tasks |
| `sp poll` | Read Super Productivity's sync files once and apply them to the bus ([operations](operations.md#super-productivity-source)) |

`add` and `edit` accept `--brief-file`, repeatable `--accept`, `--tier`,
`--harness`, `--host` and `--execution headless|interactive`. `add` alone takes `--reviews AO-<m>`: the new task reviews
task AO-<m> of the same repository (another repository or an unknown id refuses, and the
field never changes afterwards), its run's job copy also carries `ao/AO-<m>` to read, and
its prompt names that branch. Readiness requires a nonempty brief, not explicit
commands or repository validation. Only tiers 0–2 can run; there is no tier-3 confirmation override. `requeue --ready`
returns directly to ready; dependency warnings require an explicit `--force`
override. `integrate --keep` retains its scratch worktree for inspection.

`python -m agent_os serve` is a separate local entry point that starts the bus. It
reads bus settings and local storage, rather than using `AOS_URL`/`AOS_TOKEN` as a
remote client. `python -m agent_os.sp_sync auth` is the
one-time Dropbox authorization for the Super Productivity source; it needs no bus.

## Run a task

An OpenShell task with `--execution interactive` needs the `omp` flavour of the `pi`
harness and messaging enabled. The runner prints `aos attach <run-id>`: run it on a
host that reaches the gateway, from a Herdr pane or any terminal, to join that run's
terminal session. Reattaching starts nothing new; closing the client leaves the run
and its deadline alone. The agent commits and calls `mcp__aos_submit(report, commit,
children)` to request verification of exactly that commit. `/exit` without a
submission fails the task. `--takeover` detaches an existing client before attaching;
a second plain attach is refused with `already has an attached client`, and a run
that is not live is refused (`is review, not live`, `is cancelled, not live`,
`is blocked, not live`). Text typed while the agent sits in `mcp__aos_inbox_wait`
queues as omp steering until the wait returns; Esc aborts the wait, and `aos ask
<task> "<text>" --operator` ends it with the message as its result.

`aos show <parent>` prints `family: <n> children (parallel capacity <k>; ...)`
after the runs when the parent has children, followed by one `child AO-<m>: ...`
line per child: task status, attempt, run id and status/stage, waiting reason, branch,
commit, question count, import state and result links (remote, branch URL, PR and
merge commit when present). A backlog child shows `waiting=authorization`; a ready
child without a live run shows `waiting=capacity`. `import=stale` means the child
was requeued or its head moved since the import. Use
`aos family <parent> [--follow] [--interval 5]` in a neighbouring Herdr pane for
the parent line and the same block, including an empty family. Follow stops at
review, done, failed, blocked or cancelled, or detaches on Ctrl-C, with a resume hint.
Both commands use the operator bearer.

The worker resolves `aos-workflow.toml` at the policy's authoritative default commit
before claiming, equally for manual and dispatched runs. Explicit task checks win
over workflow `acceptance`, then workflow `test_command`; invalid config or no
checks refuses. Claim freezes the effective commands per run; repository prompt
instructions precede runner rules. There is no workflow sync/cache. See
[configuration and legacy migration](configuration.md#repository-workflow-file).

`aos run AO-<n>` runs a ready coding task in an OpenShell sandbox: a job copy of the
repository's default branch goes into the sandbox, the harness runs there, acceptance
runs in a second, credential-free sandbox, and the result comes back as `ao/<task>` in
the source repository. The repository needs a v2 `aos-policy.toml` on its default
branch; the run is refused otherwise. OpenShell is the only backend for both
`aos run` and `aos integrate`; `--backend host` and `AOS_BACKEND=host` are refused.
A run exits 0 only for a `review` result the bus confirmed: when the bus refuses the
result post or cannot be reached, the runner still prints its local verdict (`result
review`), adds `result not delivered: the bus did not confirm the review verdict` on
stderr and exits 1. Set the task's harness with `aos add --harness codex` or
`aos edit AO-<n> --harness codex`; the default remains `claude`.
Harnesses and their configuration are provisioned inside the sandbox, not through
a host Codex daemon or a host runner-prompt file. See the
[worker settings](../getting-started.md#configure-the-runner) for image, worker
credentials, model, messaging and plugin configuration.

Task prompts combine the task brief, committed workflow instructions and runner
rules. Every harness is instructed to commit its changes in the job copy.
Tasks in one repository can run concurrently up to
`TASK_MAX_LIVE_RUNS_PER_REPO` (default 3); their result branches are reviewed and
integrated separately.

## Dispatch

`aos dispatch` is the long-running command of a worker host (task-flow Part B): every
`AOS_DISPATCH_POLL_SECONDS` it lists the `ready` tasks whose host is `AOS_HOST` or `any`,
leaves out tier 3 and tasks on cooldown, and, while fewer than `AOS_DISPATCH_MAX_RUNS`
runs are live for this host, starts one runner per task in task-number order, exactly
`python -P -m agent_os.openshell_runner --token-fd <fd> run <task>` with the bus token
on a pipe, `AOS_TOKEN` absent from the runner's environment, its own session and its
output in `<AOS_DISPATCH_LOG_DIR>/<task>-<UTC stamp>.log`. It reads `AOS_TOKEN` once at
start and removes it from its own environment. No dispatch key is passed: the bus's
claim rule (one live run per task, the per-repo cap, host, tier) stays the only
authority, and a runner that hits a conflict exits at once. It never requeues a failed
or blocked task (`aos requeue --ready` stays the operator's), never merges, never
pushes. `aos dispatch --once` runs one poll and returns; the runners it started keep
running. See [the dispatcher on a worker host](operations.md#dispatcher-on-a-worker-host)
for the cooldown rule, restart safety and the server setup.

## Requests and credentials

Launch detached servers and dedicated browsers with `aos own -- <command>`
(for example, `aos own -- npm run serve`). The wrapper records PID and creation
time before replacing itself with the command. This also supports binaries whose
environment is hidden by macOS. Ownership ends after acceptance, including failed,
timed-out or cancelled runs. Do not wrap shared daemons or shared browser sessions.
The runner periodically records observed descendants, including session-detached
children, so their identity remains known after the parent exits. This is host tracking,
not an enforceable boundary; rapid fork/reparent before observation can escape it.
Inherited run tags also identify processes automatically when the OS exposes them.
See [resource cleanup](operations.md#run-resource-cleanup) for containers and limits.

`aos cleanup` checks all local ownership records against the bus and cleans terminal
runs without requiring a task retry. It leaves live runs, locked workspaces, live
runner identities and unconfirmed ownership alone. Bus lookup failures leave compute
untouched and return a nonzero exit code. The sweep never prunes caches or removes
worktrees. Run it periodically with the same `AOS_URL`, `AOS_WORKTREE_ROOT` and
`AOS_RESOURCE_ROOT` as the runner if unattended reconciliation is needed.

Other agents may send a task's running session a request. `aos ask AO-<n>
"<text>"` sends one; `aos requests` lists what's pending for the run, each message
followed by the line ``Acknowledge with `aos ack <id>`.`` (the bus's frame names no
command, since it also reaches sandboxes, where aos-agent acknowledges); `aos ack
<id>` acknowledges one; `aos ask --reply <id> "<text>"` answers it, and a
reply needs the request acknowledged or delivered first. The operator sends from
any terminal with the bus bearer: `aos ask AO-<n> "<text>" --operator` posts to the
task's next inbox pull and prints the intent id and its ledger state (`stored` until a
run pulls it, through the inbox hook, `aos requests` or the MCP inbox tool, with a
second line when the task has no live run);
`aos ask <id> "<text>" --reply --operator` answers a question addressed to the
operator. Neither needs a worktree or run credential; nothing in the output claims
the agent acted on the message. Nothing
pushes a request into a session: an agent sees one only when it next runs
`aos requests`, which the runner's prompt tells it to do at the start, before
every commit, and before finishing; the operator is told on Telegram about
every request still unacknowledged after `AOS_REQUEST_ACK_SECONDS`. The operator
is a receiver too: a request with `to_task_id` `operator` is posted to the
needs-you chat on the next sweep tick as `❓ AO-<n> asks you (<id>)`, and a
Telegram text reply to that notice answers it (the bus confirms with
`answered <id> (operator-i<k>)`). The answer reaches the asker's inbox framed as
`<operator-message id="operator-i<k>" to-task="AO-<n>" reply-to="<id>">` with the
line "This is from the operator."; an agent body containing that tag is refused
like the agent frames. The operator can also write through the API alone with the
bearer, `POST /tasks/operator/intents` with `{to_task_id, body}` or `{reply_to,
body}`, which is what `aos ask --operator` sends. Telegram is the one operator channel today; another
messenger implements the two methods of `agent_os/operator_channel.py`. An agent waits for an answer with
`POST /intents/<id>/wait` (its run token, `timeout` 0 to 60 seconds, response
`{"intent", "replies"}`), returning as soon as the request is answered. These
verbs read the run's credential from its private resource directory. The runner
writes a secret-free pointer in `.aos/run.json` before the harness starts and
removes both records once the harness stage ends, so `aos`
must be on the harness's PATH; no file is written when `.aos` is a symbolic
link, and the file stays when the runner learns that the bus ended the run
before it could remove it. The runner does not push the task branch when a
commit reachable from that branch or from the worktree's HEAD, merged side
branches included, touched the top-level `.aos` in any letter case; this
guards against a session committing the directory by accident.

A live run can also propose work for other runs in its own repository (agents that
talk, U7): `POST /tasks/AO-<n>/proposals` with its run token and `{title, brief,
acceptance, constraints, reviews, request_id, base}` creates a headless task that inherits
the proposer's repo, harness, tier and host, carries `proposed_by AO-<n>` (shown by `aos show`), and waits in the
backlog for the operator unless the repository's `--auto-ready-proposals on` switch
starts it ready; at most ten proposals of one task may be open at once, and the texts
are refused when they carry a frame tag, since the brief becomes another agent's
prompt. The optional `request_id` is 1–64 characters of letters, digits, `_ . : -`;
a repeat with the same identity from the same parent task returns the task already
created with `repeated: true` and creates nothing. The optional `base` is a
40-character commit the parent exported as a candidate; when omitted, the child
starts from the parent run's base commit. Its job copy is provisioned at that
recorded commit, never at the source checkout's HEAD. Proposals from a parent in
one repository never create a child in another, and `aos edit --repo` refuses a task
that belongs to a family. The optional `reviews` (the `aos_propose`
tool sends it when given) names a task of the proposer's repository the new task
reviews, as `aos add --reviews` does. When a proposed task's run ends in
`review`, `failed` or `blocked`, the bus writes one notice from the child to the
parent task, a row of kind `notice` (`task AO-<m> finished: <status>`, the branch and
commit when known, a 500-character excerpt, withheld when it carries a frame tag). For
`review` the excerpt is the harness's final text when the runner posted it as progress,
else the report; for `failed` and `blocked` it is the error or the report. It is framed
`<agent-notice id="<id>" from-task="AO-<m>" from-run="<run>" to-task="AO-<n>">` with a
line saying it is from the bus and needs no answer: a reply to it is refused (409 `a
notice needs no answer`), it cannot be retracted, and an agent's Stop hook does not
count it. So a coordinator run sees its children finish in its inbox, and the operator
sees it on `GET /tasks/AO-<n>/intents` and, when nobody acknowledges it within
`AOS_REQUEST_ACK_SECONDS`, in the needs-you chat as `⚠️ notice <id> …`.
The sandbox receives only the current run's messaging credential, not the operator
bus token. Messaging readiness checks authenticated bus access from the sandbox;
a reachable host CLI or a registered MCP tool list alone is insufficient.
`AOS_OPENSHELL_COMMS=pull` retains the tools and credential without automatic
delivery or the final-turn hold; `0` omits the messaging wiring.

## Integration and merge checks

Integration requires a v2 policy on the authoritative default branch. It executes
checks in a fresh OpenShell acceptance sandbox, not on the host. Missing policy,
invalid workflow or unavailable sandbox prerequisites produce an error rather than
falling back to unconfined execution.

`aos integrate <task id>` merges the task's current PR head (or, before a PR
opens, its accepted commit) onto the repo's current base branch in a scratch
worktree, runs the saved run checks and current authoritative workflow `test_command`,
and reports the result to the bus; it never pushes, never commits outside the
scratch worktree, and never touches the task's own worktree, branch, or the
source's checked-out branch. The branch tested is the PR's own base once a PR
is open; without one it is the remote's default branch; without a remote it
is the source's checked-out branch. Missing run snapshots, invalid workflow files or
failed authoritative configuration lookup end the check in `error`, not a zero-check
success. Exact duplicate commands are run once, preserving order. With
`CODE_INTEGRATION_CHECK` at its default `require`, Merge refuses unless a
check for exactly the PR head that is current is on record, that check
passed, that check ran onto the branch the PR actually merges into, and the
repo's base branch has not moved since the check ran; `CODE_INTEGRATION_CHECK=off`
restores the old behavior. Merge reads the PR's base from GitHub itself on
every press, not once: a PR retargeted on GitHub after a passing check is
refused the same as no check at all, and the newly read base replaces
whatever was stored, an earlier empty one included. Before the first merge
attempt for a PR opened before this repair, with no base recorded yet, `aos
integrate` tests it onto the remote's default branch, same as any PR without
one; the first merge attempt reads and records the real base, and a check that ran onto another
branch is then refused until `aos integrate` has run again.
Repository test commands are configured in committed `aos-workflow.toml`, never
with registry `--test`; migrate legacy SQLite values before rollout as described
in [configuration](configuration.md#migrate-registry-test-commands-before-rollout).
`aos repo set <name> --host <mac|server|any> --deliver pr` opts the repo into pull requests once the
runner has pushed a run's branch ([delivery](operations.md#delivery-to-github));
the default `push` leaves the pushed branch for review and `aos accept` closes
the task. `--auto-ready-proposals on|off` decides whether tasks proposed by a run
in this repo start ready or wait in the backlog (the default); omitted, like
`--deliver`, keeps the stored value. `aos repo list` prints name, host,
note and delivery. `aos integrate` only detects: a failed, conflicting, or
errored check fixes nothing by itself, and the task is requeued (`aos
requeue`) or fixed by hand.

`aos integrate <task id> [--backend openshell]` runs merged checks only in OpenShell.
`AOS_BACKEND` is shared with `aos run`; any value other than `openshell` is refused.
The merge still happens on the host, in the scratch worktree; the
merged commit is then cloned into a fresh, credential-free sandbox `<prefix>-<task>-int`
from `AOS_OPENSHELL_IMAGE` under the repository's acceptance policy (its grants and
the bus only), where the saved run checks and current workflow `test_command` run
exactly as a run's fresh acceptance does, one exit code and output tail per command.
This is the backend on which a browser check can pass on a Mac. A repository that
requests `[postgres]` in `.aos/services.toml` gets a database of its own for the
attempt behind an exact network grant, handed over as `DATABASE_URL`. The default
branch must carry a v2 `aos-policy.toml`. A missing or v1 policy, missing adapter
settings (`AOS_OPENSHELL_PLATFORM`, `AOS_OPENSHELL_IMAGE`), an unreachable gateway, a
sandbox name already in use, a surviving ownership marker of an earlier attempt of the
same task (left for `reconcile`), a task id whose slug exceeds 11 characters (the
19-character sandbox-name cap), a refused services file or a timeout end the check in
`error`: the environment refused, not the candidate. The verdict is posted before the
sandbox and the database are removed; a cleanup failure is printed under
`integration cleanup:`, never changes the verdict, and is retried by `reconcile`.
`--keep` retains the scratch worktree only; the sandbox and the database always go
with the attempt. Setup and the ownership marker:
[OpenShell backend setup](openshell-backend-setup.md#integration-checks).

Each invocation reserves a distinct scratch worktree under
`$AOS_WORKTREE_ROOT/<repo>/.integrate-<task>-<suffix>`. Starting another check never
deletes an active or previously kept worktree. `--keep` prints the exact retained
path. OpenShell additionally holds a local same-task ownership lock from before
the marker check through verdict posting and cleanup. A competing invocation is
refused before touching shared services or sandbox state; unique host scratch paths
do not permit two owners of one sandbox. See the shared-state-root requirements in
[backend setup](openshell-backend-setup.md#integration-checks).

SIGTERM uses the normal teardown path on both backends. On the host this kills the
acceptance process group, removes the attempt's database, and removes its scratch
unless `--keep` was requested. SIGKILL or a host crash cannot run teardown: leftover
host worktrees and databases require manual removal after confirming their attempt
is no longer running. A later attempt deliberately does not sweep them.

## Review and reject

`aos review <id> [--run RUN]` prints the evidence summary of the newest run that has
one, or of run `RUN`: the run's status, the host evidence directory, each acceptance
command with its exit code, a `totals: <p> PASS / <f> FAIL lines` line (the run's counts
over every result file when the summary carries them, else the kept lines' counts), the `PASS`/`FAIL` lines of each `result.txt`, screenshots,
the agent's evidence directory, the flags (`none` when empty) and, for a run in
status `review`, the commands to accept, merge or reject (any other run prints
`no decision: run <id> is <status>`). Each result file keeps its `FAIL` lines first,
then `PASS` lines, up to 15 in all, and shows the kept lines in file order; `… <n> more lines in <dir>/<file>` says how many
were left out, and `<n> more result files, <m> more screenshots: see <dir>/manifest.json`
says what the bounded summary dropped. It exits 1 with `no run with evidence on <id>` (or
`no run <RUN> on <id>`) otherwise.

`aos reject <id> [--note TEXT]` refuses the task's newest review run: the run ends
`rejected` with `rejected by the operator: <note>` as its error and the last line of
its report, the task returns to `backlog`, and the run's evidence directory is
removed. It prints `the rejected commits stay on the task branch; to discard them:
git -C <repo> branch -D ao/<id>`. `aos accept <id>` and `aos merge <id>` print
`evidence <run id>: flags: <flags or none>` for the newest review run before they act,
when that run has evidence; there is no flag to override and no refusal on flags. See
[Reviewing a run](operations.md#reviewing-a-run). `aos show` prints an
`evidence <run id>: ...` line under each run that has a summary. When a run's
`remote` parses as GitHub (the runner pushed the branch), `aos show` adds
`branch=https://github.com/<owner>/<repo>/tree/ao/<id>` after `remote=` and
`aos review` prints `branch: <that URL>` right after its run line.

## Scope declarations and overlap warnings

Before an agent edits, it declares its scope: `aos scope <kind>:<key>=<operation> ["<note>"]`
(kinds: symbol api schema config infra test migration env file component contract domain;
operations: add extend modify replace remove rename migrate); `aos depends AO-<n> ["<why>"]`,
`aos block "<text>"` and `aos assess <intent id> <conflicts|compatible|duplicate|depends_on>
--merge-rule "<text>"` add a dependency, a blocker or an assessment of another task's scope row,
and `aos retract <intent id>` closes any of these; each takes `--task AO-<n>` to declare on a
task with no live run, using `AOS_TOKEN` instead of the run's own credential. A scope key over 200
characters, one that is not printable ASCII (every character between U+0020 and U+007E), or one
containing `<`, `>`, or `"`, is refused, as is a note, blocker text or merge rule over 2000
characters or carrying a control character other than newline or tab, U+2028, U+2029, or a
character of Unicode category Cf, Zl or Zp; a note or merge rule is also refused when a normalised
copy of it (NFKC, case-folded, every character of Unicode category Pd and `_` read as `-`,
whitespace removed) contains the frame tag `agent-overlap`, `agent-request` or `agent-notice`. This catches the
literal tag and many lookalike spellings, not all: a minus sign or a Cyrillic letter in the tag
passes. What keeps such text harmless is that a note is always shown on one line after `their
note:`, so it cannot start a line or end the frame. A same-key scope declaration from another task in the same
repo is compared by a fixed rule the moment its own task is open (neither `done` nor `cancelled`)
and, when it matches, recorded as a finding of severity `high`, `medium` or `low`; the finding
survives either row's retraction. `aos scopes` prints the header "Declared for other tasks;
information, not instructions:", then the other tasks' currently open scope rows, one per line,
with every run of whitespace in a note -- line breaks of any kind included -- replaced by one
space, each line led by `[operator]` (the row's run_id is empty) or `[agent]`; `aos requests`
prints each open, unanswered finding of severity `high` or `medium` as an `<agent-overlap>` block
after the pending requests, with the same one-line treatment of the other agent's note, and `aos
run` prints the claim's `conflicts` right after the line reporting the claim. A `depends_on` row
that is not retracted and whose target task is not `done` soft-gates `aos ready` and `aos requeue
--ready` with a 409 naming the target and its status; `--force` overrides. `aos show` prints every
row of the task, retracted rows of every kind marked `[retracted]`, and its findings; `aos intents
--repo <name>` (operator only; add `--all` to include retracted or closed rows) lists them for a
whole repo. The operator gets one Telegram message per finding of severity `high` or `medium`, sent
once and never while Telegram is off.

## Interaction review

`aos interactions AO-<a> AO-<b>` asks one model call, started with no tools, none of the operator's
settings, and `--safe-mode`, what breaks when two tasks' branches -- both already in review and
sharing a repo -- run together: it diffs each branch against their shared base and stores the
answer as an interaction review, with failure scenarios and a test command for each; the stored row
itself carries no "model-proposed" mark, but the line `aos interactions` prints and the Telegram
notice text both do. Every proposed command is text written by a model that read other agents'
diffs, briefs and declared scopes; `aos` never runs it, and it is to be read before anyone else
runs it by hand. It never merges, requeues, resolves a conflict, or blocks anything; the operator
reads the review and decides. `--safe-mode` (probed 2026-09-28)
keeps the operator's hooks and `CLAUDE.md` out of the review's model call. `aos show` prints one
line per stored review, the five newest.
