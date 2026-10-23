# Live proof: `aos integrate --backend openshell` on the Mac, 2026-10-13

Historical internal design/directive: `2026-10-13-integrate-openshell` (not distributed).
Branch `integrate-openshell`
at `178a21c` (the code under test). The positive attempt was repeated at `8d19133`, after
the final review's `provision_job` fix (exclude handling never acts through a symlink) and
the documentation polish, with the same verdict and the same cleanup (posted 17:35:38Z,
sandbox deleted after 1.8 s). Mac, Colima, OpenShell 0.1.3 gateway, image `aos-harness:mvp`.

The question: can a merged candidate whose acceptance runs a browser pass `aos integrate`
on the Mac, where neither native sandbox can launch headless Chromium
([probe](runner-postgres-live-2026-10-11.md), finding 1), and does a candidate that only
breaks the browser check fail it?

## Setup

- **Bus:** the verify-acceptance test bus on `127.0.0.1:8011` (data under
  `~/.aos/test-bus-verify`, bus code equal to main `6c14c05`), not the shared bus on 8010.
  No repository was registered there, so no `test_command` ran; the task's single acceptance
  command `bash scripts/check.sh` (pytest, then the Playwright browser check) is the check.
- **Repository:** `~/code/aos-example-os` as in the
  [verify-acceptance proof](verify-acceptance-live-2026-10-12.md): local clone of aos-example
  with its origin removed, main `bed9bc9` (v2 `aos-policy.toml` granting `pypi.org:443` and
  `files.pythonhosted.org:443`; Chromium launched `--no-zygote --disable-gpu`), untracked
  `.aos/services.toml` with `[postgres]`. Main keeps the planted defect (the start page drops
  a saved note once there are more than five).
- **Candidates:** the review runs were staged by hand on the test bus against commits that
  earlier runs had produced (create, ready, claim, start, result `review`): AO-3 with
  `10f6971` (`ao/AO-1`: the fix, "Start page: list the newest five notes, newest first"),
  AO-4 with `ae36d35` (`ao/AO-2`: a README-only change, the negative control).
- **Command:** from the branch worktree,
  `env -u NODE_EXTRA_CA_CERTS AOS_URL=http://127.0.0.1:8011 AOS_HOST=mac AOS_REPO_ROOT=~/code AOS_WORKTREE_ROOT=~/.aos/worktrees AOS_OPENSHELL_PLATFORM=colima-mac AOS_OPENSHELL_IMAGE=aos-harness:mvp uv run aos integrate <task> --backend openshell`,
  AO-3 then AO-4.

## AO-3: the fix, `passed`

Sandbox `aos-ao-3-int` created 17:00:54Z, database container `aos-integrate-AO-3-postgres`;
the check posted 17:01:11Z; the runner printed (verbatim):

```
integration passed AO-3 10f69717 onto main bed9bc94
integration cleanup:
sandbox aos-ao-3-int deleted after 0.7 s
postgres container removed
```

Exit 0. The integration row on the bus: status `passed`, head `10f69717`, base `main`
`bed9bc94`, output (verbatim, 809 characters):

```
$ bash scripts/check.sh
Using CPython 3.12.15
Creating virtual environment at: .venv
   Building aos-example @ file:///sandbox/repo
Downloading pygments (1.2MiB)
Downloading playwright (45.7MiB)
Downloading psycopg-binary (6.5MiB)
 Downloaded pygments
      Built aos-example @ file:///sandbox/repo
 Downloaded psycopg-binary
 Downloaded playwright
Installed 19 packages in 65ms
.........                                                                [100%]
9 passed in 0.71s
PASS total count grew by one
PASS new note is first in the list
PASS new note is visible in the list
PASS row inserted in the notes table
note title: Verify 944ccf2e
list titles: ['Verify 944ccf2e', 'Seventh', 'Sixth', 'Water the plants', 'Book the dentist']
evidence: evidence/20261013T170106Z
[exit 0] (fresh sandbox aos-ao-3-int)
```

The browser check ran inside the fresh sandbox against the per-attempt database reached
through the `aos-services` grant (`192.168.5.2:<port>`); `DATABASE_URL` arrived through
`/sandbox/services.env`, so `check.sh` used it instead of starting its own database.

## AO-4: the README-only candidate, `failed`

Sandbox `aos-ao-4-int`, container `aos-integrate-AO-4-postgres`; posted 17:01:37Z:

```
integration failed AO-4 ae36d35 onto main bed9bc94
integration cleanup:
sandbox aos-ao-4-int deleted after 0.7 s
postgres container removed
```

Exit 1. The row: status `failed`, output ending (verbatim):

```
8 passed in 0.57s
PASS total count grew by one
FAIL new note is first in the list
FAIL new note is visible in the list
PASS row inserted in the notes table
note title: Verify 2902c9ce
list titles: ['Buy milk', 'Call the plumber', 'Renew passport', 'Book the dentist', 'Water the plants']
evidence: evidence/20261013T170131Z
[exit 1] (fresh sandbox aos-ao-4-int)
```

The unit tests pass on the unfixed code; only the browser step catches the defect, which is
the case `aos integrate` could not decide on the Mac before.

## Cleanup, checked afterwards

- `openshell sandbox list`: `No sandboxes found.`
- `docker ps -a --filter label=aos.service=postgres`: empty.
- `~/.aos/openshell/aos-ao-3-int/` and `aos-ao-4-int/`: `policy.yaml` only (no `owner.json`,
  `job.tgz`, `services.env` or `services.json`).
- `~/.aos/worktrees/aos-example-os/`: no `.integrate-*` scratch worktree; `git worktree list`
  in the source shows only its main checkout; no `integrate/*` branch exists in the source
  (the merged commit stays in the shared object store, never a ref).
- The database password appears in neither the printed lines nor the posted outputs.

## Timing

| Attempt | Sandbox created | Posted | Wall clock |
|---|---|---|---|
| AO-3 | 17:00:54Z | 17:01:11Z | 18 s |
| AO-4 | 17:01:12Z | 17:01:37Z | 26 s |

Each attempt: database start, policy rendering, clone and tarball, sandbox create, strict
effective-policy check, upload, `uv` environment build from the granted indexes, pytest,
Playwright Chromium, teardown.
