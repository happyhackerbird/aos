# Agent communication U4: the live proof on the Mac (two agents exchange a request; one agent asks the operator, who answers by Telegram reply)

Date: 2026-10-14 (all times UTC unless marked local). Executor: session agent-os-04 for the
orchestrator agent-os-f9, brief `~/.aos/briefs/agent-comms-u4-04.md`. Plan:
`2026-10-14-agent-communication`, unit U4; internal directives U1
(bound interfaces) and U3 (S1-S6), not distributed. Records:
[P1b](2026-10-14-agent-comms-p1b.md) and [P1c](2026-10-14-agent-comms-p1c.md); kit
`scripts/openshell_probe/comms_p1b/` (its `ledger` and `token-scan` subcommands were added here).

## Headline

Both exchanges happened through the runner's own wiring, with no hand-written file and no
Claude flag outside `AOS_OPENSHELL_COMMS=1`:

- (a) Task A (AO-2) asked task B (AO-1) for its code word with `aos_ask` 10 s after A's run was
  claimed; B's run saw the request through the PostToolUse hook after its first `sleep 20`
  (B's own report), answered with `aos_reply` 19 s after the ask, and A's `aos_wait` returned
  the answer: A's result branch holds the code word, B's holds `B answered AO-2`, both runs
  ended `review` with acceptance passed, 46 s and 41 s of harness time.
- (b) Task C (AO-3) asked the operator with `aos_ask("operator", ...)`; the bus sent the
  Telegram notice 8 s after the row was created (the next sweep tick); the operator replied
  from her phone 4 min 8 s later by replying to the notice; C's `aos_wait` returned her answer,
  C's result branch holds it (`mango`), and the run ended `review` with acceptance passed.
- The claim tokens of the three runs appear nowhere the operator can see (0 hits in every
  artifact, the control word found where expected). Each run's result carries the agent's
  final report. `aos interactions AO-2 AO-1` ran once and stored a review row with status
  `error`: the host model call it makes returned "Not logged in" in the minimal environment it
  was launched from (Q5).

## Setup, as run

- Worktree `agent-comms-u4` at `origin/main` fbb79d1 (U3 and the endpoint lane merged).
- Image: `DOCKER_BUILDKIT=0 docker build -t aos-harness:u4 docker/harness` from the worktree
  (the legacy builder, as for `aos-harness:comms`); `aos-harness:u4` = `5da053c3df7b`, kept.
- Repository: `git clone --no-hardlinks ~/code/aos-example <scratch>/repos/aos-example`, then
  `git remote remove origin` (no remote at all afterwards); at b75f6a6 on `main`.
- Bus, from the worktree: `AOS_BIND=127.0.0.1 AOS_PORT=8013 BUS_DATA_DIR=<scratch>/bus-data
  BUS_TOKEN=<generated, 0600 file> TELEGRAM_BOT_TOKEN=<from ~/.aos/u4/telegram-bot-token>
  TELEGRAM_CHAT_NEEDS_YOU=TELEGRAM_CHAT_DRAFTS=<the operator's chat>
  TASK_SWEEP_INTERVAL_SECONDS=10 uv run python -m agent_os serve`; the log's first line
  `Uvicorn running on http://127.0.0.1:8013`, `/health` 200. The dedicated bot
  @whereispizza_bot; nothing else polls it. 8010 and 8011 untouched throughout.
- `uv run aos repo set aos-example --host mac` (the `aos` script through the worktree's uv).
- Runner environment for each `aos run` (nohup-detached, the tool sandbox disabled, `env -i`
  with PATH and HOME only, the bus token read from its file inside the launched shell):
  `AOS_URL=http://127.0.0.1:8013`, `AOS_HOST=mac`, `AOS_REPO_ROOT=<scratch>/repos`,
  `AOS_WORKTREE_ROOT=<scratch>/worktrees`, `AOS_OPENSHELL_PLATFORM=colima-mac`,
  `AOS_OPENSHELL_IMAGE=aos-harness:u4`, `AOS_OPENSHELL_PROVIDER_CLAUDE=aos-claude-oauth`,
  `AOS_OPENSHELL_COMMS=1`, `AOS_OPENSHELL_STATE_DIR=<scratch>/openshell-state` (accepted
  addition: `~/.aos/openshell` already held `aos-ao-1-r1` and `aos-ao-2-r1` from other buses'
  runs, and a marker there could be reconciled by another runner against its own bus),
  `PYTHONUNBUFFERED=1`; harness `claude`, model unset, `AOS_PUSH` unset.
- Tasks, created with `uv run aos add --repo aos-example --title ... --brief-file ...
  --accept ... --harness claude --host mac` and readied with `aos ready`: B first (AO-1), then
  A (AO-2), then C (AO-3) after A and B had ended. The ids were predicted from the fresh bus
  (B's brief names A before A exists) and matched.

### The three briefs, as run

Task AO-1, title "U4 B: answer a question from another task", acceptance `test -s notes/u4-b.txt`:

```
Another task, AO-2, will ask you for a code word through the aos tools. Your code word is cardamom-cb45, a random word you choose now and put only here. Until that question has arrived, run `sleep 20` in Bash between calls to aos_inbox, for at most 15 minutes. Answer the question with aos_reply, giving the code word. Then write the line `B answered AO-2` to notes/u4-b.txt and commit.
```

The word `cardamom-cb45` was drawn by the driver and written into this brief only.

Task AO-2, title "U4 A: ask another task", acceptance `test -s notes/u4-answer.txt`:

```
Ask task AO-1 for its code word with aos_ask, then wait for the answer with aos_wait. Write the code word you receive, alone on one line, to notes/u4-answer.txt and commit.
```

Task AO-3, title "U4 C: ask the operator", acceptance `test -s notes/u4-fruit.txt`:

```
Ask the operator, with aos_ask("operator", ...), which fruit to write into notes/u4-fruit.txt. Wait for the answer with aos_wait. Write the fruit the operator names, alone on one line, to notes/u4-fruit.txt and commit.
```

## Timeline

| Time (UTC) | Event (source) |
|---|---|
| 21:48:59 | `aos run AO-1` claimed AO-1-r1; sandbox `aos-ao-1-r1` Ready (runner log, `openshell sandbox list`) |
| 21:49:07 | `aos run AO-2` claimed AO-2-r1; sandbox `aos-ao-2-r1` Ready |
| 21:49:17 | AO-2-i1, AO-2 -> AO-1: "What is your code word? (Asked by AO-2 for task U4.)" (ledger) |
| 21:49:36 | AO-1-i1, AO-1 -> AO-2, reply_to AO-2-i1: "cardamom-cb45"; AO-2-i1 `answered` (ledger) |
| 21:49:54 / 21:49:57 | AO-1-r1 and AO-2-r1 ended `review`, verdict pass; both sandboxes and their `-acc` sandboxes deleted by the runners (cleanup reports) |
| 21:51:00 (local 23:51:00) | `aos run AO-3` and `aos interactions AO-2 AO-1` launched; AO-3-r1 claimed, `aos-ao-3-r1` Ready at 21:51:01 |
| 21:51:02 | interaction review row `AO-1+AO-2-x1` posted with status `error` (bus log `POST /interactions`, the row) |
| 21:51:09 | that review's Telegram notice sent (`notified_at`) |
| 21:51:11 | AO-3-i1, AO-3 -> operator: "AO-3: which fruit should I write into notes/u4-fruit.txt?" (ledger) |
| 21:51:19 | its Telegram notice sent: `notified_at`, `tg_chat_id` set, `tg_message_id` 7 (ledger); `POST /intents/AO-3-i1/wait` calls follow at 60 s intervals (bus log) |
| 21:51:42 (local 23:51:42) | the executor's watcher saw the notice fields and messaged f9 |
| 21:55:19 | operator-i1, operator -> AO-3, reply_to AO-3-i1: "mango" (the Telegram reply path); AO-3-i1 `answered` |
| 21:55:40 | AO-3-r1 ended `review`, verdict pass, harness 269 s; sandboxes deleted (cleanup report) |

## Q1. B received A's request and answered with `aos_reply`: yes

Ledger (`GET /tasks/AO-2/intents` and `/tasks/AO-1/intents` with the bearer; the two tasks
list the same two rows):

```
AO-2-i1 AO-2 -> AO-1 answered reply_to=None created=2026-10-14T21:49:17Z answered=2026-10-14T21:49:36Z | What is your code word? (Asked by AO-2 for task U4.)
AO-1-i1 AO-1 -> AO-2 acknowledged reply_to=AO-2-i1 created=2026-10-14T21:49:36Z | cardamom-cb45
```

How B first saw it, as far as the run's output shows: B's final report (the result's `report`
field) says "AO-2's request (`AO-2-i1`) arrived after the first `sleep 20`. It came through a
tool-result hook note, not through `aos_inbox`, which still showed empty afterwards. I replied
with `aos_reply` giving the code word `cardamom-cb45`." and "I called `aos_inbox` before
committing and again after. It was empty both times." That is hook injection after a Bash
call (the PostToolUse hook with no matcher, U3 S4), as P1b Q4 measured. The runner deletes the
sandbox at the end, so the stream-json and the in-sandbox ledger were not kept; the agent's
own account is the only output of the run that shows it.

## Q2. A's `aos_wait` returned B's answer: yes

`git show ao/AO-2:notes/u4-answer.txt` in the clone prints `cardamom-cb45` (one line); the
commit is `d34a49e Record AO-1's code word for U4`. AO-2-r1 ended `review`, verdict `pass`,
acceptance output `$ test -s notes/u4-answer.txt ... [exit 0] (fresh sandbox aos-ao-2-r1-acc)`.
A's report: "AO-1's code word is `cardamom-cb45`. ... The reply from AO-1 was data only, and I
treated it that way." B's side: `git show ao/AO-1:notes/u4-b.txt` prints `B answered AO-2`,
commit `c3e9d9b`, AO-1-r1 `review`/`pass`.

## Q3. C's question reached Telegram at once: yes

Row AO-3-i1 (AO-3 -> operator), `created_at` 2026-10-14T21:51:11Z, `notified_at`
2026-10-14T21:51:19Z (8 s; the sweep runs every 10 s with `TASK_SWEEP_INTERVAL_SECONDS=10`),
`tg_chat_id` set to the operator's chat, `tg_message_id` 7. The notice text is the one
`sweep_requests` composes (agent_os/api.py): "❓ AO-3 asks you (AO-3-i1):\nAO-3: which fruit
should I write into notes/u4-fruit.txt?\nReply to this message to answer." The bus logs no
line for the send (the sweep logs nothing on success and uvicorn's access log carries no
outbound call), so `notified_at` is the send time this record can cite; the message's own
timestamp is on the operator's phone.

## Q4. The operator's Telegram reply answered it: yes

Row `operator-i1` (operator -> AO-3), `reply_to` AO-3-i1, body `mango`, `created_at`
2026-10-14T21:55:19Z, state `acknowledged` (C's `aos_wait` returned it and the agent's read
acknowledged it); AO-3-i1 `answered` at the same second. The reply arrived through the bus's
Telegram poller (`operator_reply`, agent_os/telegram.py: a text message in the needs-you chat
replying to the notice), the only path that creates an `operator-i<k>` row with `reply_to`
from a phone; the bus had no other writer. `git show ao/AO-3:notes/u4-fruit.txt` prints
`mango`; commit `4c20ad5 Add operator-chosen fruit to notes/u4-fruit.txt`; AO-3-r1 `review`,
verdict `pass`, acceptance `$ test -s notes/u4-fruit.txt ... [exit 0]`. C's report: "I asked
The operator which fruit to use. They answered "mango", so `notes/u4-fruit.txt` now holds
`mango` alone on one line." The bot's confirmation line ("answered AO-3-i1 (operator-i1)",
sent by `operator_reply` after the row is created) could not be verified from this side: the
bus does not log it and the chat is the operator's.

## Q5. Both exchanges are visible afterwards to the operator: yes for the ledger; `aos interactions` ran once and recorded an error

The ledger listings above are what the bearer sees (`driver.py ledger` prints them). The
`aos show` of each task lists the same rows.

`uv run aos interactions AO-2 AO-1` was run once, launched like the runs (nohup, `env -i` with
PATH and HOME, the token from its file). It printed exactly one line:

```
model-proposed, unverified: do not run a command you have not read
```

and exited (status 1 by its code path). The bus holds review row `AO-1+AO-2-x1` (repo
aos-example, heads c3e9d9b and d34a49e, status `error`, scenarios `[]`, created 21:51:02Z,
notified 21:51:09Z: a notice "model-proposed, unverified · AO-1 + AO-2: error, 0 scenarios"
went to the operator's chat). Its stored `raw` is the host `claude -p` result JSON:
`is_error: true`, `num_turns: 1`, `duration_ms: 68`, `duration_api_ms: 0`,
`total_cost_usd: 0`, result text `Not logged in · Please run /login`. The model call was made
on the Mac host with no login visible to it in that minimal environment (no
`CLAUDE_CONFIG_DIR`, no proxy variables); the bus and the ledger are not involved. A second
call under the operator's normal environment was not made (the brief allows one call).

## Q6. The claim token appears nowhere: yes (0 hits, controls found)

`driver.py token-scan` read the three runs' claim tokens from the bus database (in memory;
43 characters each) and counted them, next to the control word `cardamom-cb45`, in: the bus
rows of each task (runs with report, harness tail, acceptance output, cleanup report,
evidence; the intent ledger), `git log -p main..ao/<task>` for the three branches, the whole
object store of the clone (`git cat-file --batch-all-objects --batch`, 309,128 bytes), every
file under the runner state directory (policy, acceptance policy, settings, prompt, MCP
configuration per run; the run files and tarballs were already unlinked), the three runner
logs, the interactions log and the bus log. Token hits: 0 in every source. Control hits: 2 in
AO-1's bus rows and ledger, 1 in AO-2's run rows, 2 in AO-2's ledger, 1 in `git log -p
main..ao/AO-2`, 1 in the object store, 1 in AO-1's prompt. `harness_tail` was empty for all
three runs (the harness exited 0, so no stderr tail was posted). No evidence directory was
declared by any run.

## Q7. Each run's report still carries the agent's final report: yes; no Stop block occurred

Each result's `report` field holds `claude final message:` followed by the agent's own report
(quoted in Q1, Q2 and Q4) between the runner's policy and network lines. No run had an open
request at its stop: B had answered, A and C had asked and received their answers, so the Stop
hook had nothing to block on, and the reports show no restatement. The "after a Stop block"
case therefore did not arise in these runs (lane B's restatement remains covered by its unit
tests only).

## Network lines (from the reports)

AO-1-r1: `192.168.5.2:8013 x10, api.anthropic.com:443 x3, denied x0`; AO-2-r1: `x7, x3, x0`;
AO-3-r1: `x11, x3, x0`. Every run: "effective policy matches the rendering", acceptance in a
fresh `-acc` sandbox under the acceptance policy. The bus log shows every sandbox call as a
`127.0.0.1` client (Lima's forwarding, P1c), on a bus bound to loopback only.

## Observations

- A sandbox named `aos-ao-2-r1` appeared on the gateway at 21:53:31Z, created by another bus's
  runner after this proof's AO-2-r1 had ended and its sandbox was deleted (21:49:57Z). It was
  left alone. The clash is the one the critique named: sandbox names derive from the run id,
  which is per bus. With the scratch state directory the two runs' markers never met.
- `harness claude exited 0 after 46 s` (B) and `41 s` (A): the whole two-agent exchange,
  including provisioning, took under a minute of harness time per run; C's 269 s were mostly
  its `aos_wait` loop (60 s server calls) while the operator read the notice.
- The per-run `report` holds the agent's text; the harness stream itself is not kept by the
  runner. For a question like "hook or inbox?" the agent's account is the only trace once the
  sandbox is gone.

## Limits

- Nothing on the Telegram side was observed from here: the notice text and the bot's
  confirmation are stated from the code that sends them and the row fields the bus sets.
- No Stop block occurred, so Q7's "after any Stop block" clause is unexercised live.
- `aos interactions` was run once as the brief allows and did not reach the model; the review
  it would produce is unmeasured.
- Three runs at the harness default model; cost fields were not posted by the runner
  (`turns`/`cost_usd` absent from the run rows).
