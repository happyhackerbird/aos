# OpenShell backend setup (executed 2026-10-10/11)

[Documentation index](../README.md)

The initial Linux/Colima qualification was executed on 2026-10-10/11. The adapter
now automates the
per-job parts described below. Later features have their own evidence; see
[release validation](../release-validation.md) for current gates and qualifications.
Nothing here claims vendor support beyond OpenShell's own
[support matrix](https://docs.nvidia.com/openshell/about/support-matrix).

## Linux worker host (Ubuntu 24.04, Docker Engine 29.5)

Prerequisites checked: kernel 6.8 with `landlock` in `/sys/kernel/security/lsm`,
Docker Engine ≥ 28 (`docker version`), glibc ≥ 2.28, about 3 GiB RAM free.

```sh
curl -LsSf -o install.sh https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh
OPENSHELL_VERSION=v0.1.3 OPENSHELL_INSTALL_METHOD=deb sh install.sh
openshell status                     # Connected, Authenticated (mTLS transport), Version 0.1.3
sudo loginctl enable-linger "$USER"  # keep the user service after logout
```

The gateway runs as the systemd user service `openshell-gateway` on
`https://127.0.0.1:17670`; its CLI bundle is `~/.config/openshell/gateways/openshell/mtls/`.
Restart it after any Docker runtime change (`systemctl --user restart openshell-gateway`).

## Mac worker host (macOS 14.7, Apple Silicon, Colima)

Colima must be 0.9.0 or newer so the VM runs Docker ≥ 28 (0.10.3 ships 29.9 after
`colima update`). Docker Desktop is not used.

```sh
brew upgrade colima && colima start && colima update
curl -LsSf -o install.sh https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh
OPENSHELL_VERSION=v0.1.3 sh install.sh    # Homebrew formula + brew service; fails to start until configured
cat > ~/.config/openshell/gateway.toml <<'TOML'
[openshell]
version = 2

[openshell.gateway]
compute_driver = "docker"

[openshell.drivers.docker]
socket_path = "/Users/<you>/.colima/default/docker.sock"
grpc_endpoint = "https://host.docker.internal:17670"
image_pull_policy = "if_not_present"
TOML
brew services restart nvidia/openshell/openshell
openshell status
```

`host.docker.internal` is forwarded by Colima to the Mac loopback and is in the
gateway certificate's SANs, which is why the supervisor can reach the gateway
without changing its bind address. With a hostname endpoint the supervisor gets
no `host.openshell.internal` alias, and `host.docker.internal` does not resolve inside
a workload either (measured 2026-10-12 and again on 2026-10-14: the supervisor
logs `policy_dns_trusted_gateway_unavailable`, so a bus rule naming the alias gives
the workload no reach). The sandbox therefore reaches the Mac bus at the IPv4 literal
`192.168.5.2`, the address the per-run database already uses: the runner renders the
`aos-bus` rule and writes the run file's `bus_url` from `agent_os.openshell_policy.bus_host`,
so both name that host (measured 2026-10-14). `AOS_OPENSHELL_BUS_HOST` overrides it with another IPv4 literal or one of
the bus aliases. The Mac bus may bind loopback only (`AOS_BIND=127.0.0.1 python -m
agent_os serve`) and stays reachable from the sandbox, because Lima's forwarding
delivers the connection to the Mac's `127.0.0.1`; a bus bound to `0.0.0.0` is
also exposed on the Mac's LAN and Tailscale addresses. On Linux the rule keeps the
`host.openshell.internal` alias, which does not reach a bus on a host whose firewall drops
incoming connections (measured on the server 2026-10-16: the alias resolves to `198.18.0.2` and
times out). Set `AOS_OPENSHELL_BUS_HOST` to an address
where the bus's port is published and a sandbox reaches it: a Docker-published `172.17.0.1:<port>`
or the server bus's own binding (`203.0.113.10`) both answered from a sandbox.

**Sandbox names and the bus prefix.** Every sandbox and state directory name starts with
the bus's prefix, and every per-run database carries it in its name and label: `a` and
the first two hex digits of the sha256 of `AOS_URL`'s host and port (`127.0.0.1:8020`
gives `a67`), or `AOS_OPENSHELL_SANDBOX_PREFIX` (2 or 3 lower-case letters or digits
starting with a letter, never `aos`), which overrides it. A run's sandboxes are
`<prefix>-<run>` and `<prefix>-<run>-acc`, its state directory
`$AOS_OPENSHELL_STATE_DIR/<prefix>-<run>/`, and its database `aos-<prefix>-<run id>-postgres`,
labelled `aos.run=<prefix>-<run id>` (integrate: `<prefix>-integrate-<task>`). The ownership
marker records the bus (`bus`, its host and port) and the prefix, and `reconcile` acts only
on markers its own bus wrote: its prefix on the directory and its bus in the marker. The
run's report says which prefix maps to which bus. Use one `AOS_URL` spelling per bus: two
spellings of one bus (`localhost` and `127.0.0.1`) give two prefixes, and each runner then
leaves the other's sandboxes to the operator. Every runner sharing a state directory must
run code with the prefix: an older runner reconciles every marker in the directory and can
delete a newer runner's live sandbox, so upgrade them together or give each bus's runners
their own `AOS_OPENSHELL_STATE_DIR`. Markers written before the prefix (`aos-...`, no `bus`
field) are never deleted by a prefixed runner, which reports them in one line per
`reconcile` pass; remove such a marker by hand once `openshell sandbox list` no longer
shows its sandboxes.

## Workload image

The workload image is pinned and committed: `docker/harness/Dockerfile` (see `docker/harness/README.md` for contents, bumping and checks). Build it on each worker host with `docker build -t aos-harness:mvp docker/harness` and select it with `AOS_OPENSHELL_IMAGE=aos-harness:mvp`; keep `image_pull_policy` at `if_not_present`. Measured facts that still matter: Claude Code's postinstall must run (no `--ignore-scripts` for it); at startup omp probes the model endpoints of providers it is not configured for, which appear as denied lookups in the run's `network:` line and are harmless (the line counts each allowed `host:port`, the `denied` total of refused TCP connections and refused DNS names, and `denied <destination>` for each refusal whose gateway line names it, so a denied host can be traced after the sandbox is gone); image ENV does not reach processes started inside an OpenShell sandbox, but `/etc/profile.d` does, so the image exports its uv and Playwright paths there. The historical hand-built images `aos-harness:probe`, `probe-app`, `probe-app-omp` and `verify` are superseded by `aos-harness:mvp`.

## The per-run database grant

A repository that requests `[postgres]` in `.aos/services.toml` ([Services](configuration.md#services))
gets one disposable PostgreSQL container per OpenShell run, started on the runner host
(`agent_os/openshell_services.py`) before the job sandbox is released. The runner
publishes it on `AOS_OPENSHELL_DB_BIND` and hands the sandboxes
`postgresql://aos:<password>@<AOS_OPENSHELL_DB_HOST>:<port>/aos` as `DATABASE_URL`
through `/sandbox/services.env`, which the main script and acceptance source when it
exists. Settings, defaults and the non-superuser login are in
[Evidence and OpenShell database settings](configuration.md#evidence). On a Linux host
both addresses default to `172.17.0.1` (the docker0 bridge gateway); a host whose
docker0 bridge address differs sets `AOS_OPENSHELL_DB_BIND` and `AOS_OPENSHELL_DB_HOST`.

The grant is the `aos-services` rule group of the rendered policy
(`agent_os/openshell_policy.py`): one endpoint, the host and port of that database,
for any binary. The host must be an IPv4 literal, and the port may not be the
gateway's. The reason for the literal is the one measured for the bus rule above:
a hostname such as `host.docker.internal` does not resolve inside the workload on
colima-mac, so a rule naming it gives the workload no reach; the sandbox is given the
address it can use (`192.168.5.2` on colima-mac). The same grant is rendered into the
acceptance policy when acceptance runs in a fresh sandbox. Before acceptance the
database is reset to a fresh state, and the container is removed when the run ends
(and by `reconcile` for a run whose runner was killed).

The image is the one in [Workload image](#workload-image): `docker/harness/Dockerfile`
builds `aos-harness:mvp` (its contents and bump procedure are in
`docker/harness/README.md`; no recipe is repeated here). The earlier hand-built
`aos-harness:verify` was a local test image only and is superseded.
The database container runs the `image` from `services.toml`, not the workload image.
Its login is not a superuser, so an extension a repository needs and PostgreSQL treats
as untrusted must be created by that database image at initialisation (an initdb
script).

## Worker credentials (never the operator's own stores)

- Claude Code: `claude setup-token` once, saved to a mode-600 file; attached as a
  provider from a custom profile with `auth_style: bearer`, `header_name: authorization`,
  endpoint `api.anthropic.com:443`, binaries node and bash:
  `CLAUDE_CODE_OAUTH_TOKEN="$(cat <file>)" openshell provider create --name aos-claude-oauth --type aos-claude-oauth --from-existing`.
  Claude interactive runs use this same provider boundary and setup-token; the
  credential is attached by OpenShell, not passed as a terminal argument or task data.
- Pi family (Pi or omp): the worker's `openai-codex` OAuth, exported as a Pi-schema
  `auth.json` (mode 600) and named by `AOS_OPENSHELL_PI_AUTH`. No OpenShell provider is
  attached for Pi (`AOS_OPENSHELL_PROVIDER_PI` refuses). The runner selects only the
  `openai-codex` entry, replaces its refresh token with a public placeholder and uploads
  that snapshot per run (Pi: `/tmp/pi-agent/auth.json`; omp: the same entry as a row of
  its SQLite store `/tmp/pi-agent/agent.db`, because omp ignores `auth.json`). Export and renewal: [Pi adapter](../pi-adapter.md#worker-auth-export-and-renewal).
  (2026-10-11 historical: Pi first ran on a workspace-scoped Anthropic API key through an
  `x-api-key` profile.)
- Codex: mint the ChatGPT worker login inside a Linux sandbox
  (`codex -c cli_auth_credentials_store=file login --device-auth`), download
  `auth.json` to a mode-600 file, upload it into each job sandbox's `CODEX_HOME`
  and download it back afterwards. The macOS CLI writes logins to the keychain
  even with `CODEX_HOME` set.
- Devin: make a login for the worker in its own data directory, which gets its own key,
  `XDG_DATA_HOME=~/.aos/worker/devin devin auth login`, then
  `chmod 600 ~/.aos/worker/devin/devin/credentials.toml` and name that file in
  `AOS_OPENSHELL_DEVIN_AUTH`. No OpenShell provider is attached
  (`AOS_OPENSHELL_PROVIDER_DEVIN` refuses). The runner uploads the file into each job
  sandbox and never into acceptance, which always runs in a fresh sandbox for this
  harness. The runner scrubs its `windsurf_api_key` from what it posts, and a result whose
  commits carry it is refused. Devin's documentation says the token does not expire by
  default, so revoke it rather than wait it out.
  Devin runs are headless only: no interactive sessions. With `AOS_OPENSHELL_COMMS` on, a
  run gets the six `aos` tools as `mcp__aos__aos_<verb>` and automatic delivery. `devin
  --config` supplies no MCP servers, so the command sets
  `XDG_CONFIG_HOME=/sandbox/devin-config` and the run's user files sit in
  `/sandbox/devin-config/devin`: `mcp_config.json` names the server, and the `hooks` key of
  `config.json` holds the `SessionStart`/`SessionEnd` receiver lifecycle hooks. Automatic
  mode also installs `PostToolUse` and `Stop`, running `aos-agent inbox --hook` and
  `aos-agent stop --hook`; `pull` retains the server and lifecycle hooks only. Messaging
  sets `subagents_enabled` false. Such a run has two secrets: the runner scrubs the claim
  token and the key, with their cut fragments,
  from what it posts, and refuses a result whose commits carry either. The run file names
  the key in `headless_secrets`, so `aos-agent` masks it from what the agent sends with the
  `aos` tools too; that needs a harness image with this helper.
  A repository can get between a run and that server: its `.devin/mcp_config.json` or
  `.devin/mcp_config.local.json` replaces a user server of the same name, and its
  `.devin/config.json`, `.devin/config.local.json` and `.devin/hooks.v1.json` can add hooks.
  A run whose starting commit has any of those files gets no messaging and carries on
  without it. The same applies to any entry (directory, file or symlink) at
  `.devin/skills`, `.devin/agents`, `.agents/skills`, `.agents/agents` or
  `.windsurf/skills`: skills and agent profiles can start children sharing the root's
  session id, even with `subagents_enabled` false. Presence alone disables messaging;
  no frontmatter is parsed. A `.devin`, `.agents` or `.windsurf` symlink or non-directory
  entry also disables messaging because it may alias those sources. Each note says why
  (`messaging: off for this Devin run: …`), as for a Codex run. The check reads the start
  commit, including the task branch's tip on a continuing run, not uncommitted files.
  Skills or profiles written during the run and nested `.devin` directories below the
  repository root are not covered; the coordinator measures runtime-created sources live
  after this repair. `.claude/skills` and `.cursor/skills` are excluded because
  `read_config_from` disables those imports. Other `.devin/` content, such as rules,
  still applies and leaves messaging on.
  Grant `server.codeium.com:443`: without it a run is refused before claim,
  because `devin -p` hangs instead of exiting when that host is denied. Its API, update,
  feature-flag and Sentry hosts can stay denied, as one live OpenShell run showed. The
  run's report holds all the agent's text across turns, not only its last message. These facts come from a live Devin probe on 2026-11-02,
  whose record, including what it did not measure, is kept private.

A sandbox policy must not carry its own L4 rule for a host a provider credential
binds to; the provider supplies that rule.

## The adapter: `python -m agent_os.openshell_runner run <task-id>`

`agent_os/openshell_runner.py` is the bus-integrated adapter (commit e483592). It claims
the task, resolves the repository's authoritative default, requires a v2 `aos-policy.toml`
there (absence refuses), renders it with `agent_os/openshell_policy.py`, provisions a
job copy on `ao/<task>` (an `exclude` entry matching a tracked path refuses), writes an
ownership marker, creates the sandbox whose main process waits for the job and runs the
harness (stdin closed; Pi reads its prompt file) under an in-sandbox deadline
(`timeout --kill-after=30`, bash watchdog fallback) and then sweeps every leftover job
process, reads the effective policy back and refuses a mismatch before it uploads
credentials or releases the job tarball (a tarball above 8 MiB goes up in numbered 8 MiB
parts that the sandbox joins and checks by size and digest, because `openshell sandbox
upload` fails on larger transfers at a size that varies per host and per attempt: 24 MiB
parts broke on the Linux gateway, 8 MiB parts went through 32 times in a row, the Mac
gateway took 150 MB whole and failed at 180 MB, measured 2026-10-13), runs the run's
frozen effective acceptance commands as fresh
execs (or, with fresh acceptance, in a new sandbox under a separately rendered acceptance
policy without harness hosts, strictly checked before release), bundles the result into
the source repository's `refs/heads/ao/<task>`, posts `review`, `failed` or `blocked`
with the acceptance output, and deletes the sandbox. A deletion that a successful
`sandbox list` does not confirm forces `blocked` and keeps the marker;
`python -m agent_os.openshell_runner reconcile` (also run best-effort at the start of every
run) deletes the sandboxes of marked runs that are terminal on the bus, removes the scratch
files such a run's killed runner left (job and result tarballs, bundle, auth snapshots),
and never touches an unmarked `aos-*` sandbox (live-proven 2026-10-12 after `kill -9` of a
runner mid-harness). Pi-family specifics are in the [Pi adapter](../pi-adapter.md).
The operator token is consumed through a
pipe before any untrusted step (the adapter re-executes itself without `AOS_TOKEN`).

**What the result may not touch, and what it is compared with.** Right after the fetch, before any
acceptance sandbox, the runner refuses a result that adds, changes or deletes a locked path since the run's
start (`aos-policy.toml`, `aos-workflow.toml` and the repository's `protected` patterns) and writes a change
report into the report's protected tail ([locked paths](configuration.md#locked-paths-and-the-change-report)).
The fresh acceptance and before-run (`<name>-pre`) sandboxes export `AOS_TEST_RESULTS=/sandbox/test-results`;
with a before-run, the runner lists the regular `*.xml` files there through `/usr/bin/python3 -I -c` and reads
each through `/usr/bin/head -c` (the direct form, no login shell), at most 20 files of 5 MiB, before deleting
the sandbox, and compares the two sides test by test
([before and after](configuration.md#before-and-after-the-agent)).

**Rollback ownership.** Cancellation or result refusal restores/deletes
`ao/<task>` only if it still names the exact commit imported from this run's bundle.
The comparison and ref update are atomic. A successor's different tip or an
externally deleted ref is left untouched, and the runner reports that its rollback
did not restore/delete the ref. The expected commit comes from the selected bundle
ref before import, not a later read of the shared branch.

**What a job copy carries.** `provision_job` builds each copy without cloning: `git init`,
a fetch by sha of the base commit from the source repository (any sha, also one no ref
names, through `uploadpack.allowAnySHA1InWant` on the command line; the source's config
is untouched), the job branch checked out there, and in a run's job copy and its fresh
acceptance copy the default branch under its plain name at the default sha (integrate's
copy has neither). The copy has no remote, no `origin/*` ref, no other branch and no
`FETCH_HEAD`, and only the objects those commits need, so acceptance commands compare
against the default branch by its plain name (`main`, not `origin/main`). Until
2026-10-15 the copy was a clone of the source, which carried every branch as
`refs/remotes/origin/*` with its objects, so every other task's `ao/` branch reached every
sandbox. A review task (`aos add --reviews AO-<m>`, or a proposal's `reviews`) also gets
`ao/AO-<m>` in its job copy, fetched from the source repository (a missing branch refuses
the run), and its prompt names that branch. The reviewed branch's tracked paths go through
the policy's `exclude` check too (a match refuses and names the branch). The check reads
the branch's path list only, never a file's content, its `.gitattributes` or a link's
target, so `export-ignore` cannot hide a file from it, and a tracked symlink is matched by
its own path and never followed. The run collects only `<default sha>..HEAD` into its own
`ao/<task>`, so the reviewed branch is never written back.

A run's job copy also carries a git identity, so the agent commits as whoever runs aos:
`AOS_GIT_NAME` and `AOS_GIT_EMAIL` when both are set (one alone is refused), otherwise the
source repository's effective `user.name` and `user.email` (its own config, then the global
one). It goes into the copy's own `.git/config`, and the run's notes say which source it came
from (`git identity: AOS_GIT_NAME and AOS_GIT_EMAIL` or `git identity: the host's user.name and
user.email`), never the values. With neither, the run is refused before its claim, since the
agent's first commit would fail: set the two variables in the runner's environment (a server's
`runner.env`) rather than root's global git config. The fresh acceptance copy and integrate's
copy carry no identity.

Before claiming, manual and dispatched workers read `aos-workflow.toml` at the
same authoritative default commit as policy, validate it even for explicit task
checks, and resolve task commands > workflow `acceptance` > workflow `test_command`.
The claim freezes checks and provenance for prompts, execution and evidence;
repository prompt text precedes runner rules and does not grant permissions.
Readiness only authorizes a task with a brief: missing checks or invalid config
refuses at the worker, without a fake run. There is no workflow sync/cache.
See [configuration and pre-rollout legacy migration](configuration.md#repository-workflow-file).

Operator environment (never from the repository):

| Variable | Meaning |
|---|---|
| `AOS_URL`, `AOS_TOKEN`, `AOS_HOST` (`mac`/`server`), `AOS_REPO_ROOT` | the usual runner settings |
| `AOS_OPENSHELL_PLATFORM` | `linux` (bus host `host.openshell.internal`) or `colima-mac` (bus host `192.168.5.2`); `AOS_OPENSHELL_BUS_HOST` overrides the host with an IPv4 literal or a bus alias |
| `AOS_OPENSHELL_IMAGE` | workload image with the harnesses (and Postgres/Chromium for app verification) |
| `AOS_OPENSHELL_PROVIDER_CLAUDE`, `_CODEX` | OpenShell provider attached per harness (credential placeholders); `_PI` and `_DEVIN` are refused for those runs |
| `AOS_OPENSHELL_PROVIDER_HOSTS` | hosts a provider credential binds (default `api.anthropic.com`); the repo may not grant them |
| `AOS_OPENSHELL_HARNESS_HOSTS_CODEX` (etc.) | operator-level harness API hosts, `host:port` list, rendered as the `harness` rule |
| `AOS_OPENSHELL_CODEX_AUTH` | worker `auth.json` uploaded per run and downloaded back afterwards |
| `AOS_OPENSHELL_DEVIN_AUTH` | worker `credentials.toml`, uploaded into each job sandbox; required for a Devin run |
| `AOS_OPENSHELL_DEVIN_MODEL` | model for Devin runs (`devin --model`); unset passes no `--model`; a value that is not a plain model name refuses |
| `AOS_OPENSHELL_PI_FLAVOUR`, `AOS_OPENSHELL_PI_BIN` | `pi` (default) or `omp`; binary name or absolute path (default: the flavour name) |
| `AOS_OPENSHELL_CLAUDE_MODEL` | model for Claude runs (`claude --model`); unset keeps Claude Code's default; a value that is not a plain model name refuses |
| `AOS_OPENSHELL_CLAUDE_PLUGINS` | comma list of host plugin directories uploaded per run to `/sandbox/plugins/<name>` and loaded with `--plugin-dir`; the run then allows the Skill tool and enables hooks so the plugins' own hooks fire; Claude only; default none |
| `AOS_OPENSHELL_CODEX_PLUGINS` | comma list of host directories, each named by its basename and holding any of `skills/<name>/SKILL.md`, `hooks.json` and `agents/*.toml`; uploaded per Codex run to `/sandbox/plugins/<name>` and copied into `CODEX_HOME`; a `hooks.json` turns hooks on with the trust bypass (messaging turns them on too, for the aos hooks), agent roles join the subagents every Codex run has; Codex only; default none |
| `AOS_OPENSHELL_PI_PLUGINS` | comma list of host directories, each named by its basename and holding any of `index.ts` or `index.js` (an extension, loaded with `-e`), `skills/<name>/SKILL.md` and `agents/*.md`; uploaded per Pi run to `/sandbox/plugins/<name>`; `pi` takes skills by `--skill` and loads no agents, `omp` gets skills and agents copied into its directories, where its `task` tool (on in every omp run) finds the agents; Pi only; default none |
| `AOS_OPENSHELL_DEVIN_PLUGINS` | comma list of host directories, each named by its basename and holding any of `skills/<name>/SKILL.md` and `agents/*.md`; with messaging off, uploaded per Devin run to `/sandbox/plugins/<name>` and copied into `/sandbox/devin-config/devin`; a `hooks.json` at a directory's top refuses the run before its claim, as do skills or agent profiles when messaging is requested, before any repository downgrade; Devin only; default none |
| `AOS_OPENSHELL_PI_PROVIDER`, `AOS_OPENSHELL_PI_MODEL` | provider (default `openai-codex` for `pi`; optional for `omp`) and model (required) |
| `AOS_OPENSHELL_PI_AUTH`, `AOS_OPENSHELL_PI_ACCOUNT_ID` | worker Pi-schema `auth.json` (this or `AOS_OPENSHELL_PI_LOGIN` is required for Pi); optional pinned worker `accountId` |
| `AOS_OPENSHELL_PI_LOGIN`, `AOS_OPENSHELL_PI_LOGIN_BIN` | the worker's persistent omp login directory on the host (mode 700, omp's `agent.db`), renewed before admission by the host omp (default `omp`) through `omp token openai-codex --force-refresh`; only its nonrenewable snapshot enters a job ([Pi adapter](../pi-adapter.md#persistent-worker-login-renewed-on-the-host)) |
| `AOS_OPENSHELL_HARNESS_HOSTS_PI` | must include `chatgpt.com:443` for `openai-codex` (`auth.openai.com` is unused with the nonrenewable snapshot, measured 2026-10-12) |
| `AOS_OPENSHELL_CPU`, `AOS_OPENSHELL_MEMORY`, `AOS_OPENSHELL_STATE_DIR`, `AOS_OPENSHELL_BUS_PORT` | resources, per-run state (policy, prompt, bundle), bus port if not in `AOS_URL` |
| `AOS_HARNESS_TIMEOUT_SECONDS`, `AOS_ACCEPT_TIMEOUT_SECONDS` | timeouts |

Invoke it as `aos run <task> --backend openshell` (or set `AOS_BACKEND=openshell`); the module entry point `python -m agent_os.openshell_runner run <task>` is equivalent. Acceptance runs in a new credential-free sandbox built from the result tree by default; `AOS_OPENSHELL_FRESH_ACCEPTANCE=0` runs it in the job sandbox instead, and Pi and Devin runs are always fresh.

`AOS_OPENSHELL_COMMS=1` gives Claude its `aos` tools, `/sandbox/aos-run.json` (mode 600, the run credential, deleted with the sandbox), `/sandbox/aos-mcp.json`, root `SessionStart`/`SessionEnd` lifecycle hooks and automatic `PostToolUse`/`Stop` hooks. Codex gets the same credential and six tools (namespace `mcp__aos`, functions `aos_<verb>`), the server named on its command line (`-c mcp_servers.aos.*`) and lifecycle/automatic hooks there (`-c hooks.SessionStart`, `SessionEnd`, `PostToolUse`, `Stop`). Messaging enables `features.hooks=true` and `--dangerously-bypass-hook-trust`. The default branch's `.codex/config.toml`, `.codex/hooks.json`, either leaf as a symlink, or a `.codex` entry that is not a directory still refuses messaging fail-closed; the report identifies the reason. No assumption is made that Codex ignores an aliased repository configuration.

OMP gets the same credential and six tools, its server in `/tmp/pi-agent/mcp.json`, the names in `--tools`, and the image-owned `/usr/local/lib/aos-agent/aos_pi.ts` as the final `-e` after operator extensions. Its `/tmp/pi-agent/config.yml` reads no repository MCP file. Native Pi gets `mcp__aos__aos_<verb>` through native MCP: `--no-mcp` is dropped, `-e builtin:mcp` is installed, and its `/tmp/pi-agent/mcp.json` uses `args: ["mcp"]`, direct exposure and a 1000-second timeout. Pi also loads the same final image adapter.

Devin gets the same credential and six tools as `mcp__aos__aos_<verb>`, the server in `/sandbox/devin-config/devin/mcp_config.json` and receiver `SessionStart`/`SessionEnd` hooks in that directory's `config.json`. Automatic mode adds `PostToolUse`/`Stop`; pull retains the server and lifecycle hooks without automatic delivery. Its starting commit's Devin settings, skill/profile sources or aliases disable messaging as described in the Devin login section above. This does not claim independent idle receive.

Messaging-on Devin runs set `subagents_enabled` false: the receiver owner is the hook
payload's `session_id`. The lifecycle hooks call `aos-agent receiver-start --hook` and
`receiver-end --hook` with timeout 3. `AOS_OPENSHELL_DEVIN_PLUGINS` directories containing
skills or `agents/*.md` are refused before claim when messaging is requested, in both
automatic and pull modes, even if repository settings would later disable messaging.
With messaging on, a Devin run takes no plugin skills or agents and no repository
skills or agent profiles from the five checked root sources. Comms-off runs still admit
and install plugin skills and agents. The agent can rewrite its own user config, the
same boundary the hooks already have; the start-commit and import exclusions above
remain limits of this isolation. The coordinator must measure live behaviour on pinned
Devin 3000.11.92 after this repair; no such qualification is claimed here.
Tool and hook presence is not proof of peer delivery.

`AOS_OPENSHELL_COMMS=pull` retains those explicit tools, credentials and lifecycle boundaries. Claude/Codex/Devin retain root `SessionStart`/`SessionEnd`, but not automatic `PostToolUse`/`Stop`; Codex retains the messaging hook trust flags. Pi/OMP still load `aos_pi.ts`, with `AOS_AGENT_AUTOMATIC=0` rather than `1`, so readiness still checks the adapter. Pull has no automatic delivery, finalization hold or idle nudge: the prompt says `This run delivers nothing automatically: call <inbox> yourself.` using the harness's inbox name. The report adds `messaging: pull only (no automatic delivery, no finalization hold)` before its bookkeeping summary. Interactive runner admission still requires `1`. The default is `1`; `AOS_OPENSHELL_COMMS=0` omits messaging/lifecycle wiring and credentials.

The per-run state directory (`$AOS_OPENSHELL_STATE_DIR/<prefix>-<run>/`, mode 700) also holds `owner.json` while a sandbox may exist, and for Pi the scrubbed `session.jsonl` and `pi-diagnostics.json` (mode 600). With `AOS_OPENSHELL_CLAUDE_PLUGINS` set, a Claude run also receives `/sandbox/plugins.tgz` (extracted to `/sandbox/plugins`, each plugin directory named by the `name` in its `.claude-plugin/plugin.json`, which must be plain and unique; the host directory's own name, such as an installed plugin's version directory, is not used), its `--settings` file allows `Skill(*)` and sets `disableAllHooks` false, and the report notes the plugin names and versions. The entries are resolved when a Claude run starts and a bad one refuses that run before any sandbox exists; Codex and Pi runs, `reconcile` and `aos integrate` never read them. The tarball ships everything under each root except `.git`, `__pycache__`, `*.pyc`, `.in_use`, `node_modules`, `.venv` and `.env*` files, so keep secrets out of a plugin root; it goes up in one piece, and a failed extraction fails the run instead of starting Claude without the plugins. The setting applies to every Claude run of the worker (there is no per-task switch), the fresh acceptance sandbox never receives the plugins, and with `AOS_OPENSHELL_FRESH_ACCEPTANCE=0` the acceptance commands run in the job sandbox where `/sandbox/plugins` exists. Skills, hooks and subagents from shipped plugins were measured inside a sandbox on 2026-10-14, along with a whole run with plugins loaded through this route. Other harnesses never read it.

Interactive tasks require `tmux` and coreutils `timeout` in the workload image,
plus the image-owned readable `/usr/local/lib/aos-agent/attach.lock`. The runner
creates the job sandbox with `/dev/ptmx` and `/dev/pts` in the policy's `read_write`
paths; acceptance and headless policies keep their usual paths. Omp and Claude Code
run in their actual interactive terminal mode under `timeout --foreground`; Claude
is launched without `-p` or stream output options. The initial task prompt travels
as one positional argument, capped at 65536 bytes before claiming. Both harnesses
need `AOS_OPENSHELL_COMMS=1` for messaging and `aos_submit`.

Claude interactive runs use the configured Claude provider; they do not need a Pi
worker credential. Their private run file protects the run's claim credential
even when no database or Pi credential exists. This keeps the helper's interactive
credential validation and `aos_submit` available without weakening its fail-closed
handling of malformed run files.
The first interactive Claude launch also checks `platform.claude.com:443`.
Grant it explicitly with
`AOS_OPENSHELL_HARNESS_HOSTS_CLAUDE=platform.claude.com:443`; the API provider's
`api.anthropic.com` route alone is insufficient for onboarding. This is an
operator grant, not an automatic widening of headless policies.
On a fresh sandbox, attach to acknowledge Claude's theme/security prompts and
explicitly trust the task's repository before the initial task turn starts.

Real Claude qualification (operator, 2026-10-27): source `f18e0a4` with the
claim-credential protection repair, Claude Code 2.1.296, image
`aos-harness:release-da47c25`
(`sha256:8bbd0338963d266254cd4535307bb597aca40908d28b714ab475ac51c204b686`).
The isolated bus was `172.17.0.1:8012`, with private repository/state roots and
prefix `c25`; production jobs and credentials were unchanged.
`aos run AO-1` launched a real Claude terminal. The operator attached with
`aos attach AO-1-r3`, completed onboarding, observed `READY_FOR_OPERATOR`,
detached and reattached the same run, observed a second-client refusal, and used
`--takeover` to detach the prior client. An operator question typed into the
attached terminal, submitted with a tmux Enter key, received `CLAUDE_REPLY_4`.
On `FINISH_PROOF`, Claude wrote and committed the checked result and called the
real MCP `aos_submit`. Submission `795a713f7d5f61bbac95c1fc0912bd5c815288d7`
reached `review`; fresh `python3 check.py` printed
`PASS real Claude interactive result` and exited 0. Both job and acceptance
sandboxes were deleted. This proves live interaction/submission, not the full
agent-os regression suite.

The earlier isolated attempts exercised managed early exit: without the explicit
`platform.claude.com:443` grant, Claude exited 1 before submission and the runner
recorded failure and deleted the sandbox. Temporary startup-screen capture
identified that prerequisite; no credential-bearing Claude transcript was kept
in the feature implementation. A lifecycle regression also runs the real MCP
helper on the generated Claude run credential so mocked readiness cannot hide
loss of `aos_submit`. The explicit startup grant is checked before claim.

Set up the gateway host before using this mode: enable the runner user's lingering
(`loginctl enable-linger <runner-user>`), configure a git name and
email (or `AOS_GIT_NAME` and `AOS_GIT_EMAIL`), and configure `runner.env` with
`AOS_URL`, `AOS_HOST=server`, `AOS_REPO_ROOT`, `AOS_OPENSHELL_PLATFORM=linux`,
`AOS_OPENSHELL_IMAGE`, `AOS_OPENSHELL_PI_FLAVOUR=omp`,
`AOS_OPENSHELL_PI_MODEL=openai-codex/<model>`, `AOS_OPENSHELL_PI_LOGIN` naming the
worker's persistent omp login (or `AOS_OPENSHELL_PI_AUTH` pointing to a worker credential
export), `AOS_OPENSHELL_HARNESS_HOSTS_PI=chatgpt.com:443`,
`AOS_OPENSHELL_COMMS=1`, `AOS_OPENSHELL_BUS_HOST` reachable from the sandbox,
`AOS_OPENSHELL_STATE_DIR` and the harness timeout. A coordinating parent needs
`AOS_DISPATCH_MAX_RUNS` at least one above the children it should run beside it
(the family capacity line names the host and its cap when it is filled). These are
setup requirements; writing the configuration does not deploy or qualify the image.

Qualified server (the interactive Herdr server proof,
2026-10-22): Ubuntu 24.04 x86_64 with Docker 29.5 and OpenShell 0.1.3, the gateway
running as the runner user's systemd user unit with lingering enabled, so it
survives every SSH session ending; Herdr 0.8.2 (the `herdr-linux-x86_64` release binary) running a
named server session (`herdr --session <name> server`) that the operator's SSH
clients join (`herdr --session <name>`); the harness image built from
`docker/harness/` at the proven revision (tmux, coreutils `timeout`, `aos-agent`
protocol 2.2 and the root-owned `attach.lock`); sandboxes reach the bus on the
docker bridge address (`AOS_OPENSHELL_BUS_HOST=172.17.0.1`, the bus published
there) and the provider on `chatgpt.com:443`; `aos dispatch` with the keys above
in its environment (the proof ran it in a host tmux session; a worker host
normally runs the [systemd unit](operations.md#dispatcher-on-a-worker-host)). To
qualify a change without touching a running dispatcher, run a second one beside it
with its own bus, repository root, state directory, sandbox prefix and log
directory.

Use `aos attach <run-id>` on the gateway host from a Herdr pane or any terminal
(over SSH from a laptop: `ssh <host>`, then the Herdr CLI against the server
session, or its TUI client; split a pane, put the operator bearer in its
environment, run the attach line the runner printed; the proof drove the panes
through the CLI over SSH).
Closing the pane, killing the Herdr client or losing the SSH connection leaves
the run, its deadline, its harness process and background jobs alone; reattaching
joins the same tmux session. A second attacher is refused (`already has an attached
client`) and `--takeover` detaches the existing client. `aos attach` refuses a run
that is not live (`is review, not live`, `is cancelled, not live`, `is blocked, not
live`), so reattach never restarts an ended run. Text goes to the harness's real
terminal: Claude Code receives normal terminal input; omp steering queued during
`mcp__aos_inbox_wait` arrives when the wait returns, or use
`aos ask <task> "<text>" --operator` to deliver through the bus.
The agent submits a report, a 40-character candidate commit and a child-result list
through `mcp__aos_submit(report, commit, children)`. Submission requests verification,
not acceptance. `/exit` without a submission fails. The harness timeout
(`AOS_HARNESS_TIMEOUT_SECONDS`) bounds an interactive run like a headless one: an
idle attached run ends `failed: harness timeout after <n> s`.
Detach leaves the run live; `aos cancel <task>` ends it on the bus, cascades to its
children, deletes the parent's sandbox and ends the attachment client; a live
headless child's runner still finishes its harness and acceptance before every post
is refused.

After submission the runner posts stage `acceptance`, closes the terminal input,
kills tmux and sweeps the sandbox user's processes. Before publishing quiet, the
main script captures the messaging summary, unsets `DATABASE_URL`, and retires
`/sandbox/aos-run.json`, `/sandbox/services.env`, `/tmp/pi-agent/agent.db` and its
`-wal`, `-shm` and `-journal` sidecars. The runner checks the quiet marker and runs a
shell-free process census: only the main script and its ancestors, its exact linger
sleep, and the census and its ancestors may remain. Unreadable or nonempty census
results block verification. It checks and bundles the submitted commit, verifies
its identity and descent on the host, then runs fresh acceptance on that commit in
a credential-free sandbox. Uncommitted edits after submission are noted; the sha
selects the result. The job sandbox remains Ready until deletion: a gateway holder
can still exec during this window. The retired credential files and the immutable
candidate narrow this limit; the attach command refuses past the harness stage.

Candidate export: `mcp__aos_export_candidate(commit)` asks the runner to check that
the commit exists in the job copy and descends from the run's base, bundle it from
a named ref, verify it on the host and fetch it into the source as
`refs/aos/candidates/<run>/<sha>`. A child proposed with `base=<sha>` starts there.

Import: `mcp__aos_import(task_id, run_id, commit)` selects a direct child's result.
The bus validates family membership, the newest run, `review`/`passed` status and
the result commit. The runner bundles `ao/<child>` from the source, uploads it to
`/sandbox/imports/<child>.bundle` and fetches it inside the parent sandbox as
`refs/aos/imports/<child>`; the parent merges it itself. A requeued or changed child
head makes the selection stale, visible in the family view, and needs a new import.
`mcp__aos_family(task_id=<child>)` reads that child's full bounded report and error
when the listing's excerpt is insufficient.

The runner serves one requested export or import per 5-second poll, oldest first, and
only while the harness runs within its deadline. An entry whose new state the bus
does not record stays `requested` and is served at most three times.

Combined acceptance: at submission the manifest `children` must equal the imported
selection, with no omission, duplicate or extra, and every non-cancelled child must
be terminal. Each manifest commit must be contained in the candidate, checked on
the host in the source. The integration order is derived from the candidate's
first-parent chain and recorded on the submission. The frozen acceptance of an
interactive task with its own acceptance is composed with the workflow's
`test_command` (`acceptance_source` is `task+workflow.test_command`), so repository-wide
checks run on the assembled revision. Two passing children never substitute for
this check. The parent reaches `review` only when that run passes; children never
auto-merge. The integrated behavior is checked by the run's verify leg.

OMP delivery is locally measured on 18.8.3, not server-deployed or native Pi/Codex
automatic-delivery verification. Each adapter instance consumes only its first
`session_start`: a valid object from `getHeader()` with absent/null `parentSession`
and `parentSessionId` may adopt a nonblank session ID. Separate native-child instances and missing/invalid
headers or IDs stay disabled, including later starts. Only headless owner successful final
assistant turns with stop reason `stop` and no tool results may queue the one follow-up;
stop failure gets one same-handler retry and a sanitized failure fallback. Requests
acknowledged during that continuation get no second one. This scopes automatic
handlers, not shared MCP/shell credentials.

Protocol **2.6** requires exact-run receiver lifecycle support in helper, adapter and
runner for both headless and interactive runs. Six helper tool names remain required
headless; interactive requires eleven: the six, `aos_submit` and the four family tools
(`aos_inbox_wait`, `aos_family`, `aos_import`, `aos_export_candidate`). Both automatic
and pull check compatible versions (same major, at least the required minor) and
adapter registration. These readiness controls never open receiving and are not
behavioral proof or proof of completed MCP negotiation. Failures preserve ordinary
tool results and expose sanitized
delivery uncertainty. The retained summary is bounded scrubbed bookkeeping, not a
receipt; retained sessions redact the run claim as well as selected worker secrets.
Fresh acceptance receives neither the messaging credential nor adapter wiring.
Comms-off omits that wiring, credential and prompt without reopening discovery.
The 2026-10-18 measurement covered the stdout/complete-ledger/best-effort-ack
contract, its rollback/contention limits and the unobservable
simultaneous-output-channel failure. Existing lifecycle, heartbeat,
cancellation, deadlines, result collection and cleanup controls remain in force.

### Exact-run receiver admission

Claim and start leave `receiver_state=pending`. An authenticated root lifecycle
operation opens receiving with its native `receiver_owner`; child hook payloads,
helper initialize/tools/list/EOF and dummy registration do not open it. The helper's
`receiver-start --hook` / `receiver-end --hook` commands require root session identity,
use a two-second total deadline, print no success context and fail visibly with a
bounded sanitized diagnostic. Stop/finalization is not native session end.

Pi/OMP preserve first-root-header and exact-ID ownership. Completed replacement
requires the original cached session file to equal `previousSessionFile` and the new
ID to differ. The adapter closes the old owner as `native_owner_replaced`, clears
timers and stays retired; cancellable switches, wrong/child files and same-ID reload
do nothing. Claude's old-end/new-start sequence cannot reopen a closed AOS receiver.

Fresh requests/notices require exactly one running, receiving, unsubmitted,
unexpired recipient run. Stored `to_run_id`, `admission`, `terminal_reason` and
`terminal_at` record the outcome; a valid unavailable send remains in history and CLI
returns 2. It cannot be pulled by a future run. Replies mark the original answer while
independently recording admission only to the original asking run, never its replacement.
Run-bound family/wait/delivery/ack/reply reads preserve that exact-run identity.

Submission ends admission without ending heartbeat/process liveness. Closure keeps
prior delivery/ack/answer evidence, adds immutable terminal facts and prevents further
delivery. Native exit/submission, runner timeout/error/startup handling and final
result/cancel/lease-expiry fallback cannot reopen receiving. The runner must confirm
bus closure before collection/quiescence/acceptance; an unconfirmed close is visible,
prevents acceptance and does not erase an existing primary error. Reserved `receiver`
bus facts and `run:receiver` JSONL record new transitions, not invented historical
events. Neither these facts nor automatic acknowledgements prove model receipt.

The column-absence SQLite migration is one immediate transaction. It irreversibly
marks old runs closed as `legacy_unqualified`, without owner/opened facts. Historical
messages remain legacy with no guessed recipient run; unfinished peer work is terminal
`legacy_unbound`. Operator questions remain answerable. Database reopen never assigns
those old rows to a newer incarnation. This migration has been tested only on disposable
local databases; no production migration or rollout is qualified.

The 2026-11-01 offline native lifecycle proof used the changed Store/API/helper/adapter,
an owned derivative of the immutable cached arm64 substrate, and Claude 2.1.296,
Codex 0.162.1, Pi 1.1.0 and OMP 18.12.1. Ordinary generated commands covered
automatic/pull/off, supported native children, Pi/OMP reconnect and readiness controls.
Actual OMP `/new` and Claude `/clear`, second ordinary tool turns and normal exit
preserved closure. Interactive Claude used a loopback Bedrock-gateway fixture with
disposable bearer only, not AWS credentials, real inference or direct-provider
connectivity qualification. Separate real-HTTP runner boundary smokes used the existing
simulated OpenShell transport, not a live gateway. Historical live qualifications
retain their dates and scope. SU2 unrelated-incoming wait interruption, SU3 replacement
of automatic/legacy ack evidence, arbitrary partition/SIGKILL guarantees and deployment
remain outside this change.


A Codex run reads `AOS_OPENSHELL_CODEX_PLUGINS` instead, following the Codex mechanisms measured on 2026-10-14. Each entry is a directory the operator assembles; it carries no manifest, so its basename names it, and the basename and every skill and role name must be plain. The run receives the directories in `/sandbox/plugins.tgz` (the same filter and one-piece upload as the Claude setting), extracted to `/sandbox/plugins/<name>`, and before Codex starts the main script copies each `skills/<skill>` (a directory holding `SKILL.md`) to `$CODEX_HOME/skills/<skill>`, the `hooks.json` to `$CODEX_HOME/hooks.json` and each `agents/<role>.toml` to `$CODEX_HOME/agents/`. A failed copy fails the run like a failed extraction, and the report's harness stderr carries `plugins: install failed` followed by the copy's own error. A hook's command finds its scripts under `/sandbox/plugins/<name>`. Subagents are on in every Codex run, as Claude's Agent tool is: the command passes `features.multi_agent=true` and `agents.enabled=true` with or without the setting (`agents.enabled` is the switch; `features.multi_agent=false` alone leaves `spawn_agent` on in codex-cli 0.162.1), so agent roles and skills need no flag. A `hooks.json` sets `features.hooks=true` and adds `--dangerously-bypass-hook-trust` (Codex skips untrusted hooks under `exec`; the bypass puts two `error` items at the head of the JSON stream, which the result ignores). With messaging on the command carries `features.hooks=true`, the bypass and root lifecycle tables; automatic mode additionally carries the two automatic aos hook tables whether or not a directory ships `hooks.json`, and an operator `hooks.json` fires beside them (measured: plugin and aos hooks on the same event both run, two context-producing PostToolUse hooks reach the model as two developer messages, the aos Stop gives one continuation). Two Stop hooks that both block are unmeasured and accepted as the plugin's own behaviour. With messaging off and no `hooks.json`, hooks stay off. A repository `.codex/hooks.json` created mid-run is an accepted, unmeasured limit: the refusal checks the default branch; whether Codex re-reads hook layers during a session is unmeasured. `features.plugins` and `features.apps` stay off either way. The entries are resolved when a Codex run starts. A missing or unreadable directory, a repeated name, a symlink under the directory that is absolute or resolves outside it (the tarball keeps a link as a link, so its target would be missing in the sandbox), a skill or role name two directories share (they land in one directory), a second `hooks.json` (Codex reads one), a `hooks.json` that is not a JSON object, or a directory with none of the three parts refuses that run before any sandbox exists. The report notes what each directory delivered, for example `plugins: kit [skills: stagehand; hooks.json; agents: attacker.toml]`, and a run whose own setting is unset while another harness's is set notes `plugins: none for <harness>`. As with Claude, the setting applies to every Codex run of the worker, and the fresh acceptance sandbox never receives the directories.

A Pi run reads `AOS_OPENSHELL_PI_PLUGINS`, following the Pi mechanisms measured on 2026-10-14. The directories are assembled, named, shipped and refused like the Codex ones (no `hooks.json` part: a Pi-family harness has no hooks file, and an extension's event handlers are its hooks). The parts are an extension (`index.ts`, else `index.js`, at the directory's top), `skills/<skill>/SKILL.md` and `agents/<role>.md`. An extension loads with `-e /sandbox/plugins/<name>/index.ts` on both flavours, under the run's `--no-extensions`; on `omp` a `--tools` allowlist, such as the one messaging passes, does not hide the extension's tools (measured in the live runs). The tarball skips `node_modules`, so an extension must load without a dependency tree of its own. On `pi`, each skill loads with `--skill /sandbox/plugins/<name>/skills/<skill>` under `--no-skills`, and agent files are not loaded: no subagent package works on pi 1.1.0, and the report names the unloaded files. On `omp`, the main script copies each skill to `$PI_CODING_AGENT_DIR/skills/<skill>` and each role to `/tmp/pi-home/.omp/agent/agents/`, where omp reads them. The command then drops `--no-skills` and adds `--skills=<the shipped names>`, so discovery runs filtered to those names, which also admits the repository's `.agents/skills` entries of the same names. omp's native `task` tool is on in every omp run, roles or not: the command passes no `--tools` allowlist, and with messaging on the allowlist it needs for the `aos` tools names `task` too. `-e`, `--skill` (pi) and `--skills` (omp) join the flags the capability check requires. A skill whose `SKILL.md` frontmatter `name` differs from its directory name is refused, because omp selects skills by name. A package that registers through `omp plugin link` needs discovery on, which this setting does not do. Live runs on 2026-10-15 showed both settings end to end through `aos run` on Codex, pi and omp.

A Devin run reads `AOS_OPENSHELL_DEVIN_PLUGINS`. The directories are assembled, named, shipped and refused like the Codex ones, with two parts: `skills/<skill>/SKILL.md` and `agents/<role>.md`. When messaging is requested, either part refuses before claim, even if the repository would later disable messaging. With messaging off, before Devin starts, the main script copies each skill to `/sandbox/devin-config/devin/skills/<skill>` and each role to `/sandbox/devin-config/devin/agents/`, the directories Devin reads under `XDG_CONFIG_HOME`; a failed copy fails the run as it does on Codex. Subagents need no switch with messaging off, since Devin has them on by default. A `hooks.json` at a directory's top refuses the run before its claim, because nothing would read it and the hook would silently not fire; a directory with neither part refuses too. The report notes what each directory delivered, the setting applies to every Devin run of the worker, and the fresh acceptance sandbox never receives the directories.

## Integration checks

`aos integrate <task> --backend openshell` runs the merged candidate's saved checks
plus the current authoritative workflow `test_command` in a fresh acceptance
sandbox, using `agent_os/openshell_integrate.py`. OpenShell is the only backend;
there is no host-execution fallback.
The merge is made on the host in the integrate scratch worktree; `provision_job` copies
the merged commit onto branch `integrate/<task>` with the policy's `exclude` paths
dropped (a tracked match refuses); `accept_fresh` creates the sandbox `<prefix>-<task>-int`
from `AOS_OPENSHELL_IMAGE` under the acceptance policy rendered from the default
branch's v2 `aos-policy.toml` (the repository's grants and the bus; no harness host, no
provider), checks the effective policy strictly before releasing the tree, and reports
each command's exit code and a 3000-character output tail. The image must carry what the
checks need (Playwright's Chromium for a browser step, launched `--no-zygote
--disable-gpu`). With `[postgres]` in `.aos/services.toml` the attempt gets its own
container `aos-<prefix>-integrate-<task>-postgres` (label `aos.run=<prefix>-integrate-<task>`), published
and granted as for runs (`aos-services`); `DATABASE_URL` reaches the sandbox through
`/sandbox/services.env`; the container is removed with the attempt, `--keep` included.

State lives in `$AOS_OPENSHELL_STATE_DIR/<prefix>-<task>-int/` (mode 700) with an ownership
marker `owner.json` of `kind: integrate` that records the integrate process (`pid`,
`pid_created`). A nonblocking advisory lock on the canonical state path is acquired
before marker inspection or shared-state/service mutation, and held through verdict
posting and cleanup. Its stable lock file lives under
`$AOS_OPENSHELL_STATE_DIR/.integrate-locks/`, outside removable scratch; do not remove
these lock files while workers may run. A losing invocation owns nothing to clean up.

The review run is not live on the bus while integrate runs, so `reconcile` takes
the same lock, rechecks that its marker snapshot is still current, and follows the
process: it keeps the marker while that process is alive on this host and otherwise
deletes the sandbox, stops the database when `services.json` is present, and removes
the scratch files and marker. A cleanup failure keeps the marker for `reconcile`
and never changes the posted verdict. An ended attempt releases its lock and cannot
clean up a later attempt when cleanup is called again.

This is local coordination, not a distributed lock: competing invocations must
share the same state root and all run the lock-aware version. Upgrade those workers
together. An earlier sandbox/database live proof ran on the Mac on 2026-10-13.
