# Claude plugins on the OpenShell route: a live statiker run (2026-10-14)

Proof for `AOS_OPENSHELL_CLAUDE_PLUGINS` (the `openshell:` and `docs:` commits that introduce it). Two
`aos run --backend openshell` runs of one small task on `~/code/aos-example`, with the statiker
0.2.104 and dispatch-guards plugins loaded through the runner. Evidence:
`docs/probes/2026-10-14-openshell-plugins-live/` (`attempt1/`, `attempt2/`, sandbox lists).

## Setup

| Item | Value |
|---|---|
| Runner | `uv run aos run <id> --backend openshell` from the agent-os worktree (new code) |
| Bus | disposable, `127.0.0.1:8014`, data dir and 0600 token under the scratchpad; stopped afterwards |
| Image, platform, provider | `aos-harness:comms` (Claude Code 2.1.296 per the mechanisms probe's image check; not re-read in these runs), `colima-mac`, `aos-claude-oauth` |
| Plugins | `AOS_OPENSHELL_CLAUDE_PLUGINS=/Users/operator/.claude/plugins/cache/statiker/statiker/0.2.104,/Users/operator/code/dispatch-guards/plugin` |
| Other env | `AOS_OPENSHELL_CLAUDE_MODEL=sonnet`, `AOS_HARNESS_TIMEOUT_SECONDS=3600`, `AOS_PUSH=off`, state dir under the scratchpad, `AOS_REPO_ROOT=/Users/operator/code`; comms off; `NODE_EXTRA_CA_CERTS` unset |
| Task | add `slugify(title)` in `notes/slug.py` and a DB-free `tests/test_slug.py`; acceptance `uv run --frozen pytest -q` (added with `--brief-file`, `--accept`) |

At the time of these runs the runner named the plugins by directory basename, so the report note
read `plugins: 0.2.104@0.2.104, plugin@0.11.28` and the sandbox paths were `/sandbox/plugins/0.2.104`
and `/sandbox/plugins/plugin`. Skills were addressed by their manifest names regardless
(`statiker:statiker` loaded in both runs). The follow-up commit on the same branch names plugins by
the `name` in their manifest instead (`/sandbox/plugins/statiker`, `/sandbox/plugins/dispatch-guards`,
`plugins: statiker@0.2.104, dispatch-guards@0.11.28`); the runs above predate it and were not repeated.

## Attempt 1 (AO-1): plugins loaded, procedure reduced

`review / pass`, branch `ao/AO-1` (`227eaec`, 2 files), harness 41 s. The model loaded the skill, ran
`PREFLIGHT_OK`, then said it ran the run "in reduced form": no tracker, no design lock, no attack, no
isolated verify. The desk's final message is the only trace of the skill load and the script call (no
transcript is kept for Claude runs), so what this attempt shows is that the runner's `--output-format
json` run with `--plugin-dir` completed and reported the skill as loaded. The brief ("Effort: light",
"change nothing else") let the desk skip the forcing points. Not a defect of the mechanism; the brief
was too permissive for a measurement.

## Attempt 2 (AO-2): a whole run

Brief changed to forbid the reduced form (tracker committed as deliverable, lock, one opus attack,
isolated verify). `review / pass`, branch `ao/AO-2`, harness 508 s, acceptance in the fresh sandbox
"1/1 pass" per `runner.log` (the count of 19 tests comes from the desk's own report, not the runner's
output). Six commits (`git log` in `attempt2/commits.txt`): lock `0a627d8`, re-lock after A1
`e29e7ba`, A2 `ffa4bc8`, the unit `ba0223c`, close `73d0399`, a one-line fix `041c766`.

What the record (`attempt2/record/2026-10-14-ao-2-slugify.md`, Status COMPLETE, Verdict PASSED) shows:
- design lock with recorded decisions (D8 and others) and findings with bases;
- two attack rounds, A1 and A2, both on opus subagents (the record's Close, deviation 6, says "Attack tier
  was opus" and that the verify leg ran on sonnet; the dispatches' model fields themselves are not logged
  anywhere the runner keeps), both found something (A1: U+0130 lowercases to `i` plus a combining dot,
  pinned by tests; A2: non-ASCII literals made escapes);
- implementation by the desk itself, declared as a deviation (F15);
- isolated verify leg at `ba0223c` driven by a sonnet leg: 11 passed with `DATABASE_URL` unset, 19 on the
  full suite, doctor ok, `api-health` and `empty-title` drives passed; `create-note` and `recent-list`
  not driven (planted defect in `main`);
- closure was not a zero-delta round: both rounds bit, no third round ran (declared deviation 1).

What it does not show: a cost line (the runner prints none; the bus run record has no cost field); a
third zero-delta attack round; the plugins' hooks firing (the settings file passed `disableAllHooks`
false, but no hook output was captured: the harness stdout is not kept for Claude runs, so whether the
statiker stop guard or the dispatch-guards gates fired is unmeasured here); the model names of the
attack legs from a primary source (the record's own close text only). The runner's `network:` line for
attempt 2 reads `192.168.5.2:32869 x62, denied x0` (`attempt2/run-report.txt`): 62 connections to the
run's PostgreSQL service and none denied, over the last 600 gateway log lines the runner reads; the bus
on 8014 and `api.anthropic.com` do not appear in that tail, so the line says nothing about them.

Sandboxes: `openshell sandbox list` was empty before (`sandbox-list-before.txt`) and empty after
(`sandbox-list-after.txt`). Another session's `aos-ao-3-r1` appeared in an intermediate listing during
attempt 2 that was not kept. That this lane's sandboxes (`aos-ao-1-r1`, `aos-ao-2-r1` and their `-acc`)
were deleted by the runner itself is inferred from the empty final listing.
