#!/usr/bin/env python3
"""summarize.py <case dir> <chat|resp> [marker ...]: one compact evidence block per case.

Prints the exit code, the harness event stream's shape, every fake-model request with its tool names, the roles of
its messages and, per marker string, WHICH message (index, role, first 80 chars) carries it; the bus calls in order
with seconds since the first; the extension's fire log; aos-agent's ledger. Everything the leg needs to answer
"did the injected text reach the model, where, and when" without reading raw logs. Masks the claim token."""
import json, os, sys, time

out, wire = sys.argv[1], sys.argv[2]
markers = sys.argv[3:] or ["operator-message", "agent-request", "Requests you acknowledged", "INJECTED-MID-RUN", "SEEDED-AT-START"]
TOKEN = "probe-claim-token-9f3e2c0b-never-printed"


def read(name):
    try:
        return open(os.path.join(out, name), encoding="utf-8", errors="replace").read().replace(TOKEN, "***")
    except OSError:
        return ""


def jsonl(name):
    rows = []
    for line in read(name).splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


print(f"== {os.path.basename(out)} rc={read('rc').strip() or '?'} times={' '.join(read('times.txt').split())}")
stream = jsonl("harness.out")
if wire == "chat":
    kinds = [e.get("type") for e in stream]
    counts = {}
    for k in kinds:
        counts[k] = counts.get(k, 0) + 1
    print(f"stream: {len(stream)} events; counts={json.dumps(counts, sort_keys=True)}")
    print(f"stream: first={kinds[:3]} last={kinds[-4:]}")
    for e in stream:
        if e.get("type") == "queue_update":
            print(f"stream: queue_update steering={e.get('steering')} followUp={e.get('followUp')}")
    ends = [e for e in stream if e.get("type") == "agent_end"]
    for i, e in enumerate(ends):
        last = (e.get("messages") or [{}])[-1]
        text = "".join(b.get("text", "") for b in (last.get("content") or []) if isinstance(b, dict)) if isinstance(last.get("content"), list) else str(last.get("content"))
        print(f"stream: agent_end[{i}] last role={last.get('role')} stop={last.get('stopReason')} text={text[:100]!r}")
    for e in stream:
        if e.get("type") == "message_end" and e.get("message", {}).get("role") not in ("assistant", "toolResult"):
            m = e["message"]
            content = m.get("content")
            text = content if isinstance(content, str) else "".join(b.get("text", "") for b in content if isinstance(b, dict))
            print(f"stream: message_end role={m.get('role')} customType={m.get('customType')} text={text[:120]!r}")
else:
    kinds = []
    for e in stream:
        k = e.get("type")
        item = e.get("item") or {}
        kinds.append(f"{k}:{item.get('type')}" if item else k)
    print(f"stream: {len(stream)} events: {kinds}")
    for e in stream:
        item = e.get("item") or {}
        if e.get("type") == "item.completed" and item.get("type") == "agent_message":
            print(f"stream: agent_message text={str(item.get('text'))[:120]!r}")
        if e.get("type") in ("error", "turn.failed") or item.get("type") == "error":
            print(f"stream: {e.get('type')} {json.dumps(e)[:300]}")

err = read("harness.err").strip()
if err:
    print("stderr: " + " | ".join(l for l in err.splitlines() if l.strip())[:600])

print("-- fake model requests --")
for req in jsonl("fake.log"):
    if "schema" in req:
        print(f"request 0 schema {req['schema'].get('name')}: {json.dumps(req['schema'])[:700]}")
        continue
    n = req.get("n")
    if wire == "chat":
        msgs = req.get("messages") or []
        roles = [m.get("role") for m in msgs]
        texts = []
        for i, m in enumerate(msgs):
            c = m.get("content")
            texts.append((i, m.get("role"), c if isinstance(c, str) else "".join(b.get("text", "") for b in (c or []) if isinstance(b, dict))))
    else:
        msgs = req.get("input") or []
        roles = [f"{m.get('type')}/{m.get('role')}" if m.get("role") else m.get("type") for m in msgs]
        texts = []
        for i, m in enumerate(msgs):
            c = m.get("content")
            if isinstance(c, str):
                t = c
            elif isinstance(c, list):
                t = "".join(b.get("text", "") for b in c if isinstance(b, dict))
            else:
                t = str(m.get("output") or m.get("arguments") or "")
            texts.append((i, roles[i], t))
    print(f"request {n}: t={req.get('t')} tools={len(req.get('tools') or [])} roles={roles}")
    if n == 0:
        print(f"  request 0 tool names: {req.get('tools')}")
    if wire == "resp" and req.get("instructions"):
        ins = req["instructions"]
        for mk in markers:
            if mk in ins:
                print(f"  marker {mk!r} in INSTRUCTIONS")
    for mk in markers:
        hits = [(i, r, t[max(0, t.find(mk) - 40):t.find(mk) + 80].replace("\n", " ")) for i, r, t in texts if mk in t]
        for i, r, excerpt in hits:
            print(f"  marker {mk!r} in message {i} role={r}: {excerpt!r}")

print("-- bus calls --")
calls = jsonl("bus.log")
t0 = calls[0]["t"] if calls else 0
print(" ".join(f"{c['path']}@{c['t'] - t0:.1f}" for c in calls))
try:
    state = json.loads(read("state.json") or "{}")
    for row in state.get("rows", []):
        print(f"row {row['id']} from={row['task_id']} to={row['to_task_id']} state={row['state']} reply_to={row.get('reply_to')} body={row['body'][:60]!r}")
except ValueError:
    print("state.json unreadable")
print("-- fires --")
for f in jsonl("fires.jsonl"):
    f = {k: v for k, v in f.items() if k != "t"}
    print(json.dumps(f)[:300])
print("-- ledger --")
for l in jsonl("ledger.jsonl"):
    print(json.dumps(l)[:200])
print("-- inject --")
print(read("inject.log").strip() or "(none)")
