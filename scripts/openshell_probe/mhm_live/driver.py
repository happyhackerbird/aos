#!/usr/bin/env python3
"""Owned, network-none production-image OMP acceptance.

Run from the integrated checkout:
  uv run --extra dev python scripts/openshell_probe/mhm_live/driver.py \
      --image IMAGE --out NEW_DIRECTORY --case all

This command is evidence, not a deployed runner or a replacement bus. Scripted
cases establish placement/bounds; only behavior uses a real host-authenticated
model. No host credentials, Store database, or host listener enter the container.
Reports deliberately leave regression, deployment and idle wake-up unexercised.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import http.server
import json
import os
from pathlib import Path
import queue
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import unquote

CASES = ("arrival", "negative", "behavior", "subagent", "finalization", "stop-failure",
         "outage", "timeout", "malformed", "maximum", "lost-ack", "token", "composition", "off")
MODEL_PATH = "/v1/chat/completions"
MODELS = """providers:
  proof:
    baseUrl: http://127.0.0.1:18080/v1
    api: openai-completions
    apiKey: proof-no-secret
    models:
      - id: proof-model
        name: Proof
"""
AGENT = """---
name: proof-agent
description: Owned automatic-delivery acceptance child.
tools: bash, read, write
model: proof/proof-model
thinking-level: low
---
Run the supplied ordinary marked script once, then yield the successful report.
Never poll AOS, read a credential, send peer messages, or run git.
"""
REPLACER = """// Labelled operator acceptance fixture, not production logging.
export default function replace(pi) {
  pi.on('tool_result', event => {
    if (event.toolName === 'bash' && JSON.stringify(event.input).includes('EXT-REPLACE-ME'))
      return {content: [{type: 'text', text: '[proof-replace] ordinary content replaced'}]};
  });
}
"""
# Only the three labelled process-fault cases replace the installed helper. All
# other commands (including MCP/summary) exec the saved, image-owned executable.
FAULT = r'''#!/usr/bin/env python3
import json, os, sys, time
kind = sys.argv[1] if len(sys.argv) > 1 else ''
mode = open('/tmp/proof-fault-mode').read().strip()
if (mode == 'stop-failure' and kind == 'stop') or (mode in ('timeout','malformed') and kind == 'inbox'):
    payload = sys.stdin.read()
    with open('/tmp/proof-fault-events.jsonl','a') as f:
        f.write(json.dumps({'fixture':mode,'kind':kind,'at':time.time(),'input':json.loads(payload or '{}')})+'\n')
    if mode == 'timeout':
        time.sleep(18)
    if mode == 'malformed':
        print('PROOF-RAW-DIAGNOSTIC-{not-json', flush=True)
        print('PROOF-RAW-DIAGNOSTIC-stderr', file=sys.stderr, flush=True)
        sys.exit(0)
    if mode == 'stop-failure':
        print('PROOF-RAW-DIAGNOSTIC-stop', file=sys.stderr, flush=True)
        sys.exit(23)
os.execv('/usr/local/bin/aos-agent.proof-saved', ['/usr/local/bin/aos-agent.proof-saved', *sys.argv[1:]])
'''


def encoded(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def completion(message, finish):
    return {"id": "proof-" + uuid.uuid4().hex, "object": "chat.completion",
            "created": int(time.time()), "model": "proof-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def container_relay():
    """HTTP is private to this container; stdout contains envelopes only."""
    pending = {}
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path != MODEL_PATH and not re.fullmatch(
                    r"/(runs/[^/]+/requests|intents/[^/]+/ack|tasks/[^/]+/intents)", self.path):
                self.send_error(404)
                return
            ident = uuid.uuid4().hex
            answer = queue.Queue(maxsize=1)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                with lock:
                    pending[ident] = answer
                    print(encoded({"id": ident, "method": "POST", "path": self.path, "body": body}), flush=True)
                response = answer.get(timeout=115)
                if response.get("drop"):
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                status = response["status"]
                result = response["body"]
                stream = self.path == MODEL_PATH and body.get("stream") and status == 200
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
                self.end_headers()
                if stream:
                    choice = result["choices"][0]
                    delta = dict(choice["message"])
                    if "tool_calls" in delta:
                        delta["tool_calls"] = [{**call, "index": i} for i, call in enumerate(delta["tool_calls"])]
                    chunk = {"id": result["id"], "object": "chat.completion.chunk",
                             "model": result["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                    self.wfile.write(("data: " + encoded(chunk) + "\n\n").encode())
                    chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]
                    chunk["usage"] = result.get("usage", {})
                    self.wfile.write(("data: " + encoded(chunk) + "\n\ndata: [DONE]\n\n").encode())
                else:
                    self.wfile.write(encoded(result).encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                print("relay: client closed request", file=sys.stderr, flush=True)
            except Exception as exc:
                print("relay: " + type(exc).__name__, file=sys.stderr, flush=True)
                self.close_connection = True
            finally:
                with lock:
                    pending.pop(ident, None)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 18080), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(encoded({"ready": True}), flush=True)
    try:
        for line in sys.stdin:
            response = json.loads(line)
            with lock:
                target = pending.get(response["id"])
            if target is not None:
                target.put(response)
    finally:
        server.shutdown()
        server.server_close()
    return 0


def run_command(argv, *, input=None, timeout=30, check=True):
    result = subprocess.run(argv, input=input, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        # Do not echo arbitrary subprocess diagnostics or credential-bearing input.
        raise RuntimeError(f"{argv[0]} exited {result.returncode}")
    return result


def tool_text(body):
    return "\n".join(content_text(message.get("content")) for message in body.get("messages", [])
                     if message.get("role") == "tool")


def content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


def assistant_calls(value):
    """Walk full provider and native event shapes, including terminal calls."""
    if isinstance(value, dict):
        if value.get("role") == "assistant":
            for call in value.get("tool_calls", []):
                function = call.get("function", {})
                arguments = function.get("arguments", "{}")
                try:
                    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                except ValueError:
                    arguments = {"unparsed": arguments}
                yield {"name": function.get("name"), "arguments": arguments, "id": call.get("id")}
            for block in value.get("content", []) if isinstance(value.get("content"), list) else []:
                if block.get("type") in ("toolCall", "tool_use"):
                    yield {"name": block.get("name"), "arguments": block.get("arguments", block.get("input", {})),
                           "id": block.get("id")}
        for child in value.values():
            yield from assistant_calls(child)
    elif isinstance(value, list):
        for child in value:
            yield from assistant_calls(child)


def forbidden(call):
    name = call.get("name") or ""
    args = encoded(call.get("arguments", {}))
    if name in ("mcp__aos_inbox", "mcp__aos_wait", "aos_inbox", "aos_wait"):
        return True
    # Model-authored shell/eval helpers, including exec or quoted command forms.
    if name in ("bash", "eval", "write"):
        return bool(re.search(r"aos[-_]agent|aos[_-](?:inbox|wait)|mcp__aos_(?:inbox|wait)", args, re.I))
    return False


class Case:
    def __init__(self, name, image, out, metadata):
        self.name, self.image, self.out = name, image, out
        self.report = {"case": name, **metadata, "assertions": {}, "unexercised": [
            "affected and integrated regression suites (desk-owned)", "deployed runner lifecycle",
            "server deployment", "idle wake-up", "native Pi and Codex", "child credential exclusion"]}
        self.container = None
        self.relay = None
        self.harness = None
        self.threads = []
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        self.ready = threading.Event()
        self.write_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.history, self.bus_events, self.errors = [], [], []
        self.counters = {"parent": 0, "child": 0}
        self.injected = []
        self.marker = "FRESH-" + uuid.uuid4().hex
        self.answer = "ANSWER-" + uuid.uuid4().hex
        self.child_boundary = None
        self.dropped_ack = False
        self.secrets = []
        self.stdout = self.stderr = ""
        self.native = {}
        self.ledger = []
        self.stopping = threading.Event()
        self.collect_errors = []

    def scrub(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "***")
            # JSON may encode otherwise non-ASCII secret text; claims are ASCII.
        return text

    def retain(self, name, value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True, indent=2)
        target = self.out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.scrub(text))
        target.chmod(0o600)

    def assert_that(self, name, condition, evidence):
        self.report["assertions"][name] = {"passed": bool(condition), "evidence": evidence}

    def docker(self, *args, **kwargs):
        return run_command(["docker", *args], **kwargs)

    def execute(self, *args, **kwargs):
        return self.docker("exec", self.container, *args, **kwargs)

    def provision_file(self, scratch, name, destination, text, mode=0o644):
        source = scratch / name
        source.write_text(text)
        source.chmod(mode)
        self.docker("cp", str(source), self.container + ":" + destination)

    def start_store(self, scratch, sha):
        from agent_os.store import Store
        self.store = Store(scratch / "store")
        runs = []
        for role in ("receiver", "peer"):
            task = self.store.create_task(repo="omp-delivery-proof", title=role,
                                          brief="Owned disposable OMP automatic-delivery acceptance " + role,
                                          acceptance=["test -f /sandbox/proof-complete"], harness="pi")
            self.store.ready_task(task["id"])
            run = self.store.claim_task(task["id"], worker="omp-delivery-proof", worker_host="mac",
                                        expected_spec_hash=task["spec_hash"], effective_acceptance=task["acceptance"],
                                        acceptance_source="task", lease_seconds=1800)
            self.secrets.append(run["claim_token"])
            self.store.start_run(run["id"], run["claim_token"], base_commit=sha,
                                 branch="proof/" + role, worktree="owned-disposable-container")
            runs.append((task, run))
        (self.task, self.run), (self.peer_task, self.peer_run) = runs
        self.report["ids"] = {"receiver_task": self.task["id"], "receiver_run": self.run["id"],
                              "peer_task": self.peer_task["id"], "peer_run": self.peer_run["id"]}

    def inject(self, phase):
        with self.state_lock:
            bodies = [("operator", self.marker + "-" + phase)]
            if self.name == "arrival":
                bodies.append(("peer", self.marker + "-PEER"))
            if self.name == "maximum":
                bodies = [("peer", chr(0x10000 + int(self.marker[6:10], 16) + i) * 8000) for i in range(20)]
            if self.name == "behavior":
                bodies = [("operator", f"Reply to this request with exactly {self.answer}. Do not poll or commit. {self.marker}")]
            if self.name == "token":
                bodies = [("operator", self.marker + " TOKEN-FIXTURE " + self.run["claim_token"])]
            for sender, body in bodies:
                if sender == "operator":
                    row = self.store.create_operator_intent(to_task_id=self.task["id"], body=body)
                else:
                    row = self.store.create_intent(self.peer_task["id"], self.peer_run["id"],
                                                   self.peer_run["claim_token"], kind="request",
                                                   to_task_id=self.task["id"], body=body)
                self.injected.append({"phase": phase, "sender": sender, "row": row, "at": time.monotonic()})
            self.bus_events.append({"event": "injected", "phase": phase, "at": time.monotonic(),
                                    "ids": [i["row"]["id"] for i in self.injected if i["phase"] == phase]})

    def watch_markers(self):
        phases = [("child", "Q1")] if self.name == "subagent" else [("1", "Q1")]
        if self.name == "finalization":
            phases.append(("2", "Q2"))
        if self.name == "off":
            return
        try:
            for marker, phase in phases:
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    if self.stopping.is_set():
                        return
                    result = self.execute("test", "-f", "/sandbox/tool-start-" + marker, check=False, timeout=5)
                    if result.returncode == 0:
                        self.inject(phase)
                        break
                    if self.harness is not None and self.harness.poll() is not None:
                        raise RuntimeError("harness ended before injection marker")
                    time.sleep(0.15)
                else:
                    raise TimeoutError("ordinary tool marker not observed")
        except Exception as exc:
            self.errors.append("injection: " + type(exc).__name__ + ": " + str(exc))

    def scripted(self, body, actor, index):
        def call(name, arguments):
            offered = {t["function"]["name"] for t in body.get("tools", [])}
            if name not in offered:
                raise RuntimeError("required native/MCP tool not offered: " + name)
            return completion({"role": "assistant", "content": None, "tool_calls": [
                {"id": "proof-call-" + uuid.uuid4().hex, "type": "function",
                 "function": {"name": name, "arguments": encoded(arguments)}}]}, "tool_calls")

        def ordinary(marker="1"):
            command = "bash /tmp/proof-tool-" + marker + ".sh"
            if self.name == "composition":
                command += " EXT-REPLACE-ME"
            return call("bash", {"i": "Running ordinary acceptance tool", "command": command, "timeout": 40})

        def final(label):
            return completion({"role": "assistant", "content": label}, "stop")

        if self.name == "subagent":
            if actor == "child":
                if index == 0:
                    return ordinary("child")
                if index == 1:
                    with self.state_lock:
                        states = [self.store.get_intent(i["row"]["id"]) for i in self.injected]
                        self.child_boundary = {"at": time.monotonic(), "states": states,
                                               "pull_count": len([e for e in self.bus_events if e.get("event") == "pull"]),
                                               "request": body}
                    return call("yield", {"data": {"report": "PROOF-CHILD-SUCCESS", "success": True}})
                raise RuntimeError("unexpected child continuation")
            if index == 0:
                return call("task", {"context": "Owned child coexistence acceptance. Run the marked child script then yield.",
                                     "tasks": [{"name": "ProofChild", "agent": "proof-agent", "isolated": False,
                                                "task": "Run bash /tmp/proof-tool-child.sh once, then yield PROOF-CHILD-SUCCESS."}]})
            if index == 1:
                return call("wait", {"i": "Waiting for native child"})
            if index == 2:
                return ordinary("owner")
            return final("PROOF-PARENT-COMPLETE")
        if self.name == "finalization":
            if index == 0:
                return ordinary()
            if index == 1:
                return final("PROOF-FIRST-FINAL-Q1-UNANSWERED")
            if index == 2:
                return ordinary("2")
            if index == 3:
                q1 = next(i["row"]["id"] for i in self.injected if i["phase"] == "Q1")
                return call("mcp__aos_reply", {"id": q1, "text": "PROOF-Q1-ANSWER"})
            return final("PROOF-CONTINUATION-FINAL-Q2-UNANSWERED")
        if index == 0:
            return ordinary()
        # Reply through the real helper/Store, never through a synthetic ledger.
        if self.name in ("arrival", "composition", "token"):
            rows = [i["row"] for i in self.injected]
            if 1 <= index <= len(rows):
                return call("mcp__aos_reply", {"id": rows[index - 1]["id"], "text": "PROOF-SCRIPTED-ACKNOWLEDGEMENT"})
        return final("PROOF-FINAL-" + str(index))

    def model(self, envelope):
        body = envelope["body"]
        names = {t.get("function", {}).get("name") for t in body.get("tools", [])}
        actor = "child" if "yield" in names else "parent"
        with self.state_lock:
            index = self.counters[actor]
            self.counters[actor] += 1
            record = {"index": len(self.history), "actor": actor, "actor_index": index,
                      "at": time.monotonic(), "request": body}
            self.history.append(record)
        if self.name == "behavior":
            forwarded = {**body, "model": "openai-codex/gpt-6-astra", "stream": False}
            request = {"id": envelope["id"], "method": "POST", "path": MODEL_PATH, "body": forwarded}
            result = run_command(["omp", "auth-gateway", "stdio"], input=encoded(request) + "\n", timeout=100)
            envelopes = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            response = next(item for item in envelopes if item.get("id") == envelope["id"])
            record["gateway_status"] = response["status"]
            record["gateway_response"] = response["body"]
            if response["status"] != 200 or "choices" not in response["body"]:
                raise RuntimeError("real provider failed; no scripted fallback")
            answer = response["body"]
        else:
            answer = self.scripted(body, actor, index)
        record["response"] = answer
        record["completed_at"] = time.monotonic()
        return answer

    def dispatch(self, envelope):
        ident, path, body = envelope["id"], envelope["path"], envelope["body"]
        response = {"id": ident, "status": 200, "body": None, "drop": False}
        try:
            if envelope.get("method") != "POST":
                response.update(status=405, body={"detail": "POST only"})
            elif path == MODEL_PATH:
                response["body"] = self.model(envelope)
            elif self.name == "outage":
                self.bus_events.append({"event": "outage-drop", "path": path, "at": time.monotonic()})
                response["drop"] = True
            else:
                parts = [unquote(part) for part in path.strip("/").split("/")]
                with self.state_lock:
                    if len(parts) == 3 and parts[0] == "runs" and parts[2] == "requests" and parts[1] == self.run["id"]:
                        rows = self.store.requests_for_run(parts[1], body["claim_token"])
                        response["body"] = {"pending_requests": rows}
                        self.bus_events.append({"event": "pull", "at": time.monotonic(), "rows": rows})
                    elif len(parts) == 3 and parts[0] == "intents" and parts[2] == "ack":
                        row = self.store.ack_intent(parts[1], body["run_id"], body["claim_token"])
                        response["body"] = row
                        self.bus_events.append({"event": "ack", "at": time.monotonic(), "row": row})
                        if self.name == "lost-ack" and not self.dropped_ack:
                            self.dropped_ack = True
                            response["drop"] = True
                            self.bus_events.append({"event": "ack-response-dropped-after-commit", "row": row})
                    elif len(parts) == 3 and parts[0] == "tasks" and parts[2] == "intents" and parts[1] == self.task["id"]:
                        row = self.store.create_intent(parts[1], body["run_id"], body["claim_token"], kind="request",
                                                       body=body["body"], to_task_id=body.get("to_task_id"),
                                                       reply_to=body.get("reply_to"))
                        response["body"] = row
                        self.bus_events.append({"event": "reply" if body.get("reply_to") else "ask", "row": row,
                                                "at": time.monotonic()})
                    else:
                        response.update(status=404, body={"detail": "not a proof route"})
        except Exception as exc:
            self.errors.append("relay dispatch: " + type(exc).__name__ + ": " + str(exc))
            response.update(status=500, body={"detail": "owned acceptance dispatch failed"})
        try:
            with self.write_lock:
                self.relay.stdin.write(encoded(response) + "\n")
                self.relay.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            self.errors.append("relay response: " + type(exc).__name__)

    def read_relay(self):
        try:
            for line in self.relay.stdout:
                envelope = json.loads(line)
                if envelope.get("ready"):
                    self.ready.set()
                else:
                    self.pool.submit(self.dispatch, envelope)
        except Exception as exc:
            self.errors.append("relay reader: " + type(exc).__name__)

    def start_thread(self, function):
        thread = threading.Thread(target=function, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def collect(self):
        # Read bytes in memory and redact before the first retained byte. Never
        # docker-cp the session tree, Store, credential or raw relay envelopes.
        script = r'''import json
from pathlib import Path
roots = [Path('/sandbox/pi-sessions'), Path('/tmp/aos-agent')]
files = {}
for root in roots:
    if root.exists():
        for p in root.rglob('*'):
            if p.is_file(): files[str(p)] = p.read_text(errors='replace')
for name in ['/sandbox/work/received.txt','/tmp/proof-fault-events.jsonl']:
    p=Path(name)
    if p.is_file(): files[name]=p.read_text(errors='replace')
print(json.dumps(files))
'''
        try:
            files = json.loads(self.execute("python3", "-c", script, timeout=30).stdout)
            for name, text in files.items():
                if name.startswith("/sandbox/pi-sessions/"):
                    self.native[name] = text
                    self.retain("native-sessions/" + name.removeprefix("/sandbox/pi-sessions/"), text)
                elif name.startswith("/tmp/aos-agent/"):
                    self.retain("helper/" + name.removeprefix("/tmp/aos-agent/"), text)
                    if name.endswith(self.run["id"] + ".jsonl"):
                        self.ledger = [json.loads(line) for line in text.splitlines() if line.strip()]
                else:
                    self.retain(Path(name).name, text)
            self.collected = files
        except Exception as exc:
            self.collect_errors.append("collection: " + type(exc).__name__)
            self.collected = {}
        if self.name != "off":
            try:
                result = self.execute("/usr/local/bin/aos-agent", "summary", timeout=20, check=False)
                self.report["helper_summary_rc"] = result.returncode
                self.retain("helper-summary.txt", result.stdout + result.stderr)
                self.assert_that("helper summary observed", result.returncode == 0 and "messaging:" in result.stdout,
                                 "helper-summary.txt")
            except Exception as exc:
                self.collect_errors.append("summary: " + type(exc).__name__)
        self.retain("omp.stdout.jsonl", self.stdout)
        self.retain("omp.stderr.txt", self.stderr)

    def evaluate(self):
        all_native = []
        headers = []
        for name, text in self.native.items():
            if not name.endswith(".jsonl"):
                continue
            rows = []
            for line in text.splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    self.collect_errors.append("invalid native JSONL: " + name)
            all_native.extend(rows)
            headers.extend({"file": name, **row} for row in rows if row.get("type") == "session")
        events = []
        for line in self.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        calls = list(assistant_calls(self.history)) + list(assistant_calls(all_native)) + list(assistant_calls(events))
        positives = {name: [call for call in calls if call["name"] == name] for name in ("bash", "task", "wait", "yield", "mcp__aos_reply")}
        self.report["positive_calls"] = positives
        bad = [call for call in calls if forbidden(call)]
        self.assert_that("no model polling", not bad and bool(positives["bash"]), {"forbidden": bad, "positive_bash": len(positives["bash"])})
        self.assert_that("detector positive controls", forbidden({"name": "mcp__aos_inbox"}) and
                         forbidden({"name": "mcp__aos_wait"}) and
                         forbidden({"name": "bash", "arguments": {"command": "/usr/local/bin/aos-agent inbox --hook"}}) and
                         forbidden({"name": "eval", "arguments": {"code": "exec('aos-agent inbox')"}}) and
                         not forbidden({"name": "wait"}), "same detector with labelled synthetic controls; not runtime calls")
        self.assert_that("native sessions retained", bool(headers), {"headers": headers})
        self.report["sessions"] = headers
        self.assert_that("OMP completed", self.report.get("omp_rc") == 0 and any(
            e.get("type") == "turn_end" and e.get("message", {}).get("role") == "assistant" and
            e.get("message", {}).get("stopReason") == "stop" for e in events), "omp.stdout.jsonl; return code alone is insufficient")
        self.assert_that("relay and injection healthy", not self.errors, self.errors)
        self.assert_that("collection complete", not self.collect_errors, self.collect_errors)
        tool_surface = {t.get("function", {}).get("name") for h in self.history for t in h["request"].get("tools", []) if h["actor"] == "parent"}
        self.assert_that("native tools preserved", {"bash", "read", "write", "task", "wait"} <= tool_surface, sorted(tool_surface))
        if self.name != "off":
            self.assert_that("six MCP tools retained", {"mcp__aos_" + v for v in ("ask", "reply", "inbox", "wait", "declare", "propose")} <= tool_surface, sorted(tool_surface))
            self.assert_that("fresh injection observed after tool start", bool(self.injected), self.injected)
            absent_before_injection = all(
                item["row"]["body"] not in self.report["prompt"] and all(
                    item["row"]["body"] not in encoded(h.get("response", {}))
                    for h in self.history if h.get("completed_at", float("inf")) < item["at"])
                for item in self.injected)
            self.assert_that("fresh request not seeded in prompt or earlier provider fixture",
                             bool(self.injected) and absent_before_injection, "injection timestamps and complete model-history.json")
        placements = []
        for pull in (e for e in self.bus_events if e.get("event") == "pull" and e["rows"]):
            following = next((h for h in self.history if h["actor"] == "parent" and h["at"] > pull["at"]), None)
            exact = following is not None and all(self.scrub(row["framed"]) in tool_text(following["request"]) for row in pull["rows"])
            placements.append({"pull_at": pull["at"], "request_index": following["index"] if following else None,
                               "ids": [r["id"] for r in pull["rows"]], "exact_tool_role_frames": exact})
        self.report["placements"] = placements
        placement_cases = {"arrival", "behavior", "subagent", "finalization", "maximum", "lost-ack", "token", "composition"}
        if self.name in placement_cases:
            self.assert_that("exact frames in next owner model request after pull", bool(placements) and all(p["exact_tool_role_frames"] for p in placements), placements)
        rows = [self.store.get_intent(i["row"]["id"]) for i in self.injected]
        self.report["requests"] = rows
        self.report["all_intents"] = self.store.list_intents(self.task["id"])
        if self.name in placement_cases:
            self.assert_that("Store acknowledgement observed", bool(rows) and all(r["state"] in ("acknowledged", "answered") for r in rows), rows)
        if self.name == "arrival":
            pulled_rows = [r for p in self.bus_events if p.get("event") == "pull" for r in p["rows"]]
            self.assert_that("operator and peer framing and identity", len(rows) == 2 and {r["task_id"] for r in rows} == {"operator", self.peer_task["id"]} and
                             all(any(p["id"] == r["id"] and r["task_id"] in p["framed"] and r["id"] in p["framed"] for p in pulled_rows) for r in rows) and
                             all(r["state"] == "answered" for r in rows), pulled_rows)
            self.assert_that("reply detector live", bool(positives["mcp__aos_reply"]), "positive_calls")
        if self.name == "negative":
            observed = encoded(self.history) + self.stdout + self.stderr + encoded(self.native)
            self.assert_that("adapter-only negative control", "-e /usr/local/lib/aos-agent/aos_pi.ts" not in self.report["invocation"] and
                             self.report["comms"] and self.report["claim_mode"] == "600" and
                             not any(e.get("event") == "pull" for e in self.bus_events), "invocation, claim_mode, bus-events.json")
            self.assert_that("fresh marker unseen and pending", self.marker not in observed and bool(rows) and all(r["state"] == "stored" for r in rows), rows)
        if self.name == "behavior":
            replies = [r for r in self.report["all_intents"] if r.get("reply_to") in {q["id"] for q in rows}]
            artifact = self.collected.get("/sandbox/work/received.txt", "")
            self.assert_that("real model answered correlated Store request", bool(rows) and all(r["state"] == "answered" for r in rows) and
                             any(r["body"].strip() == self.answer for r in replies) and bool(positives["mcp__aos_reply"]), replies)
            self.report["behavior_artifact_exact"] = artifact.strip() == self.answer
            self.assert_that("real provider evidence", bool(self.history) and all(h.get("gateway_status") == 200 and "gateway_response" in h for h in self.history), "model-history.json")
        continuation_requests = []
        continuation_counts = []
        for h in self.history:
            if h["actor"] != "parent":
                continue
            users = [content_text(m.get("content")) for m in h["request"].get("messages", []) if m.get("role") == "user"]
            continuation_counts.append(sum(
                text.count("Requests you acknowledged and have not answered:") +
                text.count("[aos] Final message check failed.") for text in users))
            for text in users:
                if "Requests you acknowledged and have not answered:" in text or "[aos] Final message check failed." in text:
                    if text not in continuation_requests:
                        continuation_requests.append(text)
        finals = [h for h in self.history if h["actor"] == "parent" and h.get("response", {}).get("choices", [{}])[0].get("finish_reason") == "stop"]
        self.report["continuations"] = continuation_requests
        if self.name in ("finalization", "stop-failure", "lost-ack"):
            self.assert_that("one visible continuation and repeated final", len(continuation_requests) == 1 and
                             max(continuation_counts, default=0) == 1 and len(finals) == 2,
                             {"continuations": continuation_requests, "occurrences_per_request": continuation_counts,
                              "final_response_count": len(finals)})
        if self.name == "finalization":
            q1, q2 = rows
            replies = [r for r in self.report["all_intents"] if r.get("reply_to") == q1["id"]]
            self.assert_that("Q1 answered during continuation Q2 acknowledged without another", q1["state"] == "answered" and q2["state"] == "acknowledged" and
                             len(replies) == 1 and sum(e.get("event") == "stop_blocked" for e in self.ledger) == 1 and
                             any(q2["id"] in p["ids"] for p in placements), {"q1": q1, "q2": q2, "replies": replies})
        if self.name == "subagent":
            parents = [h for h in headers if not h.get("parentSession") and not h.get("parentSessionId")]
            children = [h for h in headers if h.get("parentSession") or h.get("parentSessionId")]
            parent_ids = {h.get("id") for h in parents}
            links = [h for h in children if h.get("parentSessionId") in parent_ids or any(str(p) in str(h.get("parentSession")) for p in parent_ids if p)]
            child_requests = [h for h in self.history if h["actor"] == "child"]
            boundary = self.child_boundary
            yield_results = [r.get("message", {}) for r in all_native
                             if r.get("message", {}).get("role") == "toolResult" and
                             r.get("message", {}).get("toolName") == "yield"]
            successful_yield = any(r.get("isError") is False and r.get("details", {}).get("status") == "success" and
                                   r.get("details", {}).get("data", {}).get("report") == "PROOF-CHILD-SUCCESS" and
                                   any(c["id"] == r.get("toolCallId") for c in positives["yield"])
                                   for r in yield_results)
            self.assert_that("native task wait yield successful", all(positives[n] for n in ("task", "wait", "yield")) and
                             successful_yield and bool(links) and all(h.get("id") not in parent_ids for h in children),
                             {"parents": parents, "children": children, "links": links, "yield_results": yield_results})
            self.assert_that("child-only boundary leaves inbox pending", boundary is not None and bool(boundary["states"]) and
                             all(r["state"] == "stored" for r in boundary["states"]) and
                             all(self.marker not in encoded(h["request"]) for h in child_requests) and self.counters["child"] == 2,
                             boundary)
            self.assert_that("owner boundary drains after child", bool(placements) and boundary is not None and all(p["pull_at"] > boundary["at"] for p in placements), placements)
        if self.name in ("outage", "timeout", "malformed"):
            model_tools = "\n".join(tool_text(h["request"]) for h in self.history)
            self.assert_that("ordinary tool intact with sanitized failure", "PROOF-ORDINARY-1" in model_tools and
                             "[aos] Inbound message delivery failed." in model_tools and
                             "PROOF-RAW-DIAGNOSTIC" not in model_tools + self.stdout + encoded(self.native), "model-history.json and native-sessions")
            if self.name == "outage":
                self.assert_that("real helper reached unavailable bus", any(e.get("event") == "outage-drop" for e in self.bus_events) and any(e.get("event") == "hook" for e in self.ledger), self.bus_events)
            else:
                faults = [json.loads(line) for line in self.collected.get("/tmp/proof-fault-events.jsonl", "").splitlines()]
                self.assert_that("labelled inbox fault exercised", any(e["kind"] == "inbox" and e["fixture"] == self.name for e in faults), faults)
                if self.name == "timeout":
                    self.assert_that("spawn failure bounded by production cap", 15 <= self.report["omp_elapsed_seconds"] < 65, self.report["omp_elapsed_seconds"])
        if self.name == "stop-failure":
            faults = [json.loads(line) for line in self.collected.get("/tmp/proof-fault-events.jsonl", "").splitlines()]
            self.assert_that("stop failure visible without later tool", bool(continuation_requests) and
                             "[aos] Final message check failed." in continuation_requests[0] and
                             len(positives["bash"]) > 0 and self.counters["parent"] == 3 and
                             sum(e["kind"] == "stop" for e in faults) >= 2 and
                             "PROOF-RAW-DIAGNOSTIC" not in self.stdout + encoded(self.native), faults)
        if self.name == "maximum":
            self.assert_that("maximum real Store batch intact", len(rows) == 20 and all(
                len(r["body"]) == 8000 and all(ord(c) >= 0x10000 for c in r["body"]) for r in rows) and
                             any(len(p["rows"]) == 20 for p in self.bus_events if p.get("event") == "pull") and
                             len({e.get("id") for e in self.ledger if e.get("event") == "acked"}) == 20, "placements, requests and helper ledger; full frames compared")
        if self.name == "lost-ack":
            self.assert_that("ack committed then response lost with retained ledger", self.dropped_ack and bool(rows) and rows[0]["state"] == "acknowledged" and
                             any(e.get("event") == "acked" and e.get("id") == rows[0]["id"] for e in self.ledger) and
                             any(e.get("event") == "stop_blocked" for e in self.ledger), "bus-events.json, helper ledger; no redelivery certainty asserted")
        if self.name == "token":
            raw_surfaces = {"stdout": self.stdout, "stderr": self.stderr, "sessions": encoded(self.native),
                            "ledger": encoded(self.ledger)}
            self.assert_that("claim scrubbed before evidence sanitization", all(self.run["claim_token"] not in text for text in raw_surfaces.values()) and
                             any("***" in tool_text(h["request"]) and "TOKEN-FIXTURE" in tool_text(h["request"]) for h in self.history),
                             {name: {"token_present": self.run["claim_token"] in text} for name, text in raw_surfaces.items()})
        if self.name == "composition":
            self.assert_that("operator replacement precedes intact AOS append", any("[proof-replace] ordinary content replaced" in tool_text(h["request"]) and
                             self.marker in tool_text(h["request"]) for h in self.history) and
                             self.report["invocation"].index("/tmp/proof-replace.ts") < self.report["invocation"].index("/usr/local/lib/aos-agent/aos_pi.ts"), "model-history.json and invocation")
        if self.name == "off":
            self.assert_that("comms off has no wiring", not any(str(n).startswith("mcp__aos_") for n in tool_surface) and
                             "aos_pi.ts" not in self.report["invocation"] and self.report["off_files_absent"] and
                             "mcp__aos_" not in self.report["prompt"], "tool surface, invocation, prompt and observed absence")

    def adapter_smoke(self):
        # Separate, explicitly synthetic subprocess-result smoke of the installed
        # adapter. It cannot stand in for the real-helper/provider cases above.
        script = r'''
import cp from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
const spawns=[], checks={};
cp.spawnSync=(path,args,options)=>{
  spawns.push({path,args,input:JSON.parse(options.input),timeout:options.timeout,maxBuffer:options.maxBuffer});
  return {status:0,signal:null,stderr:'',stdout:JSON.stringify(args[0]==='inbox'
    ? {hookSpecificOutput:{hookEventName:'PostToolUse',additionalContext:'PROOF-DIRECT-FRAME'}}
    : {decision:'block',reason:'PROOF-DIRECT-STOP'})};
};
syncBuiltinESMExports();
const {default:adapter}=await import('/usr/local/lib/aos-agent/aos_pi.ts');
const handlers=new Map(), followups=[];
adapter({on:(name,fn)=>handlers.set(name,fn),sendUserMessage:(text,options)=>followups.push({text,options})});
const ctx=(id,header={})=>({sessionManager:{getSessionId:()=>id,getHeader:()=>header}});
const owner=ctx('proof-owner'), rootSession='/sandbox/pi-sessions/proof-owner.jsonl';
const child=ctx('proof-child',{parentSession:rootSession});
handlers.get('session_start')({},owner);
handlers.get('session_start')({},child);
const original={toolName:'bash',content:[{type:'text',text:'ordinary'},
  {type:'image',data:'synthetic-image',mimeType:'image/png'}],isError:true,
  details:{exitCode:7},toolCallId:'proof-call',customMetadata:{keep:true}};
checks.childToolNoop=handlers.get('tool_result')(original,child)===undefined && spawns.length===0;
const result=handlers.get('tool_result')(original,owner);
checks.metadata=result.isError===true && result.details===original.details &&
  result.toolCallId===original.toolCallId && result.customMetadata===original.customMetadata;
checks.blocks=result.content.length===3 && result.content[0]===original.content[0] &&
  result.content[1]===original.content[1] && result.content[2].text==='PROOF-DIRECT-FRAME' &&
  original.content.length===2;
const stop=handlers.get('turn_end');
const before=spawns.length;
for(const stopReason of ['error','aborted','length','toolUse'])
  stop({message:{role:'assistant',stopReason},toolResults:[]},owner);
stop({message:{role:'user',stopReason:'stop'},toolResults:[]},owner);
stop({message:{role:'assistant',stopReason:'stop'},toolResults:[{}]},owner);
stop({message:{role:'assistant',stopReason:'stop'},toolResults:[]},child);
checks.invalidBoundariesNoop=spawns.length===before && followups.length===0;
const event={message:{role:'assistant',stopReason:'stop'},toolResults:[]};
const childHandlers=new Map(), childFollowups=[];
adapter({on:(name,fn)=>childHandlers.set(name,fn),
  sendUserMessage:(text,options)=>childFollowups.push({text,options})});
childHandlers.get('session_start')({},child);
const childBefore=spawns.length;
checks.separateChildToolNoop=childHandlers.get('tool_result')(original,child)===undefined &&
  spawns.length===childBefore;
childHandlers.get('turn_end')(event,child);
checks.separateChildFinalNoop=spawns.length===childBefore && childFollowups.length===0;
childHandlers.get('session_start')({},owner);
const childLaterResult=childHandlers.get('tool_result')(original,owner);
childHandlers.get('turn_end')(event,owner);
checks.childLaterStartClosed=childLaterResult===undefined &&
  spawns.length===childBefore && childFollowups.length===0;
stop(event,owner);
stop(event,owner);
checks.singleFollowup=followups.length===1 && followups[0].options.deliverAs==='followUp';
checks.secondFinalActive=spawns.at(-1).input.stop_hook_active===true;
const disabled=new Map();
adapter({on:(name,fn)=>disabled.set(name,fn),sendUserMessage:()=>{throw Error('unexpected');}});
disabled.get('session_start')({},ctx(null));
disabled.get('session_start')({},owner);
const n=spawns.length;
disabled.get('tool_result')(original,owner);
disabled.get('turn_end')(event,owner);
checks.invalidFirstIdentityClosed=spawns.length===n;
for(const [key,headers] of [
  ['unavailableHeaderClosed',[undefined,null]],
  ['nonobjectHeaderClosed',[false,7,'root',[]]],
  ['childIdHeaderClosed',[{parentSessionId:'proof-owner'}]],
]){
  checks[key]=true;
  for(const header of headers){
    const closed=new Map(), closedFollowups=[];
    adapter({on:(name,fn)=>closed.set(name,fn),
      sendUserMessage:(text,options)=>closedFollowups.push({text,options})});
    const first={sessionManager:{getSessionId:()=> 'proof-unqualified',getHeader:()=>header}};
    if(header===undefined) delete first.sessionManager.getHeader;
    closed.get('session_start')({},first);
    const closedBefore=spawns.length;
    const firstResult=closed.get('tool_result')(original,first);
    closed.get('turn_end')(event,first);
    closed.get('session_start')({},ctx('proof-later-root',{parentSession:null}));
    const later=ctx('proof-later-root',{parentSession:null});
    const laterResult=closed.get('tool_result')(original,later);
    closed.get('turn_end')(event,later);
    checks[key]=checks[key] && firstResult===undefined && laterResult===undefined &&
      spawns.length===closedBefore && closedFollowups.length===0;
  }
}
checks.spawnContract=spawns.every(s=>s.path==='/usr/local/bin/aos-agent' &&
  s.args[1]==='--hook' && s.timeout===15000 && s.maxBuffer===32*1024*1024);
console.log(JSON.stringify({reach:'installed adapter with labelled synthetic subprocess results, not live delivery',
  checks,spawns,followups,result}));
'''
        result = self.execute("node", "--input-type=module", "-e", script, timeout=30, check=False)
        self.retain("adapter-direct-smoke.stdout.json", result.stdout)
        self.retain("adapter-direct-smoke.stderr.txt", result.stderr)
        try:
            evidence = json.loads(result.stdout)
            checks = evidence["checks"]
            required_checks = {
                "childToolNoop", "metadata", "blocks", "invalidBoundariesNoop",
                "singleFollowup", "secondFinalActive", "invalidFirstIdentityClosed",
                "spawnContract", "separateChildToolNoop", "separateChildFinalNoop",
                "childLaterStartClosed", "unavailableHeaderClosed", "nonobjectHeaderClosed",
                "childIdHeaderClosed",
            }
            valid = (result.returncode == 0 and isinstance(checks, dict) and
                     required_checks <= checks.keys() and
                     all(checks[key] is True for key in required_checks))
        except (ValueError, KeyError, TypeError):
            evidence, valid = {"rc": result.returncode}, False
        self.assert_that("separate installed adapter metadata and boundary smoke", valid, evidence)

    def fresh_acceptance(self):
        fresh = None
        record = {}
        try:
            fresh = "aos-proof-fresh-" + uuid.uuid4().hex
            record["container_name"] = fresh
            fresh = self.docker("run", "-d", "--name", fresh, "--network", "none", "--user", "1000", self.image, "sleep", "infinity").stdout.strip()
            record["container"] = fresh
            script = """import json, os
from pathlib import Path
paths=['/sandbox/aos-run.json','/tmp/pi-agent/mcp.json','/sandbox/pi-sessions','/tmp/aos-agent']
print(json.dumps({'uid':os.getuid(),'absent':{p:not Path(p).exists() for p in paths}}))
"""
            result = self.docker("exec", fresh, "python3", "-c", script)
            record["observed"] = json.loads(result.stdout)
            # A fresh acceptance runs an ordinary command, not OMP or an adapter.
            command = self.docker("exec", fresh, "sh", "-c", "test ! -e /sandbox/aos-run.json && printf 'PROOF-FRESH-ACCEPTANCE\\n'")
            record["acceptance_stdout"] = command.stdout
            inspect = json.loads(self.docker("inspect", fresh).stdout)[0]
            record["network"] = inspect["HostConfig"]["NetworkMode"]
            record["mounts"] = inspect["Mounts"]
            self.assert_that("genuinely fresh credential-free acceptance", all(record["observed"]["absent"].values()) and
                             record["observed"]["uid"] == 1000 and command.stdout.strip() == "PROOF-FRESH-ACCEPTANCE" and
                             not record["mounts"] and record["network"] == "none", record)
        finally:
            if fresh:
                try:
                    removed = self.docker("rm", "-f", fresh, timeout=20, check=False)
                    record["cleanup_rc"] = removed.returncode
                    self.assert_that("fresh acceptance bounded cleanup", removed.returncode == 0, record)
                except Exception as exc:
                    record["cleanup_error"] = type(exc).__name__
                    self.assert_that("fresh acceptance bounded cleanup", False, record)
            self.report["fresh_acceptance"] = record

    def run_case(self, scratch):
        from agent_os import pi
        from agent_os.openshell_runner import COMMS_PROMPT_OMP, OMP_CONFIG, OMP_MCP_CONFIG
        self.start_store(scratch, self.report["source_sha"])
        comms = self.name != "off"
        extensions = ("/tmp/proof-replace.ts",) if self.name == "composition" else ()
        invocation = pi.invocation("omp", bin="omp", provider="proof", model="proof-model", session_id=None,
                                   harness_timeout=300, comms=comms, extensions=extensions)
        command = invocation.command
        if self.name == "negative":
            suffix = " -e /usr/local/lib/aos-agent/aos_pi.ts"
            if not command.endswith(suffix):
                raise RuntimeError("production invocation does not end with the declared AOS extension")
            command = command[:-len(suffix)]
        prompt = "Run the ordinary marked script /tmp/proof-tool-1.sh once with bash, then finish. Do not commit or poll AOS.\n"
        if self.name == "behavior":
            prompt += ("After the script, answer automatically received operator requests with mcp__aos_reply using the exact requested text. "
                       "Do not read run credentials, invoke helpers, use inbox/wait, or call git.\n")
        if comms:
            prompt += "\n" + COMMS_PROMPT_OMP + "\n"
        self.report.update(invocation=command, prompt=prompt, comms=comms)
        try:
            self.container = "aos-proof-" + uuid.uuid4().hex
            self.report["container_name"] = self.container
            self.container = self.docker("run", "-d", "--name", self.container, "--network", "none", "--user", "1000", self.image, "sleep", "infinity").stdout.strip()
            self.report["container"] = self.container
            inspected = json.loads(self.docker("inspect", self.container).stdout)[0]
            self.assert_that("owned private container", inspected["HostConfig"]["NetworkMode"] == "none" and
                             not inspected["Mounts"] and self.execute("id", "-u").stdout.strip() == "1000",
                             {"network": inspected["HostConfig"]["NetworkMode"], "mounts": inspected["Mounts"]})
            self.report["omp_version"] = self.execute("omp", "--version").stdout.strip()
            self.assert_that("actual OMP version observed", bool(pi.VERSION_RE.search(self.report["omp_version"])), self.report["omp_version"])
            if self.name == "arrival":
                self.adapter_smoke()
            self.execute("mkdir", "-p", "/tmp/pi-agent", "/tmp/pi-home/.omp/agent/agents", "/sandbox/work", "/sandbox/pi-sessions")
            self.docker("cp", str(Path(__file__).resolve()), self.container + ":/tmp/proof-driver.py")
            self.provision_file(scratch, "config.yml", "/tmp/pi-agent/config.yml", OMP_CONFIG)
            self.provision_file(scratch, "models.yml", "/tmp/pi-agent/models.yml", MODELS)
            self.provision_file(scratch, "proof-agent.md", "/tmp/pi-home/.omp/agent/agents/proof-agent.md", AGENT)
            for marker in ("1", "2", "child", "owner"):
                self.provision_file(scratch, "tool-" + marker + ".sh", "/tmp/proof-tool-" + marker + ".sh",
                                    f"#!/bin/sh\ntouch /sandbox/tool-start-{marker}\nsleep 6\nprintf 'PROOF-ORDINARY-{marker}\\n'\n")
            if comms:
                self.provision_file(scratch, "mcp.json", "/tmp/pi-agent/mcp.json", encoded(OMP_MCP_CONFIG))
                claim = {"task_id": self.task["id"], "run_id": self.run["id"], "claim_token": self.run["claim_token"],
                         "bus_url": "http://127.0.0.1:18080"}
                self.provision_file(scratch, "claim.json", "/sandbox/aos-run.json", encoded(claim), 0o600)
                self.docker("exec", "--user", "0", self.container, "chown", "1000:1000", "/sandbox/aos-run.json")
                self.execute("chmod", "600", "/sandbox/aos-run.json")
                self.report["claim_mode"] = self.execute("stat", "-c", "%a", "/sandbox/aos-run.json").stdout.strip()
                self.assert_that("receiver claim private outside work tree", self.report["claim_mode"] == "600",
                                 "/sandbox/aos-run.json mode observed with stat; only receiver credential copied")
            else:
                self.report["off_files_absent"] = self.execute("sh", "-c", "test ! -e /sandbox/aos-run.json && test ! -e /tmp/pi-agent/mcp.json", check=False).returncode == 0
            if extensions:
                self.provision_file(scratch, "replace.ts", extensions[0], REPLACER)
            if self.name in ("stop-failure", "timeout", "malformed"):
                self.provision_file(scratch, "fault.py", "/tmp/proof-fault.py", FAULT, 0o755)
                self.provision_file(scratch, "fault-mode", "/tmp/proof-fault-mode", self.name)
                self.docker("exec", "--user", "0", self.container, "sh", "-c",
                            "cp -L /usr/local/bin/aos-agent /usr/local/bin/aos-agent.proof-saved && "
                            "rm /usr/local/bin/aos-agent && cp /tmp/proof-fault.py /usr/local/bin/aos-agent && chmod 755 /usr/local/bin/aos-agent")
                self.report["fault_fixture"] = self.name + ": only named helper command intercepted; all others delegate image helper"
            self.relay = subprocess.Popen(["docker", "exec", "-i", self.container, "python3", "-u", "/tmp/proof-driver.py", "--container-relay"],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            self.relay_stderr = []
            self.start_thread(self.read_relay)
            self.start_thread(lambda: self.relay_stderr.extend(self.relay.stderr.readlines()))
            if not self.ready.wait(15):
                raise RuntimeError("container relay not ready")
            argv = ["docker", "exec", "-i", "-w", "/sandbox/work", self.container, "sh", "-c", command]
            self.report["command"] = shlex.join(argv)
            self.harness = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.start_thread(self.watch_markers)
            started = time.monotonic()
            try:
                self.stdout, self.stderr = self.harness.communicate(prompt, timeout=330)
            except subprocess.TimeoutExpired:
                self.harness.kill()
                self.stdout, self.stderr = self.harness.communicate(timeout=10)
                raise TimeoutError("OMP exceeded acceptance deadline")
            finally:
                self.report["omp_elapsed_seconds"] = time.monotonic() - started
                self.report["omp_rc"] = self.harness.returncode
            # Injection thread finishes before reading Store evidence.
            for thread in self.threads[2:]:
                thread.join(timeout=7)
            self.collect()
            self.evaluate()
            if self.name == "off":
                absent = self.execute("sh", "-c", "test ! -e /sandbox/aos-run.json && test ! -e /tmp/pi-agent/mcp.json && test ! -e /tmp/aos-agent", check=False)
                self.assert_that("off remains without messaging files after OMP", absent.returncode == 0, "observed after OMP exit")
                self.fresh_acceptance()
        except Exception as exc:
            self.report["failure"] = self.scrub(type(exc).__name__ + ": " + str(exc))
            self.assert_that("case execution completed", False, self.report["failure"])
            if self.container:
                self.collect()
        finally:
            self.stopping.set()
            # Stop OMP before relay; Docker removal also kills container-side execs.
            if self.harness is not None and self.harness.poll() is None:
                self.harness.kill()
                try:
                    self.harness.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.errors.append("harness cleanup exceeded deadline")
            if self.relay is not None:
                self.relay.terminate()
                try:
                    self.relay.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.relay.kill()
                    self.relay.wait(timeout=5)
            if self.container:
                try:
                    result = self.docker("rm", "-f", self.container, timeout=20, check=False)
                    self.report["cleanup"] = {"container": self.container, "rc": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
                    self.assert_that("bounded container removal", result.returncode == 0 and self.container in result.stdout, self.report["cleanup"])
                except Exception as exc:
                    self.report["cleanup"] = {"container": self.container, "error": type(exc).__name__}
                    self.assert_that("bounded container removal", False, self.report["cleanup"])
            self.pool.shutdown(wait=True, cancel_futures=True)
            for thread in self.threads:
                thread.join(timeout=2)
            self.report["errors"] = self.errors
            if self.errors:
                self.assert_that("no background errors", False, self.errors)
            self.retain("relay.stderr.txt", "".join(getattr(self, "relay_stderr", [])))
            self.retain("model-history.json", self.history)
            self.retain("bus-events.json", self.bus_events)
            self.retain("injections.json", self.injected)
            self.report["passed"] = bool(self.report["assertions"]) and all(c["passed"] for c in self.report["assertions"].values())
            self.retain("report.json", self.report)
        return self.report["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--case", choices=("all", *CASES), default="all")
    parser.add_argument("--container-relay", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.container_relay:
        return container_relay()
    if not args.image or args.out is None:
        parser.error("--image and --out are required")
    # Host imports only: the same file's relay mode needs no repository package.
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root))
    args.out.mkdir(parents=True, exist_ok=False, mode=0o700)
    relevant = ["agent_os/pi.py", "agent_os/openshell_runner.py", "docker/harness", "scripts/openshell_probe/mhm_live/driver.py"]
    metadata = {"source_sha": run_command(["git", "-C", str(root), "rev-parse", "HEAD"]).stdout.strip(),
                "implementation_dirty_status": run_command(["git", "-C", str(root), "status", "--short", "--", *relevant]).stdout,
                "image": args.image,
                "image_id": run_command(["docker", "image", "inspect", args.image, "--format", "{{.Id}}"]).stdout.strip(),
                "driver_command": shlex.join([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])}
    selected = CASES if args.case == "all" else (args.case,)
    results = {}
    for name in selected:
        out = args.out / name
        out.mkdir(mode=0o700)
        case = Case(name, args.image, out, metadata)
        # Raw credentials and real Store exist only in this auto-deleted scratch.
        with tempfile.TemporaryDirectory(prefix="aos-omp-acceptance-") as directory:
            try:
                results[name] = case.run_case(Path(directory))
            except Exception as exc:
                # Setup failures are failures, not skips, and other cases still run.
                case.assert_that("case setup completed", False, type(exc).__name__ + ": " + str(exc))
                case.report["passed"] = False
                case.retain("report.json", case.report)
                results[name] = False
        print(encoded({"case": name, "passed": results[name], "report": str(out / "report.json")}), flush=True)
    summary = {**metadata, "cases": results, "passed": all(results.values()),
               "unexercised_cases": [name for name in CASES if name not in selected],
               "reach": "owned disposable local fixtures, actual image OMP; no deployed runner/server claim"}
    (args.out / "report.json").write_text(json.dumps(summary, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
