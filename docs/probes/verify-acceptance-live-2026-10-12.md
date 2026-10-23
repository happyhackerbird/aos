# Live proof: verification connected to AOS acceptance (MVP item 3), 2026-10-13

Run record: `.clippy/runs/2026-10-12-verify-acceptance.md` (unit U8). Branch
`verify-acceptance` at `2955e28` plus this record; Mac, Colima, OpenShell 0.1.3 gateway.

## Setup

- **Bus:** a separate test bus started from this branch on `127.0.0.1:8011`
  (`env -i`, own `BUS_TOKEN` and `BUS_DATA_DIR` under `~/.aos/test-bus-verify`, no
  Telegram). `GET /capabilities` answered `{"evidence":true}`. The shared bus on 8010
  was not used.
- **Image:** `aos-harness:mvp` (`docker/harness/Dockerfile`: Claude Code 2.1.296, uv,
  Python 3.12, Playwright 1.63 Chromium under `/usr/local`, environment through
  `/etc/profile.d`).
- **Repository:** `~/code/aos-example-os`, a local clone of aos-example main
  (`b75f6a6`) with its origin removed and one local commit `bed9bc9`: a v2
  `aos-policy.toml` granting only `pypi.org:443` and `files.pythonhosted.org:443`, and
  `args=["--no-zygote", "--disable-gpu"]` on both `playwright.chromium.launch()` calls.
  Untracked `.aos/services.toml` (`[postgres]`) and `.aos/evidence.toml`
  (`dir = "evidence"`); `evidence/` is git-ignored. Main keeps the planted defect.
- **Runner:** `env -u NODE_EXTRA_CA_CERTS aos run <task> --backend openshell` with
  `AOS_OPENSHELL_PLATFORM=colima-mac`, `AOS_OPENSHELL_PROVIDER_CLAUDE=aos-claude-oauth`,
  `AOS_OPENSHELL_FRESH_ACCEPTANCE=1`, harness `claude`, acceptance
  `bash scripts/check.sh` (pytest, then the Playwright browser check).

## Positive run: AO-1-r1, the planted-defect fix

Brief as AO-3's: the start page drops a saved note once there are more than five;
repair it, add a test, prove it with the verify skill.

- Timeline: claimed 06:10:34Z, result posted 06:11:32Z. Harness exited 0 after 41 s.
- Database: `postgres for the run at 192.168.5.2:32813` (per-run container, reached
  through the `aos-services` grant: `192.168.5.2:32813 x32` in the network count).
- Result: `c70442f Start page: list the five newest notes, newest first` on `ao/AO-1`
  (`notes/app.py` 4 lines, `tests/test_notes.py` +13).
- The agent ended its final message with `Evidence directory: evidence/20261013T061103Z`.
- Fresh acceptance in `aos-ao-1-r1-acc`, verdict `review` / `pass`:

```
9 passed in 0.47s
PASS total count grew by one
PASS new note is first in the list
PASS new note is visible in the list
PASS row inserted in the notes table
[exit 0] (fresh sandbox aos-ao-1-r1-acc)
```

`aos review AO-1` on the test bus (verbatim):

```
run AO-1-r1: status=review
evidence dir: /Users/operator/.aos/evidence/AO-1-r1
acceptance: bash scripts/check.sh -> exit 0
result acceptance/20261013T061129Z/result.txt:
  PASS total count grew by one
  PASS new note is first in the list
  PASS new note is visible in the list
  PASS row inserted in the notes table
result agent/20261013T061103Z/create-note/result.txt:
  PASS POST /notes redirects (303/302)
  PASS browser is back on the start page
  PASS total grew by one
  PASS new note is visible in Recent notes
  PASS new note is first in Recent notes
  PASS exactly one row with that title and body in notes
  PASS /api/notes lists the new note first
result agent/20261013T061103Z/recent-list/result.txt:
  PASS Recent notes shows 5 items
  PASS newest note is first in Recent notes
  PASS Recent notes is the newest 5, newest first
screenshot: acceptance/20261013T061129Z/01-start-page.png
screenshot: acceptance/20261013T061129Z/02-after-save.png
screenshot: agent/20261013T061103Z/create-note/01-start-page.png
screenshot: agent/20261013T061103Z/create-note/02-form-filled.png
screenshot: agent/20261013T061103Z/create-note/03-after-save.png
screenshot: agent/20261013T061103Z/recent-list/01-start-page.png
screenshot: agent/20261013T061103Z/recent-list/02-after-save.png
agent directory: 20261013T061103Z
flags: none
accept: aos accept AO-1
merge: aos merge AO-1
reject: aos reject AO-1 --note TEXT
```

The bus summary records 26 files, 280767 bytes. On the host,
`~/.aos/evidence/AO-1-r1/` holds `acceptance/20261013T061129Z/` (result.txt, two
screenshots, after-save.html, server.log), `agent/20261013T061103Z/` (the agent's two
drives with screenshots, ARIA and HTML), `outputs/0.txt` (the full `check.sh` output)
and `manifest.json`; the directories are 0700, the files 0600.

## Negative control: AO-2-r1, a change that leaves the defect

Brief: add one README sentence about `scripts/check.sh`; change nothing else.

- Timeline: claimed 06:10:54Z, result posted 06:11:43Z. Harness exited 0 after 31 s.
- Database: `192.168.5.2:32814`. Result: `ae36d35` on `ao/AO-2` (`README.md` +1).
- Fresh acceptance in `aos-ao-2-r1-acc`, verdict `failed`, error
  `acceptance failed: bash scripts/check.sh`. Unit tests pass; the browser step fails:

```
8 passed in 0.41s
PASS total count grew by one
FAIL new note is first in the list
FAIL new note is visible in the list
PASS row inserted in the notes table
list titles: ['Buy milk', 'Call the plumber', 'Renew passport', 'Book the dentist', 'Water the plants']
[exit 1] (fresh sandbox aos-ao-2-r1-acc)
```

`aos show AO-2` evidence line and `aos review AO-2` (verbatim):

```
evidence AO-2-r1: 8 files in /Users/operator/.aos/evidence/AO-2-r1; 2 PASS / 2 FAIL lines; flags: none
run AO-2-r1: status=failed
evidence dir: /Users/operator/.aos/evidence/AO-2-r1
acceptance: bash scripts/check.sh -> exit 1
result acceptance/20261013T061139Z/result.txt:
  PASS total count grew by one
  FAIL new note is first in the list
  FAIL new note is visible in the list
  PASS row inserted in the notes table
screenshot: acceptance/20261013T061139Z/01-start-page.png
screenshot: acceptance/20261013T061139Z/02-after-save.png
agent directory: 20261013T061107Z
flags: none
```

## Cleanup and secrets

- After both runs `openshell sandbox list` printed `No sandboxes found.` and
  `docker ps -a --filter label=aos.service=postgres` printed nothing: both per-run
  databases were removed. The runner state directories hold only `policy.yaml`,
  `accept-policy.yaml`, `prompt.txt` and `claude-settings.json` (no `services.env`,
  no leftover markers).
- The per-run database password (32 characters, read back from the URL file the
  AO-1 agent saved into its own evidence on the host) does not occur in the bus's
  task JSON for AO-1, not even as an 8-character prefix.

## Observations

- The agent's drives wrote notes into the run's database; acceptance ran after the
  reset, on a fresh `aos`-owned database.
- `lsof` is absent from the harness image, so the verify skill's `app.sh doctor` could
  not confirm the server's pid or port (the AO-1 agent reported it; the drives passed).
- `aos review` on a failed run still prints the accept, merge and reject commands,
  although only a review run can be accepted or rejected.
- Not exercised live: the Telegram card warning (the test bus runs without Telegram;
  covered by unit tests), a flagged run, `aos reject`, evidence removal after a
  decision, and the Linux (the server) database address (backlog AO-5).

## Re-run after the verification repairs: AO-1-r2, 2026-10-13 12:14Z

The first verification pass found that the test suite could delete real evidence
(AO-1-r1's was lost that way) and that evidence directories were keyed by run id
alone, so two buses reusing run ids collided. The repair (unit U9, `4cad866`) keys
evidence by a bus id that `GET /capabilities` returns. The test bus on 8011 was
restarted from that code with the same data directory:

```
{"evidence":true,"bus_id":"a76658214a361f2e063645d796a0ac28"}
```

`aos reject AO-1 --note "..."` on the review run AO-1-r1 (live): run `rejected`, task
back to `backlog`, the note stored as the error and as the report's last line, and
the printed hint `the rejected commits stay on the task branch; to discard them: git -C
<repo> branch -D ao/AO-1`. The branch was discarded in the proof clone, the task made
ready and run again with the same runner settings as above.

- Result: `10f6971 Start page: list the newest five notes, newest first` on `ao/AO-1`
  (`notes/app.py` +7/-2, `tests/test_notes.py` +12); harness exited 0 after 73 s;
  database at `192.168.5.2:32843`; fresh acceptance `review` / `pass`.
- Evidence: 33 files, 347577 bytes, under
  `~/.aos/evidence/a76658214a361f2e063645d796a0ac28/AO-1-r2/` (0700 directories, 0600
  files); the old-layout directory `~/.aos/evidence/AO-2-r1/` from the first runs is
  neither read nor removed by the new code.
- The 32-character database password (read back from the agent's saved evidence) does
  not occur in the bus's task JSON, nor its first 8 characters.
- Cleanup record: both sandboxes deleted, `postgres container removed`.

`aos review AO-1` (verbatim, screenshots and agent drives abridged to their headers):

```
run AO-1-r2: status=review
evidence dir: /Users/operator/.aos/evidence/a76658214a361f2e063645d796a0ac28/AO-1-r2
acceptance: bash scripts/check.sh -> exit 0
result acceptance/20261013T121612Z/result.txt:
  PASS total count grew by one
  PASS new note is first in the list
  PASS new note is visible in the list
  PASS row inserted in the notes table
result agent/20261013T121456Z/api-health/result.txt: (4 PASS)
result agent/20261013T121456Z/create-note/result.txt: (7 PASS)
result agent/20261013T121456Z/empty-title/result.txt: (4 PASS)
result agent/20261013T121456Z/recent-list/result.txt: (3 PASS)
screenshot: acceptance/20261013T121612Z/01-start-page.png
screenshot: acceptance/20261013T121612Z/02-after-save.png
(six agent screenshots)
agent directory: 20261013T121456Z
flags: none
accept: aos accept AO-1
merge: aos merge AO-1
reject: aos reject AO-1 --note TEXT
```

AO-1-r2 stays in review on the test bus so the evidence can be checked before a decision.

## Decision and removal, 2026-10-13 13:08Z

With the review fix (`43f5874`: decision lines only for a run in review, notes for
anything the bounded summary left out), on the same test bus:

- `aos review AO-2` ends with `no decision: run AO-2-r1 is failed`; `aos review AO-1`
  ends with the accept, merge and reject lines.
- `aos accept AO-1` printed `evidence AO-1-r2: flags: none` before acting; the run became
  `passed` and the task `done`, and
  `~/.aos/evidence/a76658214a361f2e063645d796a0ac28/AO-1-r2/` was removed (the bus
  directory is left empty). The old-layout `~/.aos/evidence/AO-2-r1/` stays, as the
  docs say.
