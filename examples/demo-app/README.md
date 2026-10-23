# demo-app: the MVP verification workload

A deliberately small Flask application used by the OpenShell qualification
and [release checks](../../docs/release-validation.md), with the initial probe record
[docs/probes/openshell-2026-10-11.md](../../docs/probes/openshell-2026-10-11.md).
The AOS runner clones this directory as a job repository, a harness repairs the
defect inside the sandbox, and an independent acceptance run proves the repair
with a real database and a real browser.

## What the app does

`GET /` lists the rows of the table `items(name text, price_cents int)` and shows
their total in `<span id="total-cents">N</span>`. The seed data is three items
whose prices sum to **600** cents.

## The deliberate defect

`total_cents()` in `app.py` computes `sum(prices) - prices[0]`, which drops the
first item (the page shows 500 instead of 600). The fix is one line. The defect
is documented here on purpose: the demo is about verifying a repair, not about
hiding a bug.

## Verification

`verify.sh` runs the whole stack as an unprivileged user with no network beyond
the Python package index:

1. `initdb` a fresh Postgres cluster under `${TMPDIR:-/tmp}/demo-pg`, start it on
   `127.0.0.1:5433` with its unix socket in `/tmp`, create the `demo` database,
   apply `schema.sql` and `seed.sql`.
2. Create `/tmp/venv` if needed (override with `DEMO_VENV`) and install
   `requirements.txt`.
3. Start `app.py` on `127.0.0.1:8088`, wait up to 20 s for it to answer.
4. Run headless Chromium (`--headless=new --no-sandbox --no-zygote`, the shape
   that works inside the OpenShell boundary) to dump the DOM to
   `/tmp/demo-dom.html` and a screenshot to `/tmp/demo-shot.png`.
5. Assert the DOM contains `id="total-cents">600<`, print `VERIFY PASS` or
   `VERIFY FAIL: total was <value>`, and always stop the app and the database.

Exit code 0 means pass, 1 means fail. Nothing outside `/tmp` is written.

## Acceptance commands

See `acceptance.txt`:

```
bash verify.sh
/tmp/venv/bin/python -m pytest -q tests
```

`verify.sh` comes first because it creates `/tmp/venv`; in a fresh acceptance sandbox
nothing else does, and `pytest` through that interpreter would otherwise exit 127.

## Task prompt

`TASK.md` holds the exact prompt the harness receives.

## Synthetic secrets

`.env` and `secrets/local.txt` are **untracked, ignored local files** (see
`.gitignore`) with clearly synthetic values. Create them locally to exercise the
repository policy's `exclude` rule: a job copy is a clone, so untracked files never
reach the sandbox, and `provision` refuses an `exclude` entry that matches a
**tracked** path (a tracked secret stays readable through `git show`, and a
sanitized copy would make `git add -A` record its deletion). Do not commit them.
