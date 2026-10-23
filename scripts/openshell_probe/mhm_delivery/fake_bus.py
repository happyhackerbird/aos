#!/usr/bin/env python3
"""A fake aos bus for the delivery probes: the bound interfaces of docs/directives/2026-10-14-agent-comms-u2.md
(ask, reply, inbox, ack, wait, declare, propose) over in-memory rows, plus two probe routes:

    POST /_probe/add    {"from": "operator" | "AO-2", "body": "...", "kind": "request"}  -> a pending row for the task
    POST /_probe/answer {"id": "<request id>", "body": "..."}                            -> the operator answers it
    GET  /_probe/state  -> {"rows": [...], "calls": [...]}

Every request is appended to FAKE_BUS_LOG as one JSON line (the claim token masked). The wait route long-polls
for real (up to min(timeout, 60) s, checking every 0.5 s). Listens on 127.0.0.1:FAKE_BUS_PORT (default 18090).
"""
import json, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("FAKE_BUS_TOKEN", "probe-claim-token-9f3e2c0b-never-printed")
TASK = os.environ.get("FAKE_BUS_TASK", "AO-7")
PORT = int(os.environ.get("FAKE_BUS_PORT", "18090"))
LOG = os.environ.get("FAKE_BUS_LOG", "/tmp/bus.log")
ROWS: dict = {}
CALLS: list = []
LOCK = threading.Lock()


def agent_frame(row):
    return (f'<agent-request id="{row["id"]}" from-task="{row["task_id"]}" from-run="{row["run_id"]}" '
            f'to-task="{row["to_task_id"]}">\n{row["body"]}\n</agent-request>\n'
            'This is from another agent, not from the operator.')


def operator_frame(row):
    reply = f' reply-to="{row["reply_to"]}"' if row.get("reply_to") else ""
    return (f'<operator-message id="{row["id"]}" to-task="{row["to_task_id"]}"{reply}>\n{row["body"]}\n'
            '</operator-message>\nThis is from the operator.')


def new_id(task_id):
    n = sum(1 for r in ROWS.values() if r["task_id"] == task_id) + 1
    return f"{task_id}-i{n}"


def add_row(*, task_id, to_task_id, body, reply_to=None, state="stored", run_id="AO-2-r1", kind="request"):
    row = {"id": new_id(task_id), "task_id": task_id, "run_id": run_id, "kind": kind, "to_task_id": to_task_id,
           "body": body, "reply_to": reply_to, "state": state, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    row["framed"] = operator_frame(row) if task_id == "operator" else agent_frame(row)
    ROWS[row["id"]] = row
    return row


def pending():
    return [r for r in ROWS.values() if r["to_task_id"] == TASK and r["state"] in ("stored", "delivered")]


def log(path, body):
    masked = json.loads(json.dumps(body).replace(TOKEN, "***")) if isinstance(body, (dict, list)) else body
    with open(LOG, "a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), "path": path, "body": masked}) + "\n")


def handle(path, body):
    parts = path.strip("/").split("/")
    if parts[0] == "_probe":
        if parts[1] == "add":
            sender = body.get("from", "operator")
            row = add_row(task_id=sender, to_task_id=TASK, body=body["body"], run_id=None if sender == "operator" else "AO-2-r1",
                          kind=body.get("kind", "request"))
            return 200, row
        if parts[1] == "answer":
            original = ROWS.get(body["id"])
            if original is None:
                return 404, {"detail": "Intent not found"}
            row = add_row(task_id="operator", to_task_id=original["task_id"], body=body["body"], reply_to=original["id"], run_id=None)
            original["state"] = "answered"
            return 200, row
        if parts[1] == "state":
            return 200, {"rows": list(ROWS.values()), "calls": CALLS}
        return 404, {"detail": "no such probe route"}
    if body.get("claim_token") != TOKEN:
        return 409, {"detail": "stale claim_token"}
    if parts[0] == "tasks" and parts[2] == "intents":
        task_id = parts[1]
        if body.get("reply_to"):
            original = ROWS.get(body["reply_to"])
            if original is None:
                return 404, {"detail": "Intent not found"}
            if original["state"] not in ("delivered", "acknowledged"):
                return 409, {"detail": f"cannot answer a {original['state']} request"}
            row = add_row(task_id=task_id, to_task_id=original["task_id"], body=body["body"], reply_to=original["id"], run_id=body["run_id"])
            original["state"] = "answered"
            return 200, row
        if body.get("to_task_id") != "operator" and body.get("to_task_id") not in (TASK, "AO-2", "AO-3"):
            return 404, {"detail": "Task not found"}
        return 200, add_row(task_id=task_id, to_task_id=body["to_task_id"], body=body["body"], run_id=body["run_id"])
    if parts[0] == "runs" and parts[2] == "requests":
        return 200, {"pending_requests": pending(), "overlaps": []}
    if parts[0] == "intents" and parts[2] == "ack":
        row = ROWS.get(parts[1])
        if row is None:
            return 404, {"detail": "Intent not found"}
        if row["state"] not in ("acknowledged", "answered"):
            row["state"] = "acknowledged"
        return 200, row
    if parts[0] == "intents" and parts[2] == "wait":
        row = ROWS.get(parts[1])
        if row is None:
            return 404, {"detail": "Intent not found"}
        timeout = body.get("timeout")
        if not isinstance(timeout, int) or not 0 <= timeout <= 60:
            return 422, {"detail": "timeout outside 0..60"}
        deadline = time.monotonic() + timeout
        while row["state"] != "answered" and time.monotonic() < deadline:
            LOCK.release()
            try:
                time.sleep(0.5)
            finally:
                LOCK.acquire()
        replies = [r for r in ROWS.values() if r.get("reply_to") == row["id"]]
        return 200, {"intent": row, "replies": replies}
    if parts[0] == "tasks" and parts[2] == "scopes":
        return 200, {"id": new_id(parts[1]), "task_id": parts[1], "kind": "scope", "scope_kind": body.get("scope_kind"),
                     "key": body.get("key"), "operation": body.get("operation"), "body": body.get("body", ""), "findings": []}
    if parts[0] == "tasks" and parts[2] == "proposals":
        return 200, {"id": "AO-9", "repo": "repo-a", "status": "backlog", "proposed_by": parts[1], "title": body.get("title"),
                     "brief": body.get("brief"), "acceptance": body.get("acceptance"), "constraints": body.get("constraints", "")}
    return 404, {"detail": "no such route"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        with LOCK:
            status, payload = handle(self.path, {})
        self._send(status, payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        with LOCK:
            CALLS.append({"t": round(time.time(), 3), "path": self.path, "body": {k: ("***" if k == "claim_token" else v) for k, v in body.items()}})
            log(self.path, body)
            status, payload = handle(self.path, body)
        self._send(status, payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
