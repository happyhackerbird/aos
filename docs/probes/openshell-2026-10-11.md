# OpenShell live probe, 2026-10-10/11 (the server, Docker driver)

Scope: the live backend experiments listed below, from the internal 2026-10-10 MVP investigation.
Operator decisions in force: the server is the disposable gateway host; the Mac
stays on Colima; worker credentials are minted separately; probes authorized
2026-10-11. No production container was touched. No credential value was
printed or placed in a sandbox in this record. Synthetic fixtures only.

Grades: **live-proven** = executed here with the quoted output; **documented**
= vendor page; **open** = not yet run.

## Host and install

| Item | Result |
|---|---|
| Host | Ubuntu 24.04.4, kernel 6.8.0-106-generic, x86_64, 4 vCPU, 7.76 GiB RAM (3.1 GiB available before install), Docker Engine 29.5.3, LSM `lockdown,capability,landlock,yama,apparmor` |
| Install | `OPENSHELL_VERSION=v0.1.3 OPENSHELL_INSTALL_METHOD=deb sh install.sh` (installer sha256 `15fa05f2…66410`, 1632 lines). Debian package 76.3 MB, systemd user service `openshell-gateway`, mTLS bundle generated, `openshell status`: Connected, Authenticated (mTLS transport), Version 0.1.3 |
| Gateway footprint | `openshell-gateway` RSS 70 MB at start, 115 MB after three sandboxes |
| Linger | `Linger=no` for root; the user service stops at logout unless `loginctl enable-linger` is set (documented) |

## Policy acceptance (live-proven)

- `process.run_as_user: "sandbox"` fails on the default image: `IdentityResolutionFailed: workload user 'sandbox' does not exist in the pinned image`. Numeric `"1000"` works.
- `binaries: [{ path: "/**" }]` is accepted, loads, and matches every executable (bash, a copied bash, python).
- `landlock.compatibility: hard_requirement` with a non-existent `read_only` entry (`/nonexistent-aos`) still starts the sandbox: missing paths are skipped even in hard mode (matches the schema page: "Both modes skip paths that are missing or unopenable").
- `openshell policy update … --add-endpoint host:port --binary "/**" --rule-name <r> --wait` applies live in under 10 s; `policy list` shows revisions 1→3 with the previous ones Superseded.
- `policy get --full` prints the effective policy with `Status: Effective, Source: sandbox`.

## Boundary from inside `sandbox exec` (live-proven, sandbox `aos-p1`)

Policy: include_workdir, rw `/tmp` `/dev/null`, ro baseline, Landlock hard, uid 1000, one rule `pypi.org:443` for `/**`.

| Probe | Result |
|---|---|
| Capabilities | `CapEff 0000000000000000`, AppArmor `docker-default (enforce)` |
| Environment | only `HOME PATH PWD SHELL TERM USER OPENSHELL_SANDBOX` plus CA-bundle variables (`SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `DENO_CERT`) |
| Filesystem | `ls /` denied; `/root` denied; `/.openshell` denied; `touch /etc/x` and `/usr/x` denied; `/tmp` and `/sandbox` writable; symlink from the workdir to `/etc/shadow` denied on read |
| DNS | every name answers with a `198.18.0.0/15` placeholder, listed or not (`pypi.org → 198.18.0.2`, `example.com → 198.18.0.3`); AAAA empty |
| TCP allowed host:port | `pypi.org:443` connects |
| TCP unlisted host | `example.com:443` → `connect: Permission denied` |
| TCP listed host, other port | `pypi.org:80` denied |
| Raw IP | `1.1.1.1:443` denied |
| UDP | `1.1.1.1:53` denied |
| Metadata | `169.254.169.254:80` denied; supervisor log `transparent_tcp_policy_denied` |
| Docker bridge gateway | `172.17.0.1:2375` denied |
| Sandbox loopback | `127.0.0.1:22` → `Connection refused` (reachable, nothing listening) |
| Bus on the gateway host | denied until a rule `host.openshell.internal:8010` was added; then `GET /health` on the AOS bus succeeded (`host.openshell.internal → 198.18.0.4`) |
| Gateway port | a rule `host.openshell.internal:17670` makes the gateway's mTLS listener reachable from the sandbox. AOS must never render a rule for the gateway port; the sandbox holds no client certificate, so this is reach, not access |

Denial log lines carry the binary, destination and reason, for example
`NET:OPEN [MED] DENIED /usr/bin/bash(0) -> 169.254.169.254:80 [reason:transparent_tcp_policy_denied]`.

## Binary identity (TOFU hashing), live-proven

`cp /bin/bash /tmp/mybash` connects on first use. After `printf x >> /tmp/mybash`
the same path is denied: `Binary integrity violation: /tmp/mybash executable changed`
(`reason:transparent_tcp_identity_unavailable`). Replacing the file with a fresh
copy at the same path stays denied. A later policy update does not reset it.
Consequence: a rebuilt executable at a path that already connected loses network
for the life of the sandbox. Interpreters that are only invoked on changed
scripts are unaffected (scripts match their interpreter). Lane S1 measures the
venv/pip case.

## Container configuration (live-proven, `docker inspect`)

| | Workload | Supervisor |
|---|---|---|
| NetworkMode | `none` | `host` |
| CapAdd / CapDrop | none / `ALL` | none / `ALL` |
| SecurityOpt | `no-new-privileges:true` | `no-new-privileges:true` |
| Privileged | false | false |
| PidsLimit | 2048 | unset |
| ReadonlyRootfs | false | true |
| User | `1000:1000` | `65534:65534` |
| Mounts | channel volume rw at `/.openshell/channel` | channel volume ro, supervisor state volume ro |
| Memory at idle | 6.1 MiB | 15.6 MiB |

Default Docker seccomp and AppArmor apply; nothing privileged. This is the shape
the Docker composition would otherwise hand-build.

## Lifecycle timing (live-proven)

| Step | Time |
|---|---|
| `sandbox create` (image present) to Ready | about 1.3 s |
| `sandbox create --upload` of a 26 MB checkout, gitignore-filtered to 9 MB | 1.8 s including provisioning |
| `sandbox download` of 9 MB | 0.7 s |
| `sandbox delete` returns ("deletion accepted; cleanup is pending") | 5.3 s |
| Containers gone after delete | within 1 s of the return |

`--upload` honours `.gitignore` and did not upload `.git`. `sandbox upload … --no-git-ignore`
of the 25 MB clone with `.git` (2208 files) failed three times with
`ssh tar extract exited with status exit status: 74` after 971 files. Lane S1 diagnoses it.

## Bearer placeholder for a Claude setup-token (live-proven mechanics, synthetic value)

A custom provider profile (`id: aos-bearer-test`, credential `env_vars: [CLAUDE_CODE_OAUTH_TOKEN]`,
`auth_style: bearer`, `header_name: authorization`, endpoint `host.openshell.internal:8123`,
`protocol: rest`, `enforcement: enforce`) imported with `openshell profile import`, provider
created with `--from-existing` from a synthetic value, attached to sandbox `aos-c1` built from
the harness image. A host listener on `127.0.0.1:8123` logged request headers.

| Request from the sandbox | Listener saw |
|---|---|
| `env` | `CLAUDE_CODE_OAUTH_TOKEN=openshell:resolve:env:v…_CLAUDE_CODE_OAUTH_TOKEN` (placeholder only; no synthetic value anywhere in the environment) |
| `Authorization: Bearer $CLAUDE_CODE_OAUTH_TOKEN` via curl | the real synthetic value |
| same header via `/usr/bin/python3 urllib` (binary not in the profile's `binaries`, but a child of `bash -c`) | the real synthetic value; the parent-match rule applies |
| `?token=$CLAUDE_CODE_OAUTH_TOKEN` in the query | the real synthetic value |
| `-d token=$CLAUDE_CODE_OAUTH_TOKEN` in a POST body | placeholder unchanged (`request_body_credential_rewrite` is off by default) |
| `Authorization: Bearer not-the-placeholder` | passed through verbatim |
| same header to `host.openshell.internal:8010` (not the credential's endpoint) | `transparent_tcp_mapping_denied`: the mapping authorizes only port 8123 |
| `openshell sandbox get -o json`, `openshell provider get -o json`, `policy get --full` | zero occurrences of the synthetic value; the effective policy shows the provider rule as `_provider_aos_bearer_test` |

The default image (`nvcr.io/nvidia/base/ubuntu:24.04`) has no `curl`; the supervisor log
line `TLS termination enabled: ephemeral CA generated` and the CA-bundle variables explain
how HTTPS endpoints would be inspected. Claude Code sends its OAuth token as a bearer header
(documented), so this mechanism fits; whether Claude Code accepts a placeholder-shaped token
string locally (length/format checks) is **open** until a worker token exists.

## Workload under OpenShell (lane S1, live-proven, sandboxes `aos-h1`, `aos-h2`)

Image `aos-harness:probe` (node:24-bookworm-slim, Claude Code 2.1.296, Codex 0.162.1,
Pi 1.1.0, git, python3, curl; uid 1000; WORKDIR `/sandbox`). Policy `policy-h1.yaml`:
include_workdir, rw `/tmp` `/dev/null` `/home/sandbox`, ro baseline, Landlock hard,
uid 1000, `binaries: /**` on every rule, endpoints `host.openshell.internal:8010` and
`:9418`, `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`, `github.com`,
`api.anthropic.com` at 443. `--cpu 2 --memory 2Gi`.

| Step | Result |
|---|---|
| Provisioning by clone | host-side read-only `git daemon` on `127.0.0.1:9418`; inside `git clone git://host.openshell.internal/job-clone` 1.03 s, HEAD matches |
| Provisioning by upload | `sandbox upload … --no-git-ignore` fails on **file count**, not bytes: 1500 × 10 KB files 0.47 s, 2000 × 10 KB exit 74 every time, the 2208-file clone exit 74 every time; a 16 MB `.git` of 1951 files 0.5 s; an 11.2 MB `.tgz` as one file 0.27 s and extracted inside in under a second with `.git` intact |
| Egress | `pypi.org/simple` 200; `example.com` connect refused by policy |
| Bus | `host.openshell.internal` is the gateway host's **loopback** (supervisor `ExtraHosts host.openshell.internal:127.0.0.1`); the AOS bus publishes only on the Tailscale address, so `/health` gave an empty reply until a loopback listener was used (`200`). The bus must also listen on `127.0.0.1` of the gateway host |
| Develop | `python3 -m venv` 5.2 s; `pip install -e . pytest` 19.2 s; `pytest tests/test_policy.py` 92 passed, 1 skipped (the suite's own skip), 10.5 s; `npm view express version` 1.6 s |
| Memory | workload 118 → 168 MiB during pip; supervisor 20–22 MiB; gateway RSS 70 MB after install → 212 MB after the probe day (growth not characterised) |
| TOFU | venv `python` is a symlink to `/usr/bin/python3.11`; editing scripts and `pip install --upgrade pip` keep network; a copied interpreter at a new path connects once and is denied after one appended byte, for the sandbox's lifetime |
| Extraction without receive-pack | inside: `git commit` + `git bundle create /sandbox/out.bundle origin/HEAD..HEAD` (554 B); `sandbox download` only works for paths under the working directory (`/tmp` refused, documented); host `git bundle verify` OK and `git fetch <bundle>` lands the commit |
| App, database, browser | image `aos-harness:probe-app` adds postgresql 15, chromium, fonts; `initdb` + `pg_ctl -k /tmp -p 5433 -c listen_addresses=127.0.0.1` + `psql select 1` work; `python3 -m http.server --bind 127.0.0.1` 200; Chromium: own sandbox unavailable (unprivileged user namespaces blocked, host `apparmor_restrict_unprivileged_userns=1`), `--no-sandbox` alone fails (`Zygote could not fork`), `--headless=new --no-sandbox --no-zygote --disable-gpu --disable-dev-shm-usage --screenshot` **works** (9398-byte PNG); the supervisor denied Chromium's own egress to Google update hosts |
| Harness start, no credentials | Claude `-p … --output-format json`: `is_error: true`, `Not logged in · Please run /login`, exit 1; Codex `exec --json --skip-git-repo-check`: starts a thread, then `wss://api.openai.com` connect denied (no rule), exit on reconnects; Pi `-p`: `No API key found`, exit 1. No Landlock or seccomp crash |
| Cancellation | detached postgres from an earlier `exec` survived across exec calls; `sandbox delete` returned in 5.4 s and both containers were gone within 1 s; nothing left on the host |

## Mac with Colima (lane M1, live-proven)

- `OPENSHELL_VERSION=v0.1.3 sh install.sh` installs a Homebrew formula and a brew
  service; the gateway failed to start because auto-detection finds no driver on a
  Colima host (`no compute driver configured and auto-detection found no suitable
  installed driver`).
- With `~/.config/openshell/gateway.toml` setting `compute_driver = "docker"` and
  `socket_path = "/Users/operator/.colima/default/docker.sock"` the gateway starts and
  `openshell status` is Connected; `openshell doctor check` reports Docker 27.4.0 as ok.
- `sandbox create` fails at provisioning: `BoundaryConfigWriteFailed: stage Docker
  sandbox bundle: Docker responded with status code 500: getent unable to find entry
  "1000:1000" in passwd database`. Reproduced outside OpenShell: Colima's Docker 27.4
  rejects `docker cp -a` into a container created with a numeric `--user 1000:1000`;
  Docker 29.5.3 on the server accepts it. This is the documented Docker ≥ 28 floor
  surfacing at runtime.
- Next obstacle, untested: the supervisor dials `https://127.0.0.1:17670` by default,
  which inside the Lima VM is the VM's own loopback. The Mac's loopback is reachable
  from the VM and from a host-network container at `192.168.5.2`
  (`host.lima.internal`); `grpc_endpoint = "https://192.168.5.2:17670"` is the
  candidate, and the server certificate's SANs do not include that address.
- Colima release notes: v0.9.0 bundles Docker 28.4.0, v0.10.0 Docker 29.2.0, v0.10.2
  Docker 29.5.2 (documented). The installed Colima is 0.8.1; Homebrew offers 0.10.3.
- Colima 0.8.1 guest: Ubuntu 24.04.1, kernel 6.8.0-50, LSM list includes `landlock`;
  `--internal` networks do not resolve external names; the internal bridge gateway
  reaches the VM's sshd but not Mac-host services; `DOCKER-USER` exists and is empty.
- Left in place: OpenShell 0.1.3 and the brew service running with the Colima socket
  pinned; no sandboxes.

## Docker-composition fallback (lane S2, the server, live-proven)

| Probe | Result |
|---|---|
| `--internal` network DNS | embedded DNS returns nothing for external names (`getent` rc 2), no default route; no DNS exfiltration channel observed on Docker 29.5.3 |
| Bridge gateway IP | `17670` closed (gateway bound to loopback); **sshd on 0.0.0.0 answers on the internal bridge gateway**; the bus on the Tailscale address unreachable (no route); `host-gateway` resolves to the default bridge's gateway and is unreachable from the internal network |
| Squid 6.13 (`ubuntu/squid`) | `dstdomain -n` plus `deny all`: pypi 200, `.npmjs.org` wildcard 200, example.com `TCP_DENIED/403`, `1.1.1.1` CONNECT `TCP_DENIED/403`, `--noproxy` timeout; `access_log stdio:/dev/stdout` aborts the image, use the default file; curl honours only lowercase `http_proxy` for plain http |
| Nested path mask | tmpfs over a directory and `/dev/null` over a file hide the secrets; as root the 0500 tmpfs is still writable; `ln` across the mask fails (`Cross-device link`); the masked paths change type (device, empty dir) |
| Kill | `docker rm -f` 0.2 s; every process in the PID namespace gone |
| User namespaces | `unshare -Ur` EPERM in a default container (also with `--cap-drop ALL`); host `apparmor_restrict_unprivileged_userns=1` |

## Claude and Pi authentication, delegation, real task (lane S3, live-proven, worker token)

The worker token is a fresh `claude setup-token` minted by the operator into
`~/.aos/worker/claude-oauth-token` and copied to the gateway host with mode 600. It
was passed only as `CLAUDE_CODE_OAUTH_TOKEN="$(cat …)" openshell provider create
--from-existing`; every captured output was grepped for its prefix (0 hits except
the gateway's own credential store, below).

| Step | Result |
|---|---|
| Provider | profile `aos-claude-oauth` (credential env_vars `[CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_OAUTH_TOKEN]`, bearer, `authorization`, endpoint `api.anthropic.com:443` rest/enforce, binaries node and bash) imported; provider created from the file |
| Policy constraint | `sandbox create` refused the sandbox policy's own L4 rule for `api.anthropic.com`: "credentialed endpoint … uses L4-only; configure L7 inspection or explicitly set allow_uninspected_credentials: true". The provider rule must be the only rule for a credentialed host |
| Inside | `CLAUDE_CODE_OAUTH_TOKEN` holds the placeholder; `ANTHROPIC_OAUTH_TOKEN` is **empty** (only the first `env_vars` alias is injected); no token prefix in `env` |
| Claude reply | `claude -p 'Reply with exactly OK' --output-format json`: `is_error false`, `result "OK"`, 1 turn, $0.0213, exit 0. Traffic only `api.anthropic.com:443`; **no request to `platform.claude.com` or `claude.ai`** with a setup-token and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` |
| Delegation | settings `dontAsk`, `Agent(*)` allowed, WebFetch/WebSearch/mcp denied, inner sandbox off, hooks off: the Agent tool subagent created `/sandbox/sub.txt` = `hello`; 3 turns, $0.0542, `permission_denials []`, zero supervisor denials |
| Real task | seeded repo with `add` returning `a - b`; Claude fixed it, created `/tmp/venv` for pytest (system `pip install` is `externally-managed-environment`, Debian PEP 668, not a policy denial; one hit each to `pypi.org` and `files.pythonhosted.org`), committed `8c20a2c fix`; 8 turns, $0.0483, 20.4 s |
| Independent acceptance | fresh `sandbox exec`: `python3 -m pytest` → no module (image lacks pytest); `/tmp/venv/bin/python -m pytest -q` → 1 passed. The acceptance command must name the interpreter the task used, or the image must ship the test runner |
| Pi | `ANTHROPIC_OAUTH_TOKEN` alias: Pi sends it as `x-api-key`, which the proxy does not rewrite → 401 at Anthropic (placeholder reached the API verbatim, so nothing leaked). `ANTHROPIC_AUTH_TOKEN` alias (bearer): the substitution worked and the request authenticated, then Anthropic answered `400 invalid_request_error: "You're out of extra usage…"` while Claude Code calls with the same token succeeded seconds earlier. Whether Anthropic restricts subscription OAuth tokens to its own client or this is a real cap is **undetermined**; it is an account-policy question, not a sandbox one |
| Plain `--env` placement | aos-a2 with `--env CLAUDE_CODE_OAUTH_TOKEN=<real>`: the real value is in the workload process environment, but absent from `sandbox get -o json`, both containers' `docker inspect`, `openshell.db` and the journal. It travels over OpenShell's channel, not Docker's `Config.Env` |
| Credential at rest | the gateway's SQLite WAL (`~/.local/state/openshell/gateway/openshell.db-wal`, mode 600) contains the provider credential, as the credential store must |
| Cleanup | aos-a1, aos-a2 deleted; provider and profile kept; `/root/openshell-probe/creds/` holds the one 108-byte file |

## Mac with Colima 0.10.3 / Docker 29.9 (lane M2, live-proven)

After the operator upgraded Colima (`brew upgrade colima`) and ran `colima update`
(VM Docker 27.4 → 29.9.0, containerd image store), the Docker driver works:

| Step | Result |
|---|---|
| Stale image id | first create failed `ControlSupervisorStartFailed … No such image: sha256:95bc…`: the gateway had cached the supervisor image id resolved under the old image store. A gateway restart cleared it. Rule: restart the gateway after any Docker runtime change under it |
| Networking | the gateway binds `127.0.0.1:17670` only, but Colima forwards the VM's `host.docker.internal` (192.168.5.2) to the Mac loopback, and that name is in the certificate SANs. Config: `[openshell.drivers.docker] socket_path = "/Users/operator/.colima/default/docker.sock"`, `grpc_endpoint = "https://host.docker.internal:17670"`, `image_pull_policy = "if_not_present"` (`always` breaks local-only images). Sandboxes Ready in 0.2–1.2 s |
| Boundary | identical to the server: CapEff 0, Landlock denies `/etc` `/usr` `/root` `/`, every unlisted TCP/UDP/raw-IP destination and the Mac gateway itself denied, sandbox loopback reachable; workload `net=none capdrop ALL no-new-privileges uid 1000 pids 2048`, supervisor host-net read-only uid 65534; guest LSM list includes `landlock`; idle workload 5.5 MiB, supervisor 15 MiB, gateway RSS 54–58 MB |
| Harness image | server Dockerfile built on arm64 in 100 s (1.77 GB); Claude 2.1.296, Pi 1.1.0, Codex 0.162.1; venv + pip 4 s; npm registry 1 s; example.com denied |
| Claude auth | same bearer profile and provider as the server, created from the Mac's worker token file; same L4-rule refusal and fix; `claude -p 'Reply with exactly OK'` → `is_error false`, `result OK`, 3 s, 0 denials, 6 allowed connections to `api.anthropic.com:443`, no other host |
| Bus alias | with a hostname `grpc_endpoint` the supervisor gets no `host.openshell.internal` alias (only IPv4 literals do), so on this Mac an AOS bus rule must name `host.docker.internal:<port>` |
| Cleanup | sandboxes and volumes removed; gateway left running with the config above; provider `aos-claude-oauth` holds the worker token in the Mac gateway's store |

OpenShell's support matrix lists macOS via Docker Desktop only; Colima works here
with the two configuration lines above and is **not vendor-supported**.

## Pi with an Anthropic API key (lane S5, Haiku only)

Operator decision: Pi runs on a separately billed API key, restricted to Haiku. Profile
`aos-anthropic-apikey` (credential `env_vars: [ANTHROPIC_API_KEY]`, `auth_style: header`,
`header_name: x-api-key`, endpoint `api.anthropic.com:443` rest/enforce, binaries node and
bash); provider created from `/root/openshell-probe/creds/claude-api-token` (106 bytes, 0600);
sandbox `aos-pi1` from the harness image with `policy-a1.yaml`.

| Step | Result |
|---|---|
| Placeholder | `ANTHROPIC_API_KEY` inside is `openshell:resolve:en…`; `pi --list-models` shows `claude-haiku-4-5`, `claude-haiku-4-5-20251001`, `claude-haiku-5-5` |
| Call | `pi -p 'Reply with exactly OK' --model anthropic/claude-haiku-4-5 --no-session --no-extensions --no-skills` → exit 1, `400 invalid_request_error: "This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id header…"`. Supervisor: 0 denials, `NET:OPEN ALLOWED node -> api.anthropic.com:443 [policy:_provider_aos_anthropic_apikey]`, `HTTP:POST ALLOWED …/v1/messages [engine:l7]`: the key was substituted and accepted; the account requires a workspace scope that Pi's built-in Anthropic provider does not send |
| Not run | JSON-mode call, tiny task, acceptance (blocked by the same error); no completion, so no tokens should have billed (unverified against billing) |
| Leak audit | 0 occurrences of the key prefix in outputs, logs, history and provider JSON |
| Remedies | a workspace-scoped key (inferred from the error text), or a Pi `models.json` provider entry carrying the `anthropic-workspace-id` header (Pi documents header interpolation for custom providers; field name for the built-in provider unverified) |

## Codex with a worker ChatGPT login (lane S7, live-proven)

The worker login was created **inside** a sandbox: `codex -c cli_auth_credentials_store=file
login --device-auth` in `aos-cx1` (image `aos-harness:probe`, `CODEX_HOME=/sandbox/codex-home`,
policy `policy-codex.yaml` = `policy-a1` plus L4 rules for `auth.openai.com`, `chatgpt.com`,
`api.openai.com`, `ab.chatgpt.com` at 443). The operator entered the device code in a
browser; Codex wrote `auth.json` (`auth_mode: chatgpt`, access/id/refresh tokens, account id)
into the worker home. On the Mac, two `codex login` attempts with `CODEX_HOME` pointing at a
worker directory went to the shared macOS keychain instead of a file, which is why the Linux
sandbox is the right place to mint a worker login. A master copy lives at
`/root/openshell-probe/creds/codex-home/auth.json` (mode 600); token values were never printed.

| Step | Result |
|---|---|
| Reply | `codex exec --json --skip-git-repo-check --sandbox danger-full-access -c approval_policy="never" 'Reply with exactly OK'` → rc 0, events `thread.started, turn.started, item.completed(agent_message), turn.completed`, text `OK`, usage in 14161 (cached 12288) / out 5; default model `gpt-6.1-sol` accepted on this ChatGPT account |
| Real task | seeded bug fixed, venv pytest, commit `db3ccfa fix`; 33 s; usage in 105921 (cached 98304) / out 589 / reasoning 22. Fresh-exec acceptance: `1 passed`, clean status except `__pycache__/` |
| Hosts | `chatgpt.com:443` (dozens), `auth.openai.com:443` (login polling), `ab.chatgpt.com:443`; `api.openai.com` **not** contacted on a ChatGPT login; pip reached `pypi.org` and `files.pythonhosted.org`. Denied: `sdmntpr*.oaiusercontent.com:443` (OpenAI user-content CDN fetches, 12 attempts, harmless) |
| Token file | unchanged after each call (sha identical). Setting `last_refresh` 10 days back did **not** trigger a refresh: tokens unchanged, no `auth.openai.com` traffic. The access token carries a 10-day `exp`, the id token 1 hour; rotation is presumably tied to expiry (inferred). The lineage did not move |
| Persisting the lineage | after each run, `sandbox download` of `auth.json` back to the host master copy keeps the worker login current once a refresh does occur |
| Nested sandbox | `codex sandbox linux -- echo ok` → `bwrap: No permissions to create a new namespace…`; Codex's own sandbox cannot start under OpenShell on this host, so `--sandbox danger-full-access` with the OpenShell boundary is the required shape (documented by OpenAI for containers) |
| Cleanup | `aos-cx1` deleted; no token shapes (`eyJ`, `rt_`) in any captured output or probe file |

## Pi with a workspace-scoped API key, Haiku only (lanes S6 and follow-up, live-proven)

The operator replaced the key with a workspace-scoped one; `openshell provider update
aos-anthropic-apikey --from-existing` persisted it (0 occurrences of the key prefix anywhere).
Model fixed to `anthropic/claude-haiku-4-5`; four Pi invocations in lane S6 plus two follow-up
runs, about $0.06 billed in total.

| Step | Result |
|---|---|
| Reply | `pi -p 'Reply with exactly OK' --model anthropic/claude-haiku-4-5 --no-session --no-extensions --no-skills` → `OK`, exit 0; supervisor `HTTP:POST ALLOWED …/v1/messages [engine:l7]` under `_provider_aos_anthropic_apikey`; the workspace scope removed the earlier 400 |
| JSON mode | `--mode json` emits `session, agent_start, turn_start, message_start, message_update, message_end, turn_end, agent_end, agent_settled`; the assistant `message_end` carries `stopReason "stop"`, `rawStopReason "end_turn"`, `model`, `provider`, `api`, `usage` with `cost.total` (about $0.0024 for the reply), `responseId`, `durationMs`. Filter on `message.role == "assistant"`; there are several `message_end` events per run |
| Open-stdin hazard | `pi … --mode json` run inside `sandbox exec … bash -lc` with stdin left open produced **no output**, the exec relay closed (`SSH:FAIL` lines) and a later check found Pi's fix applied and tests passing but the claimed commit absent (staged only). Run as the sandbox main process with stdin open, Pi sat idle for minutes with zero network traffic. With `< /dev/null` the same main-process run finished in about 35 s |
| Real task, final shape | `openshell sandbox create … -- bash -lc 'seed repo && pi -p "<task>" --mode json --model anthropic/claude-haiku-4-5 … < /dev/null > /sandbox/pi-task.jsonl 2> …; echo rc=$? > /sandbox/pi-rc'`: 8 assistant turns, `stopReason stop`, final text explains the fix; fresh-exec acceptance `1 passed`, `git log` → `f22c1da fix`, `3a33d50 base`, status clean except `__pycache__/`. Hosts: `api.anthropic.com` ×2 (node), `pypi.org` ×1 and `files.pythonhosted.org` ×1 (python), 0 denials |
| Adapter rule | run the harness as the sandbox's main process (or a single exec) with stdin closed, write JSONL/exit code to files under the working directory, and read them in a separate exec; never chain post-processing after the harness in one exec |
| Cleanup | `aos-pi2`, `aos-pi3`, `aos-pi4` deleted; provider kept with the new key; no sandboxes remain |

## Mac: Codex and Pi real tasks, native-tool negatives, siblings (lane M3, live-proven)

Mac gateway (Colima 0.10.3, Docker 29.9, OpenShell 0.1.3), image `aos-harness:probe` (arm64).

| Step | Result |
|---|---|
| Codex login | worker `auth.json` uploaded to `/sandbox/codex-home/`; `codex login status` → Logged in using ChatGPT |
| Codex real task | `codex exec --json … --sandbox danger-full-access` fixed the seeded bug, venv pytest, commit `81fde92 fix`; usage in 89054 (cached 82304) / out 466; fresh-exec acceptance `1 passed`. Hosts `chatgpt.com` ×18, `ab.chatgpt.com` ×1, pypi ×2; `*.oaiusercontent.com` denied (6 attempts) |
| Codex negatives | file-edit tool: `Failed to write file /usr/local/bin/sentinel`; `curl https://example.com` exit 7; Codex 0.162 has no dedicated read tool (reads via shell). Shell sentinels: `/etc/shadow`, `/usr/local/bin/sentinel`, `/.openshell/channel` → Permission denied; `host.docker.internal:17670` → not resolvable from the workload (no alias injected on this Mac), denied; `1.1.1.1:443` denied |
| Pi real task | main process with stdin closed: rc 0, 9 assistant turns, `stopReason stop`, `claude-haiku-4-5`, about $0.005; commit `a5a8227 fix`; fresh-exec acceptance `1 passed`; hosts `api.anthropic.com` ×1, pypi ×2; 0 denials |
| Pi negatives | native read tool: `EACCES: permission denied, access '/etc/shadow'`; native write tool: `EACCES … open '/usr/local/bin/sentinel'`; bash curl exit 7. Landlock binds Pi's in-process Node `fs` calls the same as shell commands |
| Siblings and controller | both workloads `net=none`, no IP, only `lo`, zero routes; the policy DNS answers the sibling's hostname with a placeholder but TCP to it is denied; `/.openshell` denied; no docker socket; bridge gateway denied |
| Cleanup | sandboxes and volumes removed; providers `aos-claude-oauth` and `aos-anthropic-apikey` kept on the Mac gateway; gateway RSS 61 MB |

Gaps: the reverse sibling direction was not individually captured (same mechanism);
whether Codex rewrote the Mac-side `auth.json` was not re-checked.

## Negative-boundary sentinel suite with harness-native tools (lane S8, the server, live-proven)

Sandbox `aos-nb1` (harness image, `policy-codex.yaml`, providers `aos-claude-oauth` and
`aos-anthropic-apikey` both attached, Codex `auth.json` uploaded); sibling `aos-nb2` (no
providers). Claude ran with `Read(*)`, `Write(*)`, `WebFetch(*)`, `Agent(*)` allowed and its
inner sandbox off, so every denial is OpenShell's.

| Check | Observed |
|---|---|
| Shell and interpreters | `cat /etc/shadow`, `ls /.openshell/channel`, write `/usr/local/bin/sentinel`: Permission denied; Python `open('/etc/shadow')` EACCES; Python `socket.create_connection(('example.com',443))` EACCES; Node `fs.readFileSync('/etc/shadow')` EACCES; symlink and `cp` of `/etc/shadow` denied; docker sockets absent; `/proc/1/environ` denied |
| Controller | `host.openshell.internal:17670` denied (`transparent_tcp_mapping_denied`), `:22` denied, `:8010` allowed (declared bus port, control); `/.openshell`, `/.openshell/runtime` denied; `bootstrap.json` not visible; no `openshell` CLI inside; env holds only `OPENSHELL_SANDBOX=1`, the two placeholders and CA-bundle paths; `/sandbox/../.openshell/x` denied; `NoNewPrivs 1`, `Seccomp 2` with 6 filters |
| Claude own tools | Read `/root/.bashrc` → `EACCES`; Write `/usr/local/bin/sentinel` → `EACCES` on its atomic temp file; WebFetch `https://example.com` → `connect EACCES 198.18.0.2:443`; `permission_denials []` (the harness layer allowed everything; the boundary denied); 5 turns, $0.097 |
| Claude delegated child | Agent-tool subagent repeated the three plus Bash curl: all denied, report relayed; $0.108 |
| Claude classifier note | a prompt naming `/etc/shadow` was refused by the model's safety classifier (`[cyber]`, $0.078, no tool ran); boundary suites must use neutral protected paths such as `/root/.bashrc` |
| Codex own tools | no dedicated read tool in 0.162 (falls back to `cat` → denied); edit tool `Failed to write file /usr/local/bin/sentinel`; curl exit 7 |
| Codex delegated child | collab subagents are on by default; one was spawned and reported `Permission denied` for read and write and `curl: (7)` |
| Pi own tools | `read`, `write`, `edit` → `EACCES` (`/root/.bashrc`, `/usr/local/bin/sentinel`, `/etc/hostname` unchanged); bash curl exit 7; Haiku, about $0.0065 total |
| Siblings | nb1 served `http.server` on `0.0.0.0:8777` (200 locally); from nb2 every route to it (container ids via placeholder DNS, bridge addresses, loopback) denied; nb1's secret file absent in nb2; distinct channel volumes; both `net=none`, private IPC |
| Attribution | every supervisor denial names the real executable (`curl`, `claude.exe`, `python3.11`, `bash`, Codex's vendored binary) and destination, so AOS can attribute denials per harness and child |

## Verification bar through AOS-rendered policy (lanes E, F and the runner end-to-end, live-proven)

Tooling: `scripts/openshell_probe/render_policy.py` (v2 TOML → OpenShell YAML) and
`run_job.py` (provision, run, accept, extract, cancel, effective), demo repository
`examples/demo-app` (deliberate total defect, disposable Postgres, headless Chromium
`verify.sh`, acceptance `pytest tests` and `bash verify.sh`). The repo policy is
`exclude = [".env", "secrets/**"]`, `allow = ["pypi.org:443", "files.pythonhosted.org:443"]`;
the renderer adds the bus rule per platform and refuses a rule for a provider-bound host.

| Check | the server (Linux) | Mac (Colima) |
|---|---|---|
| Render refusals | no port → `policy: network.allow entry 'pypi.org' has no port; add :443`; `local_binding` → `policy: network.local_binding is retired…` (exit 2) | same renderer, same output |
| Provision | clone without hardlinks, `.git` kept, no `.env`/`secrets` entries in the tarball | same |
| Claude demo | fix committed (`c1ae910 fix total`), 7 turns, $0.05; `effective` match; excluded paths absent; acceptance `2 passed` + `VERIFY PASS`; bundle fetched on the host; hosts `api.anthropic.com` ×3, pypi ×2; cancel 0.7 s | fix `b2d5ceb`, 5 turns, $0.05; same checks pass; cancel 0.4 s |
| Codex demo (overlay policy adds `chatgpt.com`, `auth.openai.com`, `ab.chatgpt.com`; worker `auth.json` uploaded) | fix `5fe5150`, in 129 769 (cached 118 784) / out 673; acceptance pass; `auth.json` unchanged; hosts `chatgpt.com` ×21, `ab.chatgpt.com` ×1, pypi ×2; `*.oaiusercontent.com` denied | fix `d313745`; acceptance pass; hosts `chatgpt.com` ×21, pypi ×2 |
| Pi demo (Haiku) | fix `0fc9686`, 20 turns; first acceptance `verify.sh` failed on a port held by Postgres processes Pi's own interrupted `verify.sh` left alive; passed once they exited | fix `6730549`, 20 turns, acceptance pass; Pi's `git add -A` also committed a venv and the deletions of the excluded tracked files |
| Failure path (prompt: change nothing) | harness `DONE` in 5 s; acceptance: pytest rc 127 (no venv), `VERIFY FAIL: total was 500`; detected as failure | same |
| Retry | fresh sandbox with the real task: fix `e83a0cf`, acceptance pass after the leftover Postgres exited | not run (Linux covers it) |
| Timeout | runner `--timeout 25` on a `sleep 300` task: `harness.rc did not appear within 25 s`, rc 1; `cancel` gone in 2.1 s; no `sleep` on the host | not run |
| Negative sentinels under the rendered policy | `/etc/shadow`, `/root/.bashrc`, `/.openshell` denied; write denied; Python and Node file and socket calls EACCES; gateway port denied; bus port allowed (control); `pypi.org:80` and raw IP denied; no docker socket; `effective` match | same, plus `host.docker.internal:17670` unresolvable from the workload |
| Runner end to end as committed (after the fixes below) | not rerun | Pi Haiku: harness 86 s, `effective` match, 0 leftover processes, fix `1607edb`, acceptance `2 passed` + `VERIFY PASS`, bundle 403 B, hosts anthropic ×3 pypi ×3, cancel 0.3 s, 0 leaks |

Defects found and fixed in the tooling during the bar: `wait_for_file` used `test -f` on a
directory; a prompt file inside a git work tree uploaded as a filtered directory (uploads now
staged from scratch); `provision` now refuses `exclude` entries matching tracked paths (the
demo's synthetic secrets became untracked ignored files); the main script now sweeps every
leftover process of the workload uid before writing `harness.rc`; `verify.sh` stops its own
previous instances by pid file and no longer probes ports with `connect()`.

Runtime facts learned: a loopback listen entry can outlive its process in the sandbox
(`/proc/net/tcp` keeps `LISTEN` on 8088 and 5433 after the servers exit) while rebinding
the port still succeeds, so `connect()` probes report phantom servers; agents interrupt
verification scripts and leave daemons alive, so acceptance needs a swept process table or
a fresh sandbox; Chromium contacts Google update hosts on every run (about 48 denials per
`verify.sh`), all attributed to `/usr/lib/chromium/chromium`.

Gap for the production adapter: the repo policy cannot carry harness API hosts; an
operator-level harness-hosts setting is needed (Codex: `chatgpt.com`, `auth.openai.com`,
`ab.chatgpt.com`; Claude and Pi: supplied by their provider profiles).

## Runner end to end as committed, with harness hosts and fresh-sandbox acceptance (lanes G and H)

Tooling at commit 6b34487: `render_policy.py --harness-host` (operator-level rule, no overlay
file), `run_job.py run` (directory wait, uploads staged from scratch, post-harness process
sweep), `provision --ref`, `accept-fresh` (independent acceptance in a new credential-free
sandbox from a tarball of the fix branch).

| Step | Mac (lane G) | the server (lane H) |
|---|---|---|
| Render with `--harness-host chatgpt.com:443 auth.openai.com:443 ab.chatgpt.com:443` | `harness` rule emitted; gateway accepted it; `effective` match | same |
| Provision | 51 files, excluded `[]`, `.git/` present | same |
| Codex `run` | rc 0 in 46 s; fix `d7ea699 fix total`; 0 leftover processes after the sweep; `.env` absent; `accept` `2 passed` + `VERIFY PASS`; bundle 394 B fetched; hosts `chatgpt.com` ×18, `ab.chatgpt.com` ×1, pypi ×2; cancel 0.3 s | see lane H row below |
| Claude `run` | rc 0 in 30 s; `is_error false`, 5 turns, $0.045, 0 permission denials; fix `5dff5fe`; 0 leftovers; `accept` pass; bundle 453 B; hosts `api.anthropic.com` ×3, pypi ×2; cancel 0.3 s | see lane H row below |
| `accept-fresh` of a fix branch | `provision --ref codex-fix` (fix verified by content in the tarball) → new sandbox, no harness, no provider: venv+pip+pytest `2 passed` rc 0, `bash verify.sh` `VERIFY PASS` rc 0; cancel 0.3 s | `provision --ref pi-fix` → `accept-fresh`: `2 passed` rc 0, `VERIFY PASS` rc 0; cancel 2.0 s |
| Lane H, Claude `run` | | rc 0 in 57 s; 9 turns, $0.054; fix `30ce5f7`; 0 leftovers; `accept` pass; bundle 436 B; hosts `api.anthropic.com` ×3, pypi ×2; cancel 0.6 s |
| Lane H, Codex `run` | | rc 0 in 52 s; fix `3b8c8bc`; in 130 715 (cached 121 088) / out 781; `auth.json` unchanged; `accept` pass; bundle 403 B; hosts `chatgpt.com` ×13, `ab.chatgpt.com` ×1, pypi ×2 (`auth.openai.com` unused on a fresh token); denied `*.oaiusercontent.com` ×5 and Chromium's Google hosts; cancel 1.9 s |
| Lane H, Pi `run` (Haiku) | | rc 0 in 93 s; 18 turns, `stopReason stop`; fix `e7267a2`, bundle diff `app.py` only (the `.gitignore` kept the venv out, nothing tracked deleted); `accept` pass first time; hosts `api.anthropic.com` ×4, pypi ×2; cancel 0.8 s |
| Lane H, sentinels under the rendered policy | | all denied as before (`/etc/shadow`, `/root/.bashrc`, write, `/.openshell`, Python and Node file and socket calls, gateway port, `pypi.org:80`, raw IP, no docker socket); bus and `pypi.org:443` allowed; `effective` match |
| Operational note | the bundle path for `git -C <repo> fetch <bundle>` must be absolute | same |

## The AOS adapter on the bus lifecycle (lanes J and K, live-proven)

`agent_os/openshell_runner.py` (commit e483592; comparison fix 74fbab4) run as
`python -m agent_os.openshell_runner run <task>` against a local bus on each host
(`agent_os.api` on 127.0.0.1:8020 on the Mac, :8030 on the server), repo root with the demo
repository on `main` (12 tracked files, `aos-policy.toml` v2), tasks created with
`POST /tasks` and moved with `POST /tasks/<id>/ready`.

| Check | Mac (lane J) | the server (lane K) |
|---|---|---|
| Token handoff | the re-executed adapter process has 0 `AOS_TOKEN` entries in its environment | same (`/proc/<pid>/environ`) |
| First run | `blocked`: "effective policy differs from the rendering: missing: - \"/tmp\"…" because the gateway prints scalars bare while the renderer quotes them; sandbox deleted in 0.2 s, task moved to `blocked`, re-readied without force. Fixed in 74fbab4 with a real `policy get --full` fixture | same defect, same fail-closed behaviour |
| Claude demo | `review`, `verdict pass`, `result_commit 0e4e4a6` on `ao/AO-1`; report: policy rendered, effective match, harness exited 0 after 31 s, bundle 472 B, `network: api.anthropic.com x3, pypi x2`, sandbox deleted 0.4 s; acceptance `2 passed` + `VERIFY PASS` | `review`/`pass`, `cd99342` on `ao/AO-1`, 54 s, bundle 463 B, same hosts |
| Codex demo | `review`/`pass`, `271e78e`; `chatgpt.com x19`, `ab.chatgpt.com x1`, pypi x2, no `api.openai.com`; worker `auth.json` downloaded back, sha unchanged, mode 600 | `review`/`pass`, `ee9b890`; `chatgpt.com x23`; `auth.json` unchanged |
| Pi demo (Haiku) | `review`/`pass`, `bc625c9`, 66 s | `review`/`pass`, `56f788a`, 98 s |
| Sentinel task (Pi) | `review`/`pass`, `PROBE.md` committed; acceptance 8/8: `/etc/shadow`, `/root/.bashrc`, write to `/usr/local/bin`, Python connect to example.com, raw IP, gateway port, docker socket all denied; `pypi.org:443` control passed | 9/9 including the bus port positive control `host.openshell.internal:8030` |
| State and cleanup | per-run state dir keeps policy, prompt and settings only; no tarball, bundle or credential left; no sandboxes or containers after the lane | same; `free -m` 3.1 GiB available afterwards |

The adapter is invoked as a module; wiring it into the `aos run` argument parser touches
`agent_os/cli.py`, which another session is editing, and is left to the build.

## `aos run --backend openshell` with fresh-sandbox acceptance (live-proven, both platforms)

Commits e609d79 and e0993ab: `aos run <task> --backend openshell` (or `AOS_BACKEND=openshell`)
dispatches to the adapter; `AOS_OPENSHELL_FRESH_ACCEPTANCE=1` re-provisions the result tree
from the source repository at the result commit and runs the acceptance commands in a new
sandbox `aos-<run>-acc` with no harness, provider or credential. Sandbox names are
`aos-<run id>` because OpenShell refuses names longer than 19 characters (the first Linux
attempt with `-accept` was refused and correctly posted `blocked`).

| | Mac (Pi, Haiku) | the server (Pi, Haiku) |
|---|---|---|
| CLI | `.venv/bin/python -m agent_os.cli run AO-1 --backend openshell` → `claimed AO-1-r1`, `acceptance 2/2 pass`, `result review`, exit 0 | `.venv/bin/aos run AO-1 --backend openshell` on the re-readied task → `claimed AO-1-r2`, `acceptance 2/2 pass`, `result review`, exit 0 |
| Report | policy rendered for colima-mac, effective match, harness exited 0 after 79 s, result `e744226` on `ao/AO-1`, `acceptance ran in a fresh sandbox aos-ao-1-r1-acc`, network `api.anthropic.com x4`, pypi x2, sandbox deleted 0.4 s | task status `review`, branch `ao/AO-1` at `eb01e64 fix total` |
| Fresh acceptance | venv + pip + pytest `2 passed` and `bash verify.sh` `VERIFY PASS`, each tagged `(fresh sandbox aos-ao-1-r1-acc)` | same commands, `acceptance 2/2 pass` |
| Cleanup | no sandboxes; state dir keeps policy, prompt and settings only; 0 credential shapes in outputs | same |
| Codex through the CLI | `aos run AO-2 --backend openshell` with the worker `auth.json` and `AOS_OPENSHELL_HARNESS_HOSTS_CODEX`: `claimed AO-2-r1`, harness exited 0 after 49 s, result `0461a1e fix total` on `ao/AO-2`, fresh-sandbox acceptance `2 passed` + `VERIFY PASS` in `aos-ao-2-r1-acc`, `review`/`pass`; hosts `chatgpt.com x20`, `ab.chatgpt.com x1`, pypi x2; `auth.json` still 3835 B mode 600 | not rerun through the CLI (lane K ran Codex through the module entry on Linux) |
| Claude through the CLI | lane J ran Claude through the module entry on the Mac | `aos run AO-3 --backend openshell` with provider `aos-claude-oauth`: `claimed AO-3-r1`, harness exited 0 after 59 s, result `e303ca3 fix total` on `ao/AO-3`, fresh-sandbox acceptance `2 passed` + `VERIFY PASS` in `aos-ao-3-r1-acc`, `review`/`pass`; hosts `api.anthropic.com x3`, pypi x2 |
| Full test suite after the final adapter changes | `1258 passed in 242.87s` (whole repository) | |

## Open items

- Codex: the refresh trigger (expiry versus `last_refresh`) is undetermined; probe when a worker token nears its 10-day expiry, and keep the per-run `auth.json` download.
- Pi on the Claude subscription: the 400 "out of extra usage" after successful bearer authentication is an account/client-policy question; the operator chose a separately billed, workspace-scoped API key restricted to Haiku instead.
- Chromium: the zygote fork failure under OpenShell's seccomp filter is undiagnosed; `--no-sandbox --no-zygote` is the working shape.
- Gateway RSS growth (70 MB fresh to about 210 MB after a day of probes) is uncharacterised.
- The license decision (operator). The Chromium zygote failure under OpenShell's seccomp filter is undiagnosed;
`--no-zygote` is the working shape.
