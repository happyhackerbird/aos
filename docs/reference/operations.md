# Operations

[Documentation index](../README.md) · [Configuration](configuration.md)

## State and process model

SQLite is the durable bus state; `runs.jsonl` records lane and task activity.
`curl http://127.0.0.1:8010/health` checks a running bus. Run the bus as a
single process; its lock is in-process. Workers run separately, launched manually
with `aos run` or automatically by `aos dispatch` on a worker host. Repository
preflight and acceptance resolution belong to the runner, not the bus.

Malformed heartbeat replies count toward the same three-consecutive-failure limit
as transport/server failures. They never renew the local lease; a valid active
response resets the failure count. At the limit the worker signals lost contact
instead of silently losing its heartbeat thread.

## Deployment example and notifications

The bundled Compose deployment is a local starting point. Configure remote access
explicitly rather than exposing the authenticated bus by accident.

Operator HTTP routes require `Authorization: Bearer <BUS_TOKEN>`. `GET /health`
is unauthenticated. Agent-facing request/scope routes accept the run claim token
without the operator token; runner lifecycle routes also require bus authentication.
Compose reads `./.env`, persists `agent-os-data` and publishes `127.0.0.1:8010`.
It does not require an operator-specific external Docker network.
Lane health warnings are marked delivered only after Telegram returns a message
ID. A failed send remains eligible on the next health check, including after a bus
restart. A crash after Telegram accepts a warning but before the database records
it can produce a duplicate; delivery is preferred over silently losing the warning.

For the repository-workflow cutover, stop dispatching and let existing runners finish
before upgrading. Back up the bus database, confirm workers can fetch each repository's
committed `aos-workflow.toml`, then update both the bus and worker installation to the
same revision before restarting dispatch. The new claim API requires an execution
snapshot; older workers cannot claim from the upgraded bus. Preserve legacy registry
test commands as migration data; no workflow sync command is needed. See
[workflow migration](configuration.md#migrate-registry-test-commands-before-rollout).

## Super Productivity source

The `sp.poll` lane reads the operator's tasks from Super Productivity's sync files in
Dropbox and keeps the bus backlog in step with them. It is a reader: it never writes a
sync file, so results are not written back into the app. Settings:
[configuration](configuration.md#super-productivity-source).

**Making the Dropbox app, once.** In the Dropbox App Console create a scoped-access app
of your own (an app folder is enough when the sync folder is inside it; otherwise full
Dropbox) with the single permission `files.content.read`. Note its app key and app
secret, then run on any machine:

    python -m agent_os.sp_sync auth --app-key <key> --app-secret <secret>

It prints the authorize URL (`token_access_type=offline`, no redirect: Dropbox shows the
code on its page), takes the pasted code and prints the refresh token once. Put the three
values in the bus environment as `AOS_SP_DROPBOX_APP_KEY`, `AOS_SP_DROPBOX_APP_SECRET`
and `AOS_SP_DROPBOX_REFRESH_TOKEN`. Access tokens are short-lived; the lane mints one
from the refresh token when it needs it.

**The sync folder.** `AOS_SP_DROPBOX_PATH` is the folder that holds `sync-data.json`,
as the app's Dropbox sync settings show it (the app's *base path*), for example
`/Apps/super-productivity`; `/` for an app-folder app whose folder is the sync folder.
With sync encryption on in the app, set `AOS_SP_SYNC_PASSWORD` to the same password;
without it an encrypted file is a lane error and nothing changes on the bus. The split
sync files (the app's opt-in "split sync files" setting, `sync-ops.json` plus a
`sync-state` snapshot) are refused with a lane error naming the cause: turn that
setting off and let the app sync once.

**The project or tag, the repo line, and release.** Tasks are read from the project named
`AOS_SP_PROJECT` or the tag named `AOS_SP_TAG` (titles as shown in the app). Each task
maps as follows: a line `repo: <name>` anywhere in the notes (or inside `## AOS`; not
inside a code fence) names the repo, which must be registered, and is removed from the
brief; when there is no such line, exactly one tag whose title equals a registered repo
name still picks the repo; without either the task is not created. The title is the
title; the notes are the brief (a task with empty notes, or only a repo line, uses its
title as the brief), with the titles of its subtasks appended as a list; a `## Acceptance` heading in
the notes holds explicit acceptance commands, one per line (list markers and backticks are
stripped). Omit it to let the worker resolve committed repository defaults; missing
checks are a worker refusal, not a board readiness gate. A `## AOS` heading may set
`harness:`, `host:` and `tier:` (0 to 2; 3 is ignored with a notice). Tasks are released
to ready at the next poll, without a tag, once they have a brief; tag a task `hold` (`AOS_SP_HOLD_TAG`) to
keep it back, which also takes a ready task back to backlog before a claim. Setting
`AOS_SP_READY_TAG` to a tag name restores the gate: only tasks carrying that tag are
released. A task done
or deleted in the app while backlog or ready is cancelled on the bus. A task dragged under
another task (now a subtask) or moved out of the project is not deleted: its bus task waits
as it is, and the lane follows it again once it is top-level in the project. From the claim
on, the app's state is ignored: the bus owns the task. Readiness of these tasks is
managed with the hold tag, not with `aos ready`: the next poll follows the tags.

**What the operator sees.** Each task carries `source=sp` and the app's task id as
`external_id`, so re-reads update rather than duplicate. Every poll that changed
something is one `sp.poll` line in the bus log and one notice in the code chat listing
the created, updated, released, unreleased and cancelled tasks; the tasks held back
(no repo, tier 3 ignored, not released) are listed when that list
changes. A read that fails (Dropbox, password, format) is the lane's error in
`GET /health` and changes no task or run on the bus. The authoritative `task` model
must contain an `entities` object with object-valued tasks. If `ids` is present,
it must be a list of strings naming existing entities; missing or malformed task
collections are errors, never an empty task list. An explicit empty `entities`
object with absent or empty `ids` is valid and still cancels removed pre-claim
tasks. Optional project/tag metadata and omitted optional task fields remain
supported. `aos sp poll` (or `POST /sources/sp/poll` with the operator token) runs
one poll at once and prints the outcome.

## Code lane and GitHub

The code lane: a review run whose result carries `remote` (the runner pushed its branch,
see [delivery](#delivery-to-github)) gets a pull request when `GITHUB_TOKEN` is set, the
remote is GitHub and the repository is registered with `aos repo set <name> --deliver pr`;
one card in `TELEGRAM_CHAT_CODE` with Merge and Requeue. `aos merge AO-n` merges the PR
(bound to the acceptance commit's head; the task is done only when GitHub reports the
merge); `aos accept` stays for repos without a remote, for push-only repos (the default)
and for runs whose PR step was off. Settings:
`GITHUB_TOKEN`, `GITHUB_API_BASE`, `GITHUB_MERGE_METHOD`, `TELEGRAM_CHAT_CODE`,
`CODE_POLL_INTERVAL_SECONDS`.

See [integration and merge checks](cli.md#integration-and-merge-checks) for the
head/base checks required before Merge.

## Delivery to GitHub

Task-flow Part C (2026-10-14). With `AOS_PUSH=on` on the runner host, the OpenShell
runner pushes exactly `+refs/heads/ao/<task>:refs/heads/ao/<task>` from the source
repository to `AOS_PUSH_REMOTE` (default `origin`) after acceptance and before the
result post, whenever the run has a result commit, the verdict is pass or fail, and the
bus has not already ended the run. The push goes through the runner's hook-free git
(`core.hooksPath` empty, the host's `GIT_*` dropped), terminal prompts off, 300 s
timeout. The credential: `AOS_PUSH_TOKEN_FILE` names a file holding a GitHub token,
read by a git credential helper for that one call, so the token is never in argv, in
another process's environment, in a log or on the bus; unset, the host's own git
credentials apply. The runner pushes only `ao/<task>` (the first guard); a repository
ruleset restricting the token to `ao/*` is the second where the plan allows it.

A private repository needs a host credential for reads as well. Before the claim the
runner asks the remote for its default branch (`git ls-remote --symref origin HEAD` in
`policy.resolve_default`), and `AOS_PUSH_TOKEN_FILE` covers only the push. Without a
credential the run stops before the claim with `policy: default branch unavailable:
fatal: could not read Username for 'https://github.com'`, and the dispatcher puts the
task on cooldown. On a server (measured 2026-10-14 with a private repository) a
credential helper in root's git configuration that reads the same token
file covers both:

```sh
git config --global credential.https://github.com.helper \
  '!f() { test "$1" = get || exit 0; echo username=x-access-token; echo "password=$(cat /root/aos-runner/github-token)"; }; f'
```

The runner's git keeps the host's git configuration (only `GIT_*` variables are dropped),
so this applies to every runner git call; the sandbox has its own home and never sees it.
A runner started by systemd needs `HOME` set to find that configuration (the example unit
sets it).

The push is non-fatal: a failure keeps the verdict, omits `remote` from the result and
ends the report with `push failed: <reason>` (credentials redacted); the runner's log
says `push failed (non-fatal); ao/<task> stays in the source repository: <reason>`, so
the branch can be pushed by hand. A successful push puts `remote` in the result (the
push URL with http(s) userinfo removed; an ssh form keeps its `git` login; a URL still
carrying a password is not posted, and the report says so) and the report line
`pushed ao/<task> to <url>`.

Per repository, `aos repo set <name> --deliver push|pr` decides what follows: `push`
(the default, and the behaviour of an unregistered repository) leaves the branch on the
remote for review and `aos accept` closes the task; `pr` also opens a pull request
through the code lane. `aos show` (`branch=`), `aos review` (`branch:`) and the Telegram
card (`branch`) show `https://github.com/<owner>/<repo>/tree/ao/<task>` when `remote`
parses as GitHub.

Cancel during acceptance: when the bus has ended the run (cancelled, lease lost) before
the result post, nothing is pushed and `refs/heads/ao/<task>` in the source repository
is restored to the value it had before this run fetched its bundle, or deleted when it
did not exist; the log says `ao/<task> restored to <sha>` or `ao/<task> deleted`. A
progress post refused with 409, or answered with a run status that is no longer live,
marks the run as ended on the bus, and the runner reads the run's status once more
before it provisions a fresh acceptance sandbox and before each in-sandbox acceptance
command, so a cancelled run spends neither. A run whose lease lapsed while it still
reads live is left to the heartbeat, whose next post sweeps the lease and marks the run
ended, so nothing is posted; a late result reaches the bus only when the runner cannot
reach it at all (the transport-outage case the late-result admission exists for).

## Dispatcher on a worker host

Task-flow Part B (2026-10-14). `aos dispatch` ([CLI](cli.md#dispatch)) starts the runs
nobody types `aos run` for. It is stateless; the bus is the state. Each poll
(`AOS_DISPATCH_POLL_SECONDS`, default 60) it reaps runners that exited, counts the live
runs of this host (runs `claimed` or `running` on the bus whose `worker` is this host's
name, plus its own runners still alive, by task id) and, below `AOS_DISPATCH_MAX_RUNS`
(default 1; the operator sets it per host within the sandbox memory), starts one runner
per candidate: `ready` tasks whose host is `AOS_HOST` or `any`, tier below 3, not on
cooldown, lowest task number first. Each runner is
`python -P -m agent_os.openshell_runner --token-fd <fd> run <task>` with the bus token on
a pipe and `AOS_TOKEN` absent from its environment (the runner refuses otherwise), in its
own session, stdout and stderr in `<AOS_DISPATCH_LOG_DIR>/<task>-<UTC stamp>.log`
(default `~/.aos/logs/dispatch`). The dispatcher's own lines go to its stdout: what it
started (pid and log path), how many wait at the cap, what exited and why.

After each poll's count, the dispatcher publishes its capacity with the bearer:
`POST /hosts/<its hostname>/capacity {max_runs, live}`. The bus trusts a publish
for 10 minutes; otherwise it reports host capacity as unknown, with no free slot.
Under the shipped default `AOS_DISPATCH_MAX_RUNS=1`, a coordinating parent is told
`parallel_children 0`, with the blocker naming the host, before its first proposal.
Raise the cap on the host that runs the children, or run them on another host.

A runner that exits while its task is still `ready` (a refusal before the claim, a claim
conflict with a run another host holds) puts the task on an
`AOS_DISPATCH_COOLDOWN_SECONDS` cooldown (default 900), so the loop cannot spin; after
three such exits in a row the task is moved back to `backlog` (`POST /tasks/<id>/unready`)
and the line names the last log, which holds the runner's refusal. A move the bus refuses
or cannot receive keeps the task on cooldown and keeps the count; the next such exit
retries the move. A runner that claimed resets the count. The dispatcher never requeues a failed or blocked task, never merges,
never pushes.

Restart safety: runners outlive the dispatcher. `SIGTERM` (or `SIGINT`) stops the polling
and exits with `dispatcher stopping; N runner(s) keep running`; the next dispatcher sees
those runs as live on the bus and counts them against the cap. A runner that died while
the dispatcher was gone ends on the bus through the lease sweep, and the runner's
`reconcile` (at the start of every `aos run`, or `python -m agent_os.openshell_runner
reconcile`) removes its sandboxes. `aos dispatch --once` runs one poll for a test or a
cron-style use.

Server setup. The `aos` CLI is not installed on the server today (the runner source is
rsync'd to `/root/aos-runner` and run from its virtual environment). Install it into
that environment once, as an editable package so later rsyncs need no reinstall:
`<venv>/bin/pip install -e /root/aos-runner` (or `uv pip install --python <venv>/bin/python
-e /root/aos-runner`), then check `<venv>/bin/aos dispatch --once` by hand. The runner
settings live in an environment file outside the repository (`/root/aos-runner/runner.env`,
`export KEY=value` lines: `AOS_URL`, `AOS_TOKEN`, `AOS_HOST=server`, `AOS_REPO_ROOT`, the
`AOS_OPENSHELL_*` settings, `AOS_PUSH*`, the `AOS_DISPATCH_*` settings). The saved server
configuration sets `AOS_OPENSHELL_BUS_HOST=203.0.113.10` and `AOS_OPENSHELL_COMMS=1`;
the dispatcher reads these on startup, so deployment must restart it to apply them.
The worker credentials stay under `/root/.aos/worker`. The example unit
[deploy/systemd/aos-dispatch.service](../../deploy/systemd/aos-dispatch.service) starts
the dispatcher through `bash -c 'set -a; . /root/aos-runner/runner.env; exec <venv>/bin/aos
dispatch'` (adjust the venv path), with `KillMode=process` so `systemctl stop` or `restart`
signals the dispatcher only and every runner finishes its task:

```sh
cp deploy/systemd/aos-dispatch.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now aos-dispatch
journalctl -u aos-dispatch -f          # the dispatcher's own lines
ls ~/.aos/logs/dispatch/                # one log per runner started
```

Log rotation: the dispatcher's lines are in the journal (its own retention); the per-run
logs under `AOS_DISPATCH_LOG_DIR` are plain files, one per runner, never reopened by the
dispatcher, so a logrotate stanza may compress and remove them by age:

```text
/root/.aos/logs/dispatch/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    nocreate
}
```

## Task families

A parent run proposes children with a stable `request_id`. Children run headless
in their own sandboxes from the parent's recorded base or an exported candidate.
A backlog child waits for `aos ready` (`waiting=authorization`); a ready child
beyond the host or repository caps waits with `waiting=capacity`.

`aos cancel <parent>` cancels its pending and live descendants and leaves completed
work, including branches, reports and review states, visible. The response's
`cascade` names the affected descendants. A parent's failed or blocked finish and
a lapsed lease hold its ready children by moving them back to backlog, and cancel
its live children.

A failed child is retried only by the operator's `aos requeue <child> --ready`;
the parent asks through the operator route. Requeue keeps `proposed_by`,
`proposal_key` and `base_commit`. `aos show <parent>` and `aos family <parent>`
show the family; `--follow` refreshes it in a neighbouring pane. See the
[CLI reference](cli.md#run-a-task).

Operating a family from a laptop, as proven on the server on 2026-10-22:

1. Create the parent with `aos add --execution interactive --harness pi --host
   server` and `aos ready` it; the dispatcher starts it like any ready task and
   its log names the attach line. Children a parent proposes stay in `backlog`
   with `waiting=authorization` until `aos ready <child>` (the repository's
   `auto_ready_proposals` is off by default); the dispatcher then starts them
   inside its cap, and `aos family` shows the capacity line with the blocker
   when the host cap is filled.
2. `ssh <host>` and control the Herdr session with its CLI (the proof's method:
   `herdr --session <name> pane split`, `pane run`, `pane read`), or join it with
   the Herdr TUI client (joined in the proof, not used for typing there); split a
   pane and, with the operator
   bearer in that pane's environment (`AOS_URL`, `AOS_TOKEN`; without it `aos`
   prints `AOS_TOKEN is not set`), run `aos attach <run-id>`; run `aos family
   <parent> --follow --interval 5` in a neighbouring pane. Typed text reaches the
   agent as its next turn when it is idle; while it is inside
   `mcp__aos_inbox_wait` the text queues as omp steering until the wait ends, so
   press Esc first or post through `aos ask <task> "<text>" --operator`.
3. A child's question reaches the parent as an `agent-request` frame inside its
   wait, marked as coming from another agent; the operator answers the parent in
   the terminal and the parent replies with `mcp__aos_reply`, which continues the
   child. Finish notices arrive inside the parent's wait or its next inbox call.
   A message posted while the agent is idle after Esc stays unacknowledged until
   the agent's next inbox call, and the screen shows `N AOS message(s) are
   waiting; call mcp__aos_inbox`. A message posted while the agent runs a long
   tool reached it at that tool's end in the proof with no inbox call on screen
   (the bus row reads `via inbox` for every pull; the run's messaging summary
   counts the hook pulls).
4. Closing the pane, killing the Herdr client or losing the laptop's SSH
   connection changes nothing on the server; reattach with the same command.
   `aos attach` refuses a second client (`--takeover` replaces it) and refuses a
   run that is not live (`is review, not live`, `is cancelled, not live`,
   `is blocked, not live`).
5. Submission runs the task's acceptance composed with the repository's
   `test_command` on the assembled revision in a fresh sandbox; a combination that
   fails ends the parent `failed` with the acceptance output, and the children stay
   in `review`. A requeued child makes its import stale, the submission is refused
   (`stale selection: <child> has a newer run`) until the newest run is imported
   again, and the superseded import stays recorded on the run.
6. Deadlines are finite: the harness timeout ends an idle interactive run
   `failed`; a bus outage of about a minute is survived (the runner logs
   `submission poll: … Connection refused` each poll and the heartbeat extends
   the lease once the bus is back); an outage spanning the runner's three consecutive failed heartbeats (176 s on
   the proof's 240 s lease; the heartbeat spacing scales with the lease, so
   production's 600 s lease tolerates more) makes the runner give up
   (`run unreachable on the bus; result not posted`), delete its sandbox and
   keep the scrubbed session, after which the bus's sweep marks the run
   `blocked` with `lease expired`. Detach leaves a run live; `aos cancel` ends
   it and its descendants on the bus and deletes the parent's sandbox, while a
   live headless child's runner still finishes its harness and acceptance
   before every post is refused.

## Reviewing a run

For a repository that declares evidence ([configuration](configuration.md#evidence)),
a run in `review` carries a summary of what it captured.

- `aos show AO-<n>` adds one line under each run that has a summary:
  `evidence <run id>: <n> files in <host dir>; <p> PASS / <f> FAIL lines; flags: <flags or none>`
  (the counts are the run's totals over every result file when the summary carries them).
- `aos review AO-<n> [--run RUN]` prints the newest run that has evidence (or the
  named run): its status, the host evidence directory, each acceptance command with
  its exit code (or `not run` / the timeout note), the `PASS`/`FAIL` lines of each
  `result.txt`, screenshot paths, the agent's evidence directory, the flags, and the
  three next commands (`aos accept`, `aos merge`, `aos reject AO-<n> --note TEXT`)
  only for a run in review (status review); any other run prints `no decision: run <id> is <status>`.
  It exits nonzero with `no run with evidence on AO-<n>` when no run has a summary.
  The Telegram code card shows the first flag and a count of the others
  (`⚠️ evidence flagged: ...`).
- `aos accept` and `aos merge` print `evidence <run id>: flags: <flags or none>` for
  the newest review run before they act. Flags warn; there is no override flag and
  nothing is refused because of them, so reading the line is the review. Both act as
  before on a flagged run.
- `aos reject AO-<n> [--note TEXT]` refuses the newest review run. It is run-level:
  that run ends `rejected` with `rejected by the operator: <note>` as its error and as
  the last line of its report, and the task returns to `backlog`, so the next
  attempt's previous-attempt section shows why. A task without a review run is
  refused (`no review run to reject`). The code card is retired as `rejected`.
  Nothing is deleted from git: the next run's job tree starts from `ao/AO-<n>` as the
  attempt left it (unless that tip is already contained in the default branch), and
  its prompt carries the previous attempt's status, verdict, error and report.
  `aos reject` prints `git -C <repo> branch -D ao/AO-<n>` (with a literal `<repo>`,
  as the CLI holds no repository path) for discarding the commits first, so the next
  run starts from the default branch. It writes no runs-ledger line.

Evidence stays under `$AOS_EVIDENCE_ROOT/<bus id>/<run id>/` (default
`~/.aos/evidence`) on the runner host until the run is accepted, merged or rejected,
or its task is cancelled or done; a newer run alone removes nothing. The bus id is a
random 32-character token the bus creates once for its data directory and returns from
`GET /capabilities`; it keeps the run ids of different buses apart in one root. `aos
accept`, `aos merge` and `aos reject` remove the directory of the newest review run
under their own bus's id when they succeed, and with no id from the bus they remove
nothing and print one line. Each `aos run` and `aos cleanup` prune eligible evidence
for their bus: a directory goes when its run is `passed`, `rejected` or `cancelled`,
or its task is
`done` or `cancelled`. A run in `review`, `failed`, `blocked` or live, an unknown run
and a directory without a readable `manifest.json` are kept, as is everything when the
bus cannot be read. A removal failure is printed and the directory stays. Run
directories directly under the root (the layout before bus ids) and the directories of
a bus that no longer exists are never pruned; remove them by hand.

A PR merged in the GitHub UI is closed by the code-lane sweep past the flags, because
aos cannot refuse a merge GitHub already made; the card and `aos show` still carry the
flags. Its evidence is not removed by the command path, so it goes at the next prune.

## Known limits

- Telegram delivery is at-least-once; retries can duplicate messages, with no exactly-once guarantee.
- The bus is single-process; its lock is in-process.

Task branches and evidence can persist after compute cleanup. Retry uses the
recorded task branch; there is no general branch-retirement policy. Family candidate
export/import transfers exact revisions, not a shared writable worktree.

OpenShell owns execution isolation. Process-group cleanup alone is not a sandbox,
and a successful terminal detach is not evidence that cancellation or runner loss
was clean. See [release validation](../release-validation.md) for measured limits.

## Run resource cleanup

OpenShell runs and integration checks track ownership in the runner state directory.
`python -m agent_os.openshell_runner reconcile` retries cleanup of owned sandboxes
and per-attempt databases after their recorded owner is gone. Never delete a
resource merely because its name resembles another run's.

The result is posted before sandbox cleanup. Cleanup success/failure is recorded
separately and does not change the acceptance verdict. The host resource ledger
and `aos cleanup` remain for explicitly registered host resources and retained
evidence; they are not a host-execution backend.

`harness.kill_group` refuses a resolved process group `<= 1` and the caller's own
group before any signal or wait, and returns `process group <id> cleanup refused: ...`
as the cleanup event instead of retargeting. On Linux a `killpg` on group 1 from a
privileged caller reaches every process it may signal; a test that handed
`kill_group` a fake process with pid 1 took a host down in October 2026 (AO-21).
Run the Linux suite only in a verified private PID namespace (an OpenShell
acceptance sandbox or an isolated container), preferably as an unprivileged user;
never run it in the host process namespace. Test unsafe cleanup targets only with
`os.killpg` mocked. The OpenShell environment used for AO-21 refused signals even
to a newly spawned child group with EPERM. The real child-group smoke skips when
that permission probe fails; verify ordinary group termination separately in an
isolated environment that permits it. Keep PID isolation after the fix lands.

A result that arrives after the lease sweep already blocked the run is still
accepted from the run's own claim token for `TASK_LATE_RESULT_GRACE_SECONDS`
(default 3600) after the expiry, provided the task is still blocked and no newer run
exists; the row then carries `late_result_at` and the ledger a `result:late` line.
Later than that the result is refused and only logged, as before.

## Evidence through lease loss

The run row carries evidence before the result exists. `claim` sets `stage` to
`claimed`; the runner posts `harness` when the harness starts, `acceptance` when it
ends (with the harness's final text as `report` and the last 4000 bytes of its stdout
and stderr as `harness_tail`), `push` when acceptance ends (with `acceptance_output`
and the `verdict`), and `cleanup` with the cleanup record. All of it goes through
`POST /runs/{id}/progress` from the run's own claim token, is accepted while the run
is live and after a lease expiry, and is refused only for the result's own fields
once a result is posted. A run that loses its lease therefore keeps its stage, report
and acceptance result on the bus; the AO-4 report that sat only in
`.aos/harness.stdout.json` would now be on the row. The runner's stage lines are
flushed as they are printed, so a log file shows where a run is while it runs.

## Launch warnings and failure diagnostics

Sandbox startup/refusal errors are recorded separately from harness failure.
A nonzero harness exit includes a bounded stderr tail from `/sandbox/harness.err`;
Pi-family contract errors retain that diagnostic too. Inspect the recorded stage,
result and cleanup state rather than inferring success from an exited launcher.
There is no sandboxed `aos run --dry-run`.

## Runner git calls, branch tip and source configuration

Every git command the runner issues (worktree creation, the Codex commit, the
policy reads, the push, `aos integrate`'s merge, the OpenShell clone and fetch)
runs with `core.hooksPath` set to an empty private directory the runner creates for
its own process, so no hook of the target repository runs under the runner's git:
not `.git/hooks/*`, not a `core.hooksPath` the clone configured (husky). The
agent's own git commands inside the harness still run the repository's hooks; that
is the project's business. AO-4 r2 is the case this closes: the target's pre-push
hook (`npm run lint && npm run test`) committed a fixture into `ao/AO-4` and set
`core.bare` and `core.worktree` in the source clone's shared `.git/config`.

`aos interactions` uses the same Git wrapper for commit verification, invariants
and diffs: replacement objects are disabled and inherited `GIT_*` variables are
removed, so review reads stay bound to the selected repository and commits.

Two checks surround acceptance and the push. The runner re-resolves `ao/<task>`
after acceptance and again after the push; a tip other than the accepted
`result_commit` blocks the run with `branch ao/<task> moved during acceptance:` or
`… during push:` and both commits named. The push itself is pinned to
`result_commit`, so what reaches the remote is the accepted tree. The runner also
snapshots `core.bare`, `core.worktree`, `core.hooksPath` and `extensions.*` from
the source clone's shared config at claim, compares them when the harness ends
(blocking with `source .git/config changed during the harness: key: old -> new`
before any further git call) and once more before the result is posted (`… during
acceptance or push: …`).

Acceptance commands run with `GIT_DIR`, `GIT_INDEX_FILE`, `GIT_WORK_TREE` and
`GIT_COMMON_DIR` removed from their environment, together with the operator's
`NODE_EXTRA_CA_CERTS`, `NODE_OPTIONS`, `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONHOME`
and `VIRTUAL_ENV` (its `bin` is taken off `PATH`); the run tags and `DATABASE_URL`
stay. One function, `acceptance_env`, owns this list.

Worktree reuse: a retry on the same task worktree first removes the previous attempt's
runner files from `.aos/` (`harness.stdout.json`, `harness.stdout.jsonl`,
`harness.stderr.txt`, `run.json` and its temp files; the `resources-*.json` records
and anything the agent left stay), and the source clone's `.git/info/exclude` gets a
`/.aos/` line, which git applies to every worktree of the clone, so no `git add`
without `-f` can stage the runner's directory. AO-4 r2 reused r1's worktree with r1's
`.aos/` in it, and the hook's commit swept `harness.stdout.json` into the branch.

Push policy: a failed or timed-out push is not a verdict. The run keeps
`review`/`pass`, `remote` stays empty (so the code lane opens no PR), the report
ends with `push failed: <tail>` and the log says
`push failed (non-fatal); ao/<task> stays in the source clone: <reason>`; push it by
hand from the source clone. The alternative, skipping the push when no credential
is configured for the remote, was not chosen: the runner cannot see credential
helpers, SSH agents or `GITHUB_TOKEN` (a bus setting), and a push that fails for
lack of one now costs nothing but a line.

## Retained host-resource recovery

The host runner is retired. The following registry and `aos cleanup` behavior
remains for recovering existing host-owned resources; it is not the sandbox
execution lifecycle. OpenShell task and integration acceptance run in sandboxes.

Host-resource cleanup terminates tagged processes and processes registered by
`aos own`, checking process creation identity before signalling. Persisted
descendant identities remain usable after session detachment, environment
stripping and cwd changes. Launch registration and cleanup use a lock so
`aos own` cannot race record closure.
Cleanup escalates TERM to KILL after a grace period. Worktree
command/cwd matches without ownership are reported and retained. PID reuse is refused.
Shared Colima/VM and Docker daemon processes are retained even if tagged.

Compose containers and networks are found by project label and removed directly,
without requiring the original Compose files. For plain `docker run`, add
`--label aos.run="$AOS_RUN_ID"`. Volumes and build caches are retained. The runner
rechecks ownership on each selected resource and retains resources labelled
`aos.shared=true`, even if they also have a run/project label. Run/project labels
declare exclusive ownership; shared services should not acquire them.
The runner
uses its launch-time Docker environment/context; resources created on another
endpoint or with overridden project names cannot be discovered automatically.

The host registry (normally `~/.aos/resources`) is authoritative and survives worktree
deletion; `.aos/resources-<run-id>.json` is an inspection copy. The closing report and
records list stopped resources, failures, retained resources and cache pruning. Records
include the bus, task, host and runner PID/creation time, without run credentials or
full process environments. Unfinished or failed cleanup is retried on a same-task attempt
or by `aos cleanup` across tasks. The sweep checks terminal bus state and acquires the
workspace lock before stopping compute. Live runs are retained; both the sweep and
same-task recovery retain not-yet-closed records whose runner PID/creation identity is
still alive, even when the bus run is terminal.
Exited but unreaped (zombie) runners do not block either recovery path from
stopping owned resources, and neither does a recorded runner PID whose identity this
user may no longer inspect (the PID was reused by another user's process); PID and
creation-time checks still protect live owners.
Unreachable bus state leaves resources untouched. Recovery skips Docker if its endpoint
identity changed, preserves the original endpoint binding across every failed retry,
and never prunes caches or retires worktrees. Docker discovery/inspection failures and
endpoint mismatches remain explicitly pending; they cannot mark cleanup complete.
An unknown original endpoint requires operator identification rather than adopting
the currently selected context.

Malformed or truncated process-ledger entries, invalid encoding and symlinked ledgers
remain explicit cleanup failures rather than evidence of an empty ledger. Valid entries
in a partially malformed ledger can still be cleaned, but cleanup stays pending.
Legacy-ledger migration refuses to discard unresolved identities; repair the original
ledger from known ownership evidence before retrying cleanup. Do not delete uncertain
entries merely to clear the pending state. Repeated `close()` calls return the same
report; a later recovery attempt rechecks persisted ownership.

Host records live under `AOS_RESOURCE_ROOT`; existing flat records remain sweepable.
There is no current host Codex execution route or per-thread writable-root setup.

This is best-effort host cleanup, not cgroup/container enforcement: tools can strip
tags, hide environments, delegate work to an existing daemon, or spawn unregistered
detached children. The polling tracker can miss rapid fork-and-reparent sequences.
Use `aos own` for exclusively owned commands and existing shared services for shared
work. These legacy records are not an enforceable descendant boundary. SIGKILL
and host failure cannot run immediate teardown; run `aos cleanup` to reconcile
persisted identities independently of retry.
Cache policy and disk preflight are described in [configuration](configuration.md#regenerable-cache-policy).
