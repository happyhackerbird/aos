# Agent communication probe P1c: a loopback-bound Mac bus reached from a sandbox at 192.168.5.2, 2026-10-14

[P1b](2026-10-14-agent-comms-p1b.md) · [Selected probes](public-index.md) ·
[OpenShell backend setup](../reference/openshell-backend-setup.md) · helper:
`scripts/openshell_probe/comms_p1b/driver.py sandbox-up` and `probe` (added here)

**Question.** The operator chose the IPv4 literal for the bus endpoint on `colima-mac` (P1b:
a sandbox reaches the bus at 192.168.5.2, the host as the Lima VM sees it). The Mac bus binds
`0.0.0.0` today (`agent_os/__main__.py` 22-23), so its port is reachable from the LAN. Can the
bus bind `127.0.0.1` only and still be reached from a sandbox through 192.168.5.2?

**Answer: yes.** Lima's user-mode network delivers a VM connection to `192.168.5.2:<port>` as
a connection from `127.0.0.1` on the Mac: both buses below logged the sandbox's request with
client `127.0.0.1`, and the loopback-bound bus answered it. Binding the Mac bus to loopback
closes the LAN and Tailscale exposure of its port without losing sandbox reach. No Claude run.

## Setup

- Worktree `agent-comms-p1c` at `92d3d05`; image `aos-harness:comms` (`34ad426d78a4`, P1b's);
  OpenShell 0.1.3 Connected; Colima `default` (aarch64, macOS Virtualization.Framework).
- One sandbox `p1c-reach`, no provider, main process `sleep 3600`, from a minimal policy
  (`[network] allow = []`) rendered for `colima-mac` with `services=(("192.168.5.2", 8012),)`:
  rules `aos-bus` (`host.docker.internal:8012`, dead on this Mac per P1b) and `aos-services`
  (`192.168.5.2:8012`). Ready for an exec 0.2 s after `create`. Deleted at the end.
- Two disposable buses on port 8012 from the worktree code, one after the other, data under
  the session scratchpad (deleted): A `uvicorn.run(create_app(Settings()), host="127.0.0.1",
  port=8012)`, then B the same with `host="0.0.0.0"`. `/health` needs no bearer.
- The Mac's addresses (`ifconfig`): `127.0.0.1`; `10.22.247.163` on `en0`, the default-route
  interface (`route -n get default` → `interface: en0`; `ipconfig getifaddr en0`); and
  `100.105.59.94` on `utun4` (Tailscale), measured as an extra.
- From inside the sandbox: `driver.py probe --url http://192.168.5.2:8012/health --url
  http://192.168.5.2:8010/health` (the second as the denied control), which also keeps the
  gateway's lines naming those ports (`openshell logs`).

## Q1. Bus on 127.0.0.1:8012, sandbox rule 192.168.5.2:8012, `/health` 200: yes

Bus A listening (`lsof -nP -iTCP:8012 -sTCP:LISTEN`): `Python 5289 ... TCP 127.0.0.1:8012
(LISTEN)`. From the sandbox (20:42:10 CEST):

```
getent host.docker.internal: rc=2
http://192.168.5.2:8012/health: rc=0 http=200
http://192.168.5.2:8010/health: rc=7 http=000
```

Bus A's log for that request: `INFO: 127.0.0.1:61592 - "GET /health HTTP/1.1" 200 OK` (the
earlier `127.0.0.1:61516` line is the Mac-side loopback curl of Q3). Gateway, fetched after
Q2 (the fetch right after the exec showed nothing yet): `NET:OPEN [INFO] ALLOWED
/usr/bin/curl(0) -> 192.168.5.2:8012 [policy:aos-services engine:opa]`, `HTTP:GET [INFO]
ALLOWED GET http://192.168.5.2:8012/health`, `NET:OPEN [MED] DENIED /usr/bin/curl(0) ->
192.168.5.2:8010 [reason:transparent_tcp_policy_denied]`.

## Q2. Control: the same sandbox and rule against a bus on 0.0.0.0:8012: yes

Bus B listening: `Python 10351 ... TCP *:8012 (LISTEN)`. From the sandbox (20:42:41 CEST):

```
http://192.168.5.2:8012/health: rc=0 http=200
http://192.168.5.2:8010/health: rc=7 http=000
```

Bus B's log: `INFO: 127.0.0.1:61648 - "GET /health HTTP/1.1" 200 OK` for the sandbox's
request (client `127.0.0.1` here too). Gateway: the same three lines as Q1, a second time.

## Q3. Exposure from the Mac's own side: yes, loopback closes it

With bus A (`127.0.0.1`):

```
http://127.0.0.1:8012/health -> rc=0 http=200
http://10.22.247.163:8012/health -> rc=7 http=000
http://100.105.59.94:8012/health -> rc=7 http=000
```

With bus B (`0.0.0.0`):

```
http://127.0.0.1:8012/health -> rc=0 http=200
http://10.22.247.163:8012/health -> rc=0 http=200
http://100.105.59.94:8012/health -> rc=0 http=200
```

Bus B's log shows the three clients as `127.0.0.1:61640`, `10.22.247.163:61641`,
`100.105.59.94:61642`. curl rc 7 is "failed to connect" (refused), not a timeout.

## Q4. Where the shared local bus on 8010 listens now (read-only)

`lsof -nP -iTCP:8010 -sTCP:LISTEN`: `Python 54301 operator 14u IPv4 ... TCP *:8010 (LISTEN)`,
that is `0.0.0.0`. Not touched. (8011, another session's test bus, is `127.0.0.1:8011`.)

## Limits

- One Mac, one Colima VM and gateway version; the forwarding behaviour is Lima's user-mode
  network as configured by Colima here, not a general property measured elsewhere.
- Linux not measured; the 2026-10-11 record says `host.openshell.internal` is the gateway
  host's loopback there and "the bus must also listen on 127.0.0.1 of the gateway host", so
  loopback is the measured path on Linux already.
- The gateway log fetched immediately after an exec can lag behind it (Q1's first fetch was
  empty; the later fetch had the lines): fetch again before concluding "no line".
- `/health` only; the token-bearing routes travel the same TCP path and were exercised by P1b.
