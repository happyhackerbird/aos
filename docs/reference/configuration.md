# Configuration

[Documentation index](../README.md) · [Getting started](../getting-started.md)

## Repository permission policies

`aos-policy.toml` on the authoritative default branch controls OpenShell runs and
integration acceptance. Only the v2 shape is accepted: `execution.fail_closed=true`,
optional `filesystem.exclude` and `filesystem.caches`, and `network.allow` with
explicit host/port pairs. See the [complete example](../getting-started.md#add-the-repository-policy).
An exclusion matching a tracked path refuses the run. Path rules do not erase
copies or historical Git objects: keep secrets out of repository history.

The operator configures worker credentials and harness API hosts separately.
Acceptance defaults to a fresh sandbox without harness credentials or API grants.
There is no native Claude/Codex host-policy fallback.

## Repository workflow file

`aos-workflow.toml` sits at the repository root. It is read only at the
default-branch commit that the repository policy uses, never from a worktree or a
task branch, so a task cannot change it for its own run. A missing file means no
workflow; a repository with no commits has none either.

Three keys are allowed and any other key, including a table, is refused:

- `test_command`: a string. Surrounding whitespace is stripped; blank is refused,
  and so is any control character other than tab.
- `acceptance`: a list of strings. An empty list is refused (omit the key for no
  default). Entries are stripped; a blank entry or a control character other than
  tab refuses the file.
- `prompt`: a string. CRLF line endings become LF and the text is stripped. Blank
  is refused, so is any control character other than newline and tab, and the
  stripped text may not exceed 4096 bytes (bytes, not characters).

A file that does not parse, is not UTF-8, is not a regular file (a symbolic link or
a directory of that name), or breaks a rule above is an error, never a silent skip.

Both manual `aos run` and dispatcher-launched workers resolve this file before
claiming, at the same authoritative default-branch commit as `aos-policy.toml`.
The dispatcher only schedules work: there is no workflow sync command or bus cache.
The worker still requires its existing local repository checkout. It validates the
file even when the task supplies explicit commands; invalid configuration refuses
the run before a claim. A missing file is allowed only when the task supplies checks.

Command precedence is explicit task `acceptance`, then workflow `acceptance`,
then a one-command list from workflow `test_command`. No commands means worker
refusal, not successful zero-check execution. Readiness authorizes a task with a
nonempty brief; it does not validate repository configuration. The worker records
preflight refusals in task activity without creating a run.

Claim sends `expected_spec_hash` and atomically checks the task spec while saving
`effective_acceptance`, `acceptance_source`, `workflow_commit`, `workflow_ref` and
`workflow_sha` on the run (the three workflow fields are null without a file).
Defaults never overwrite the task's explicit fields. That immutable per-run command
snapshot drives the harness prompt, checks, timeouts and evidence. The repository's
`prompt` is included before runner rules, without an enable flag; it grants no
permissions. Integration uses the saved run checks plus the current authoritative
workflow `test_command`, dropping exact duplicates in order. Missing historical
snapshots refuse rather than invent checks. PR delivery records an actionable refusal
and still sends the review card; requeue and run the task again to obtain a trustworthy
snapshot before opening a PR or integrating.

### Migrate registry test commands before rollout

Before upgrading, export nonempty legacy SQLite `repos.test_command` values from
the bus database (for example with a read-only SQLite query:
`SELECT name, test_command FROM repos WHERE trim(coalesce(test_command, '')) <> '';`).
For every affected repository, commit the intended `test_command` to root
`aos-workflow.toml` on its authoritative default branch and ensure workers can read
that commit before rollout. Use TOML quoting appropriate to the actual command;
do not copy SQL output blindly into a file. Registry `--test` and API test-command
configuration are retired, and runtime resolution never falls back to those values.
The existing database column and values are preserved as migration data: do not
drop or erase them. Historical runs are backfilled only when nonempty task checks
and the matching task spec prove their origin; otherwise their snapshots stay absent.

## Bus environment

Set required `BUS_TOKEN` and optionally `BUS_DATA_DIR` (host default `~/.aos/data`,
resolved against the bus process's home directory). The bundled container image
sets `BUS_DATA_DIR=/data` to keep using its mounted persistent volume; an explicit
environment override still takes precedence.
Export environment variables before running; `.env.example` lists optional defaults.
An empty `TELEGRAM_BOT_TOKEN` disables Telegram; otherwise set `TELEGRAM_CHAT_NEEDS_YOU`.
The health interval defaults to 60 seconds and `FAIL_THRESHOLD` to 3.

The application does **not** automatically load a `.env` file. Export values in
the process environment. [.env.example](../../.env.example) is a template;
[Settings](../../agent_os/config.py) defines defaults and validation. The Compose
deployment loads its explicitly named env file separately.

Telegram requires the needs-you chat ID when its bot token is nonempty. Code chat
falls back to needs-you. GitHub is off without `GITHUB_TOKEN`.
The internal code reconciliation poll always runs and is health-monitored, so
already-merged tasks can finish local completion recovery without either
integration. GitHub and Telegram calls remain conditional on their credentials;
task, lease and request operations also remain available.

**Migration from the deployment-specific defaults:** non-container installations
that relied on `/data` must explicitly export `BUS_DATA_DIR=/data` to keep using
that directory. The bundled image preserves `/data` automatically. Explicit
values retain their behavior; data is not moved automatically. The bundled
Compose now reads `./.env` and publishes `127.0.0.1:8010`; deployments that need a
remote bind, another env-file or an external network must configure those explicitly.
Disabled lanes retain historical health outcomes with cadence `0`, but do not
generate stale alerts until re-enabled.

**Removed in AO-14:** the email and freelance lanes and the approval items
(`/inbox`, `/items`, `/approve`, `/skip`, `/done`, `python -m agent_os inbox`) are
gone, with their settings and `TELEGRAM_CHAT_DRAFTS`. Passing one of the removed
settings to `Settings` directly is an error; in the process environment they are
ignored. An existing database keeps its `items` table untouched; a new one has none.

| Setting | Default / meaning |
|---|---|
| `TASK_LEASE_SECONDS` | `600`; renewed by the runner heartbeat |
| `TASK_SWEEP_INTERVAL_SECONDS` | `60`; expired-lease sweep |
| `TASK_MAX_LIVE_RUNS_PER_REPO` | `3`; each task still has one live run |
| `AOS_REQUEST_ACK_SECONDS` | `900`; unacknowledged-request notification delay |
| `CODE_INTEGRATION_CHECK` | `require`; `off` disables the combined-check merge gate |

See [operations](operations.md) for GitHub lane behavior.

### Super Productivity source

Tasks read from Super Productivity's sync files in Dropbox (the `sp.poll` lane,
[operations](operations.md#super-productivity-source)). The lane is on when the
three Dropbox credentials, the folder and a project or tag are set; otherwise the
bus starts without it.

| Setting | Default / meaning |
|---|---|
| `AOS_SP_DROPBOX_APP_KEY`, `AOS_SP_DROPBOX_APP_SECRET` | The operator's own Dropbox app (`files.content.read` only) |
| `AOS_SP_DROPBOX_REFRESH_TOKEN` | From `python -m agent_os.sp_sync auth`, minted once with offline access |
| `AOS_SP_DROPBOX_PATH` | Folder holding `sync-data.json`; `/` for an app-folder app's root |
| `AOS_SP_PROJECT`, `AOS_SP_TAG` | Project title or tag title (as shown in the app) whose tasks are read; either or both |
| `AOS_SP_READY_TAG` | Empty: a task is released to ready automatically at the next poll. Set to a tag name to restore the gate: only a task carrying that tag is released and, with it removed, goes back to backlog |
| `AOS_SP_HOLD_TAG` | `hold`; a task carrying this tag (compared case-insensitively) is not released, and a ready task that gets it goes back to backlog before a claim |
| `AOS_SP_POLL_SECONDS` | `3600`; the poll interval and the lane's health cadence |
| `AOS_SP_SYNC_PASSWORD` | Empty; the app's sync encryption password, needed only for encrypted sync files |
| `AOS_SP_DROPBOX_API_BASE`, `AOS_SP_DROPBOX_CONTENT_BASE` | `https://api.dropboxapi.com`, `https://content.dropboxapi.com`; test hooks like `TELEGRAM_API_BASE`, pointed at a fake Dropbox in tests |

## CLI and runner environment

These are read directly by the CLI/runner, separately from bus `Settings`.

| Variable | Default / meaning |
|---|---|
| `AOS_URL` | `http://127.0.0.1:8010`; an explicit URL overrides the local default |
| `AOS_TOKEN` | Required for operator commands; use the bus's `BUS_TOKEN` |
| `AOS_HOST` | `mac`; supported values `mac`, `server` |
| `AOS_REPO_ROOT` | `~/code`; source checkout is `<root>/<repo name>` |
| `AOS_WORKTREE_ROOT` | `~/.aos/worktrees`; task worktrees under `<repo>/<task>` |
| `AOS_CLAUDE_BIN` | `claude`; host executable for the separate interaction-review command, not sandbox task runs |
| `AOS_BACKEND` | `openshell`; the only backend of both `aos run` and `aos integrate` |
| `AOS_HARNESS_TIMEOUT_SECONDS` | `7200` |
| `AOS_ACCEPT_TIMEOUT_SECONDS` | `900` |
| `AOS_PUSH` | `off`; `on` pushes `refs/heads/ao/<task>` from the source repository to its remote after acceptance and before the result post, whenever the run has a result commit and a verdict, and the result then carries `remote` ([delivery](operations.md#delivery-to-github)). Any other value refuses the run before the claim |
| `AOS_PUSH_REMOTE` | `origin`; the remote name the push goes to (its `git remote get-url --push` URL is what gets posted, credentials removed) |
| `AOS_PUSH_TOKEN_FILE` | Empty: the host's own git credentials; else a file holding a GitHub token, handed to git through a credential helper for the push only, so the token is never in argv, in a log or on the bus |
| `AOS_DISPATCH_POLL_SECONDS` | `60`; how often `aos dispatch` lists the ready tasks ([dispatcher](operations.md#dispatcher-on-a-worker-host)) |
| `AOS_DISPATCH_MAX_RUNS` | `1`; live runs this host may have at once: runs claimed or running on the bus under this host's worker name plus the dispatcher's own runners still alive. The operator raises it per host within the sandbox memory |
| `AOS_DISPATCH_COOLDOWN_SECONDS` | `900`; how long a task waits after its runner exited with the task still ready (a refusal before the claim, a claim conflict) |
| `AOS_DISPATCH_LOG_DIR` | `~/.aos/logs/dispatch`; one `<task>-<UTC stamp>.log` (mode 0600) per runner started |
| `AOS_RESOURCE_ROOT` | Sibling `resources` directory of `AOS_WORKTREE_ROOT` (normally `~/.aos/resources`); durable host ownership records |

## Regenerable cache policy

OpenShell validates `filesystem.caches` names but does not mount host caches.
The retired native runner's `.aos/cleanup.toml` and minimum-free-space settings
are not launch controls for sandboxed tasks. Manage the worker host's capacity
explicitly; sandbox CPU/memory limits do not reserve host disk space.

## Services

A repository requests a per-run PostgreSQL container through the local,
uncommitted `<source repo>/.aos/services.toml`. The runner reads it before claiming:

```toml
[postgres]
image = "postgres:15-alpine@sha256:f7d23353e1b15400d22ebe31189f4d314b87a4c129cc400c8c2d8d4ca127bf81"
```

The `[postgres]` table requests the service; `image` is its only key, optional, and
the value above is the default. An image must carry an `@sha256:` digest. Any other
table or key, a non-string image, an unpinned image or invalid TOML refuses the run
before the claim with `services.toml: <reason>`, and the task stays ready. When
`docker` is missing, its context cannot be discovered or the daemon does not answer,
the run is refused the same way with
`services: postgres requested but Docker is unavailable`. A failure after the claim
(pull, start, port or readiness) ends the run `failed` with
`postgres: <stage>: <reason>` before the harness starts.

The OpenShell runner provisions the container on the worker host and grants only
its selected address/port to the job and acceptance sandboxes. `DATABASE_URL`
travels in the run's private service environment, not a host Codex command line.
See [OpenShell database settings](#openshell-database-settings) for bind and route
configuration. The database is reset before acceptance, and cleanup/reconciliation
removes the run-owned container.

Known database passwords are redacted from runner reports and evidence summaries.
This is not a general secret-removal guarantee for arbitrary files, Git history or
secrets the agent invents or transforms.

`aos integrate` does the same for each combined check when the integrated
repository's `.aos/services.toml` requests it: one database for each attempt,
handed to the acceptance and repository test commands as `DATABASE_URL` and removed
with the attempt, `--keep` included, which retains the scratch worktree and not a
running service. An invalid file or missing Docker posts an `error` result before a
scratch worktree exists. An interrupted attempt can leave its
`aos-<prefix>-integrate-<task id>-postgres` container. OpenShell `reconcile` removes
it once the recorded owning process is gone; `--keep` never keeps a live database.

## Evidence

A repository can ask the runner to keep what proves a change. The declaration is the
local, uncommitted file `<source repo>/.aos/evidence.toml`, created by hand in each
host's checkout like `services.toml` (a committed `.aos/` path makes the runner skip
the push). The runner reads it before claiming, on the native and the OpenShell route:

```toml
dir = "evidence"
max_bytes = 52428800
```

`dir` is required: a repo-relative POSIX path (a trailing `/` is dropped) with no
empty, `.` or `..` segment, no backslash, not absolute and not under `.git` or
`.aos`. `max_bytes` is optional, an integer from 1 to 1073741824 (default 52428800,
50 MiB). Any other key, invalid TOML or a bad value refuses the run before the claim
with `evidence.toml: <reason>`, and the task stays ready. The directory must be
ignored by git in the source repository, checked as `git check-ignore -q <dir>/probe`;
otherwise the run is refused with `evidence.toml: <dir>/ is not ignored by git in the
source repository; add it to .gitignore`.

**What is kept, and where.** After acceptance the runner copies evidence to
`$AOS_EVIDENCE_ROOT/<bus id>/<run id>/` on the runner host (default `~/.aos/evidence`;
the bus id is a random 32-character token the bus creates once for its data directory
and returns from `GET /capabilities`), outside every worktree and sandbox, with modes 0700 for directories and 0600 for
files. The layout:

| Path | Content |
|---|---|
| `acceptance/` | files under `<dir>` that acceptance created or changed (compared by size and modification time before and after), same relative paths |
| `agent/` | files under the directory the agent named (see below), relative to it |
| `outputs/<i>.txt` | the output of the i-th acceptance command (zero-based), its last 1048576 bytes |
| `manifest.json` | the local record: files with sizes, skipped files, acceptance entries, results, screenshots, agent directory, flags |

On the OpenShell route, every file under the run's evidence directory, and the retained
Pi session file, is redacted of the run's database password and claim token before the
capture completes: the outputs, the manifest and the session file are redacted before
their first write, and the fetched `acceptance/` and `agent/` files are rewritten in place
right after extraction (a failed rewrite fails the capture).

At most 500 files are taken from each of `acceptance/` and `agent/`. Files are kept in
the order outputs, acceptance, agent while their running total stays at or under
`max_bytes`; the first file that would pass it and everything after it are skipped
and counted in a flag. Links and non-regular files are never copied. `results` lists
each kept `result.txt` (outside `outputs/`) with its `PASS ` and `FAIL ` lines;
`screenshots` lists kept `.png`, `.jpg` and `.jpeg` files.

**The agent's evidence field.** When the repository declares evidence, the task prompt
ends with one more line (after the verify-skill line, when there is one):

```text
Evidence: write what proves your change under <dir>/ in a new directory for this run (for example <dir>/<UTC stamp>), and end your final message with the line: Evidence directory: <dir>/<that directory>
```

The runner reads the last `Evidence directory:` field of the agent's final message.
The field is matched as `Evidence directory:` followed by `<dir>/` and a name, so
`Evidence directory: evidence/20261011T043423Z` and the same with the name in
backticks or the label in bold or italics are accepted, and trailing punctuation
(`. , ; : )` and `*`, `_`, backtick) is stripped. A name containing `<`, `>` or a
`..` segment is not accepted. The name ends at the
first whitespace, so a path with a space is not supported in this field. A name holding
a control character is refused, and the run is flagged as for a missing field.

**Flags.** A run whose evidence is incomplete is flagged, never failed: it still
reaches `review`. Each flag is stated only when true:

- `acceptance wrote no evidence under <dir>` (acceptance ran and wrote nothing)
- `no "Evidence directory: <dir>/..." line in the agent's final message`
- `the agent's evidence directory <dir>/<name> does not exist`
- `the agent's evidence directory <dir>/<name> holds nothing written during this run`
- `evidence over the cap: <n> files skipped`
- `evidence capture failed: <exception type>` (capture never raises; the run is not blocked)

The flags are put first in the run's report as `evidence flagged: ...` on the native
route (a note of the run on the OpenShell route), and appear on
`aos show`, `aos review`, the `aos accept` and `aos merge` output and the Telegram
code card ([Reviewing a run](operations.md#reviewing-a-run)).

**The bus summary and `GET /capabilities`.** The runner posts a bounded summary with
the result: `run_id`, `task_id`, `dir` (the host directory), `declared`, `file_count`,
`bytes`, `flags` (at most 10), `acceptance` (at most 20 entries of command, exit and
note, with `acceptance_omitted`), `results` (at most 8 files, those with a `FAIL` line
chosen first and the kept files listed in path order, each with at most 15 lines and
`lines_omitted`, the number of its lines left out; `results_omitted` counts the files
left out), `pass_total` and `fail_total` (the `PASS` and `FAIL` line counts over every
result file of the run), `screenshots` (at most 20 paths of at most 300 characters, with
`screenshots_omitted`) and `agent_dir`. Every string is cut to 300 characters
(result lines to 200), and each control character in a summary string becomes `?`. The summary is trimmed to 60000 bytes by dropping
result files one at a time from the end of the list, then screenshots, then acceptance
entries. The bus stores the summary on the run and
replaces anything over 65536 bytes, or not a JSON object, with a marker that shows as
the flag `evidence summary refused by the bus: <reason>`. `GET /capabilities`
(authenticated) returns `{"evidence": true, "bus_id": "<id>"}` on a bus that stores
summaries. A runner whose repository declares evidence asks for it before claiming
and refuses unless the bus returns `200`, `"evidence": true`, and a 32-character
lowercase hexadecimal `bus_id`. Upgrade an older bus before starting such runs.
There is no sandboxed `--dry-run`. Database passwords are redacted from posted summaries.

**The acceptance environment.** Fresh acceptance gets a new sandbox, the result
tree, declared services and repository network grants. It does not receive the
job's run credential, worker authentication or operator plugins. Setting
`AOS_OPENSHELL_FRESH_ACCEPTANCE=0` weakens that separation by reusing the job
sandbox; Pi-family runs always use fresh acceptance.

**OpenShell database settings.** On the OpenShell route a repository that requests
`[postgres]` in `.aos/services.toml` gets a per-run container on the runner host, and
the job and acceptance sandboxes reach it through a network grant
([setup](openshell-backend-setup.md#the-per-run-database-grant)). Two settings, both
IPv4 literals, say where:

| Variable | Meaning |
|---|---|
| `AOS_OPENSHELL_DB_BIND` | the address on the runner host where the container's port is published |
| `AOS_OPENSHELL_DB_HOST` | the address the sandbox reaches that port by |

On `AOS_OPENSHELL_PLATFORM=colima-mac` they default to `127.0.0.1` and `192.168.5.2`
(measured). On `linux` both default to `172.17.0.1`, the docker0 bridge gateway. A
Linux host whose docker0 bridge address is not `172.17.0.1` (a custom bridge address,
rootless Docker) sets both `AOS_OPENSHELL_DB_BIND` and `AOS_OPENSHELL_DB_HOST`;
without that the database start fails after the claim and the run ends blocked with
the address named. A value that is not an IPv4 literal is refused before the claim.

The login in `DATABASE_URL` is `aos`, created `NOSUPERUSER NOCREATEDB NOCREATEROLE`;
the container's superuser and its password stay on the runner. Because the login is
not a superuser, an extension that PostgreSQL treats as untrusted cannot be created by
the run's own commands: the image must create it at initialisation (an initdb script).
After the harness ends and before acceptance, the runner resets the database: it
resets the role's settings, terminates its connections, drops and recreates the
`aos` database and sets the password again, so no table, setting or object the login
could create survives into acceptance (large objects in the admin database remain,
and the login cannot reach them). A reset failure raises `postgres: reset: <reason>`
and the run does not reach acceptance.

## Task prompt instructions

Sandboxed tasks do not read the retired `.aos/runner-prompt.toml`. Commit repository
instructions in `aos-workflow.toml`; the runner freezes authoritative workflow
resolution with each claim and combines its prompt with the task brief and runner
rules. If the default commit contains `.claude/skills/verify/SKILL.md`, the prompt
asks the harness to read it by path. Operator plugins are configured separately;
they are never discovered by reading the repository's own harness configuration.
