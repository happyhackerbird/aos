# Runner-provisioned PostgreSQL: live run against aos-example, 2026-10-11

One real `aos run` of the runner built on `ao/AO-1` (merged with main at b2b87a8)
against [aos-example](https://github.com/happyhackerbird/aos-example) at 6eaf1f1,
on this Mac, Claude Opus under the repository's `aos-policy.toml`, a plain
(non-statiker) host prompt, a host-local `.aos/services.toml` with `[postgres]`.
Task AO-3 on the local bus (`~/.aos/local-bus`), run AO-3-r1; log in
`~/Library/Logs/aos/AO-3-r1-*.log`; the agent's evidence in
`~/.aos/worktrees/aos-example/AO-3/evidence/`.

## What the run proved

| Step | Observed |
|---|---|
| Dry run | prints `services: postgres postgres:15-alpine@sha256:f7d2…` and `DATABASE_URL: provisioned at run time`, after the policy lines and before the prompt |
| Provisioning | `postgres container 22247f5376c7 ready` before `harness pid`; `docker ps` showed `aos-AO-3-r1-postgres`, `127.0.0.1:32774->5432/tcp`, labels `aos.run=AO-3-r1`, `aos.service=postgres` |
| Harness reach | the agent, inside the Claude policy sandbox, migrated and seeded the database through `DATABASE_URL`, ran the unit tests against it (12 passed), and read rows back (`db-rows.txt`); it did not start Docker itself |
| Acceptance reach | `uv sync --frozen` passed and the unit tests inside `scripts/check.sh` passed under `codex sandbox` with the same `DATABASE_URL`; `check.sh` exited 2 on the browser step (below), so acceptance was 1 of 2 |
| Teardown | cleanup reported `removed owned Docker container 22247f5376c7`; `docker ps -a --filter label=aos.service=postgres` was empty after the run |
| The task | the agent committed a fix with four order tests on `ao/AO-3` (15c082f) and left an evidence directory named in its report |

So the seam the directive specified works end to end on the success and
acceptance-failure paths. Timeout, cancellation and lease-loss teardown remain
proven only by the unit suite.

## Findings

1. **Headless Chromium cannot launch inside a macOS seatbelt sandbox.** In the Claude
   policy sandbox the agent saw
   `FATAL:base/apple/mach_port_rendezvous_mac.cc:159 bootstrap_check_in org.chromium.Chromium.MachPortRendezvousServer: Permission denied (1100)`.
   Reproduced outside a run under `codex sandbox -- python -c "playwright … chromium.launch()"`:
   the browser process dies with SIGTRAP at the same point. Chromium registers a Mach
   service at startup and both sandboxes deny it. Consequence for the MVP: real browser
   verification cannot run inside either native macOS sandbox; it needs the Linux
   workload environment the MVP recommends (OpenShell or a container), or a browser
   endpoint outside the sandbox. The agent drove the same form over HTTP instead and
   said so; `scripts/verify_browser.py` exits 2 (cannot run) rather than failing a check.
2. **A host variable leaked into the sandbox and produced a false denial line.** The
   operator shell exports `NODE_EXTRA_CA_CERTS=~/.claude/cache-fix-ca/ca.pem` (the
   `claude` wrapper's proxy certificate). The runner passed it to acceptance, Playwright's
   Node driver tried to read it, `~/.claude` is denied, and Node printed
   `Warning: Ignoring extra certs … Operation not permitted`. The denial scanner reported
   it as `acceptance denied:`. The run would have failed anyway on finding 1, but a
   runner that scrubs `NODE_EXTRA_CA_CERTS` and the proxy variables from the harness and
   acceptance environment, or an operator who launches with `env -u`, avoids a misleading
   verdict.
3. **The example's planted defect was mis-planted.** aos-example's first commit left the
   start page's GET path correct and the oldest-first slice only in the rejected-save
   re-render, which the browser check never reaches; the agent found and fixed that
   path. Main now carries the defect on the GET path (25ea7c3), where
   `scripts/verify_browser.py` fails on it and the unit tests pass.

## Not covered

Connecting to the database from a Pi run (no Pi adapter); a Codex harness run with
`[postgres]`; the OpenShell runner, which has no services handling; the masking
follow-up in `docs/directives/2026-10-11-runner-postgres-masking.md`.
