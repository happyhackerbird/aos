# The operator's own repositories on OpenShell (decision 6c, 2026-10-13)

Decision 6 deletes the host route only after the operator's own repositories each pass a
run on OpenShell under a policy. agent-os passed on 2026-10-13 (its policy is in the
repository root; Pi `667594b`, Claude `bcfae68` on the Mac gateway). This record covers
super-productivity and freelance_bot: what each needs from the harness image, the policy
each got, and one marker-task run each through `aos run --backend openshell`, driven by
`scripts/release_matrix.py` with the two repositories as fixtures (`--clone-from` points
the driver at a clone whose default branch carries the policy; the driver clones that and
removes `origin`, so the policy is read from the clone's own default branch).

## What the harness image needed

Measured in plain containers of `aos-harness:mvp` (user `sandbox`, 2 GiB, the clone from
a bundle so no `.env` or local databases are present, which is what a job clone gives a
sandbox) on the Linux server.

| Repository | Toolchain in the image | Measured |
|---|---|---|
| freelance_bot | **Poetry 2.1.3 added** (`ec5ea2d`): `pyproject.toml` pins `requires-python = "^3.11"` and its dependencies with Poetry's caret specifiers, which uv refuses (`Failed to parse version: Unexpected end of version specifier`); `poetry install --no-root` from `poetry.lock` (the lock file's own Poetry version) resolves in a few seconds, 224 MB venv under the sandbox user's cache | `pytest -q tests`: 3864 passed, 58 failed, 44 skipped in 5 min 46 s at the pushed tip `077ef99` (60 failed at the operator's local tip `c561655`). The failures are the repository's own: test fakes without the new `pre_frame` keyword, detached SQLAlchemy instances, `after_deepl` dictionaries that changed shape, in five files (`test_publish_draft`, `test_review`, `test_skills_years`, `test_task5_local_e2e`, `test_voice_pass`). The acceptance leaves those five files out so it measures the sandbox, not the drift. |
| super-productivity | Nothing added: Node 24.21 and npm 11 (the repository pins Node 22 in `.nvmrc`, no `engines`), Chromium for Karma (`CHROME_BIN=/usr/bin/chromium`) | `ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm ci --ignore-scripts`: 1846 packages in 29 s, 863 MB; the `prepare` steps (ts-patch, env, the four package builds): 12 s; `npm run test:tools`: 97 pass; `packages:test`, `release-notes:test`, `test:lint-rules`: pass; peak 1.26 GB. **Karma does not run in the image**: `ng test` for one subset (`src/app/util`) fails with `Angular compilation diagnostics failed` (assertion in the angular-compiler parallel worker) at 2 GiB (peak 1.67 GB) and with `compilation emit failed` at 3 GiB (peak 2.2 GB); cause not isolated (Node 24 against the pinned 22, or the worker's memory), so the acceptance is the node-only suites. |

## Policies

Both are the minimal v2 shape: `[execution] fail_closed = true` and one network grant.
A job clone carries tracked files only, so `.env`, local SQLite files, logs and backups
(all git-ignored in freelance_bot) never enter the sandbox; neither repository tracks a
secret, so no `exclude` entry is needed.

| Repository | `[network] allow` | Commit (in the proof clone) |
|---|---|---|
| freelance_bot | `pypi.org:443`, `files.pythonhosted.org:443` | `4041e01` on `main` (`077ef99`) |
| super-productivity | `registry.npmjs.org:443` | `f2461f7` on `master` (`fd9a8ea9c0`) |

## Sandbox upload limit on the Linux gateway

The super-productivity run on the Linux gateway ended `blocked` before the harness
started: `openshell sandbox upload` of the job tarball (208 MB: the clone's `.git` is
187 MB, the tree 76 MB) exited 1 with `ssh tar extract exited with status 74` after four
attempts, a partial `job.tgz` of 8 to 20 MB left in `/sandbox/incoming`. Measured with
random data into a probe sandbox under the same policy:

| Host (OpenShell 0.1.3) | 25 MB | 35 MB | 45 MB | 60 MB | 150 MB | 180 MB | 208 MB |
|---|---|---|---|---|---|---|---|
| Linux gateway (server) | ok, 1 s | ok, 1 s | fails, status 74 | fails | fails | | fails |
| Mac gateway (Colima) | ok, 2 s | | | ok, 5 s | ok, 13 s | fails, status 74 | fails |

The failed uploads leave a partial file (8 to 20 MB on Linux, 69 to 87 MB on the Mac) and
end after four attempts in about 9 s. The sandbox's filesystem is the host overlay with
room to spare (3 GB on Linux, 75 GB on the Mac), `ulimit -f` is unlimited, the sandbox
memory limit makes no difference (512 MiB and 2 GiB probes), and the gateway journal
records only the relay session; the failure is in the CLI's ssh sync path. The limit is
per host: about 40 MB on the Linux gateway, between 150 and 180 MB on the Mac gateway. A
shallow clone of super-productivity makes a 52 MB tarball, which a sandbox can still turn
into a result bundle on the host (checked: clone, commit on `ao/<task>`, `git bundle
create`, fetch into the shallow source), but the run from that shallow source ended
`blocked` at the runner's bundle step in the sandbox (`git bundle failed`, the output
starting with git's `info/grafts` deprecation hint; cause not isolated). So the runner
change came first: `upload_job` in `agent_os/openshell_runner.py` uploads a job tarball
above `UPLOAD_PART_BYTES` in numbered parts and has the sandbox join them and check size
and sha256 before `/sandbox/job.tgz` exists (`JOIN_PARTS`; both the job sandbox and the
fresh acceptance sandbox), which also removes the per-host difference.

The part size was measured too. With 24 MiB parts the Linux run got five parts through
and failed on the sixth; a probe with sequential 24 MiB parts into one sandbox failed on
the second. With 8 MiB parts, 32 sequential uploads (256 MB) all succeeded into one
sandbox on the same host minutes later. The failure therefore depends on the size of the
single transfer and varies per attempt, not on a cumulative volume; `UPLOAD_PART_BYTES` is
8 MiB.

## npm and the sandbox egress

Under the policy, `curl` reaches `registry.npmjs.org` (the sandbox resolves it to
`198.18.0.2`), but `npm ci` with its default of 15 parallel sockets fails: some connects
get `EACCES` from the egress and npm gives up (`The operation was rejected by your
operating system`), reproduced in a probe sandbox with the same rendered policy. With
`--maxsockets 4 --fetch-retries 5` the same install completes: 1845 packages in 32 s. The
acceptance commands carry those flags; an agent that runs `npm ci` itself in the job
sandbox needs them too (the Claude run reported the denial and did not get past it). The
refusal is not only a matter of concurrency: on the Mac gateway one acceptance with four
sockets (`mac-sp-proof-3`) still lost a single connect (`EACCES 198.18.0.2:443` on one
tarball fetch) and npm does not retry a connect error, so the acceptance command runs
`npm ci` a second time with two sockets when the first attempt fails; the second attempt
reuses what the first cached.

Measured further on the Mac gateway, in a probe sandbox under the same policy (evening of
2026-10-13): `curl` to the registry never failed (200 sequential requests, then 4 x 25 and
15 x 25 in parallel); Node's `fetch` failed once in 160 sequential requests with the same
`EACCES` on connect, and not at all in a further 200 (100 with, 100 without
`--no-network-family-autoselection`); the sandbox's resolver returns only the IPv4 fake
address `198.18.0.2`. So the refusal is rare, specific to Node's connect path, and
independent of address-family selection; an install that opens a few thousand
connections meets it almost every time, a single request almost never. On the Linux
gateway the same install at four sockets passed twice. The cause is in OpenShell's
egress (0.1.3 on Colima); this is the reproduction to report upstream.

## Proof runs

Driver: `scripts/release_matrix.py --fixture <repository> --harness claude --scenario success`
on the Mac gateway (image `aos-harness:mvp` `f127d68c236a` with Poetry; agent-os main with
the chunked upload), one disposable bus per run, evidence root shared with the day's
regression matrix.

| Repository | Task (run) | Duration | Status / verdict | Result | Acceptance in the fresh sandbox | Network |
|---|---|---|---|---|---|---|
| freelance_bot | AO-1 (AO-1-r1), work dir `mac-fb-proof-2` | 473 s | `review` / `pass` | `a390573` on `ao/AO-1` (the marker file, 621-byte bundle) | `poetry install --no-root` exit 0; pytest **3340 passed, 11 skipped in 3 min 29 s** (eight files left out, see above) | `pypi.org:443` x6, `files.pythonhosted.org:443` x7, provider `api.anthropic.com:443` x3, denied x2; effective policy matched; both sandboxes deleted |

Earlier freelance_bot attempt (`mac-fb-proof`): the same task, `failed` / `fail` on the
acceptance with 34 failures in `test_apply_worker.py`, `test_apply_worker_evidence_steps.py`
and `test_decisions_ui.py`; the agent's own report named the missing
`/sandbox/code/scope-agent/scripts/corpus_index.py`. Re-run in a container with the sandbox
layout: 32 of the failures are that missing sibling checkout, the rest the `.env`
database URL and an import-location assertion; in the full suite those tests pass only
through a fixture of one of the five files already left out.

super-productivity: the first attempts are in the upload section (`mac-sp-proof`, blocked
at the 219 MB upload; `mac-sp-proof-2`, shallow source, blocked at the bundle step;
`linux-sp-proof-2`, 24 MiB parts, blocked at the sixth part). With 8 MiB parts the run
from the full-history clone passed on the Linux gateway, the host with the lower limit:

| Repository | Task (run) | Duration | Status / verdict | Result | Acceptance in the fresh sandbox | Network |
|---|---|---|---|---|---|---|
| super-productivity | AO-1 (AO-1-r1), work dir `linux-sp-proof-3` on the Linux gateway, agent-os `c870ecd`, image `aos-harness:mvp` `582e667cbf3d` (the Linux image was rebuilt with Poetry after this run, once the server disk had room: `e82a4c59f353`, 20:14) | 166 s | `review` / `pass` | `dc55150` on `ao/AO-1` (the marker file, 622-byte bundle) | `npm ci --ignore-scripts --maxsockets 4` exit 0, the five `prepare` steps exit 0, `npm run test:tools` exit 0 (97 tests) | job sandbox: provider `api.anthropic.com:443` x3, denied x0 (the agent did not install anything); the acceptance sandbox fetched from `registry.npmjs.org:443` under the repository policy; effective policy matched; both sandboxes deleted |

The 219 MB job tarball went up as 28 parts of 8 MiB into each of the two sandboxes.

On the Mac gateway the same task did not pass: `mac-sp-proof-3` (24 MiB parts, which that
gateway accepts) and `mac-sp-proof-4` (8 MiB parts, install command with a second
attempt at two sockets) both reached the fresh acceptance sandbox and both lost a single
registry connect to `EACCES` in every `npm ci` attempt, while the probe sandbox under
the same policy had installed everything at four sockets minutes earlier. The agent's
run and the upload were fine each time (result `4a4f57d` and the fourth run's marker on
`ao/AO-1`, both sandboxes deleted). So a large npm install under the sandbox egress is
reliable on the Linux gateway and not on the Mac gateway as of OpenShell 0.1.3; the
cause is in the egress (every refused host is the allowed one), and a repository that
needs it on the Mac should be run on Linux until the vendor side is understood.

Conclusion for decision 6c: each of the operator's repositories has passed one run on
OpenShell under its own policy (freelance_bot on the Mac gateway, super-productivity on
the Linux gateway), with Poetry in the harness image and the chunked upload in the
runner as the two changes the proofs needed.
