#!/usr/bin/env python3
"""A scripted fake OpenAI Responses model on 127.0.0.1:18080 for `codex exec` (0.162.1 refuses wire_api=chat).

FAKE_SCRIPT names a JSON file: a list of steps, consumed one per POST. A step is
    {"call": {"name": "<tool>", "arguments": "<json string>", "namespace": "<optional>"}}  -> one function_call item
    {"text": "..."}                                                                      -> an assistant message
After the list is exhausted every request gets {"text": "done-<n>"}.
Every request is logged to FAKE_LOG as one JSON line: {"n", "t", "tools": [names], "input": the request's input array
verbatim, "instructions": the instructions string}. Hook-injected developer context is found by grepping that log.
"""
import json, os, time
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG = os.environ.get("FAKE_LOG", "/tmp/fake.log")
STEPS = json.load(open(os.environ["FAKE_SCRIPT"])) if os.environ.get("FAKE_SCRIPT") else []
STATE = {"n": 0}


def sse(handler, events):
    handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
    for e in events:
        handler.wfile.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "fake-model", "object": "model"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        n = STATE["n"]; STATE["n"] += 1
        tools = req.get("tools") or []
        names = [t.get("name") or f"<{t.get('type')}>" for t in tools]
        with open(LOG, "a") as f:
            f.write(json.dumps({"n": n, "t": round(time.time(), 3), "path": self.path, "tools": names,
                                "instructions": req.get("instructions"), "input": req.get("input")}) + "\n")
            if n == 0:  # the tool schemas once: the shell tool's argument key is read from here
                for t in tools:
                    if t.get("name") in ("exec_command", "shell", "mcp__aos") or not t.get("name"):
                        f.write(json.dumps({"n": 0, "schema": t}) + "\n")
        step = STEPS[n] if n < len(STEPS) else {"text": f"done-{n}"}
        rid = f"r{time.time_ns()}"
        if "call" in step:
            item = {"type": "function_call", "id": f"fc{n}", "call_id": f"call_{n}", "name": step["call"]["name"],
                    "arguments": step["call"].get("arguments", "{}")}
            if step["call"].get("namespace"):
                item["namespace"] = step["call"]["namespace"]
        else:
            item = {"type": "message", "role": "assistant", "id": f"m{n}",
                    "content": [{"type": "output_text", "text": step["text"], "annotations": []}]}
        sse(self, [{"type": "response.created", "response": {"id": rid}},
                   {"type": "response.output_item.done", "output_index": 0, "item": item},
                   {"type": "response.completed", "response": {"id": rid, "usage": {
                       "input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}])


HTTPServer(("127.0.0.1", 18080), H).serve_forever()
