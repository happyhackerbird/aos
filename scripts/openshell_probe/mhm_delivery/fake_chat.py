#!/usr/bin/env python3
"""A scripted fake OpenAI chat-completions model on 127.0.0.1:18080 (omp and pi).

FAKE_SCRIPT names a JSON file: a list of steps, consumed one per POST. A step is
    {"tool": "<tool name>", "args": {...}}   -> one tool call (streamed), finish_reason tool_calls
    {"text": "..."}                          -> an assistant text, finish_reason stop
After the list is exhausted every request gets {"text": "done-<n>"}.
Every request is logged to FAKE_LOG as one JSON line: {"n": request number, "t": time, "tools": [names],
"messages": the request's messages array verbatim}. The injected text (a frame) is found by grepping that log.
"""
import json, os, time
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG = os.environ.get("FAKE_LOG", "/tmp/fake.log")
STEPS = json.load(open(os.environ["FAKE_SCRIPT"])) if os.environ.get("FAKE_SCRIPT") else []
STATE = {"n": 0}


def chunk(delta, finish=None, usage=None):
    c = {"id": "c1", "object": "chat.completion.chunk", "model": "fake-model",
         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage:
        c["usage"] = usage
    return c


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
        names = [t.get("function", {}).get("name") or t.get("name") for t in req.get("tools") or []]
        with open(LOG, "a") as f:
            f.write(json.dumps({"n": n, "t": round(time.time(), 3), "path": self.path, "stream": req.get("stream"),
                                "tools": names, "messages": req.get("messages")}) + "\n")
        step = STEPS[n] if n < len(STEPS) else {"text": f"done-{n}"}
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        if "tool" in step:
            call = {"index": 0, "id": f"call_{n}", "type": "function",
                    "function": {"name": step["tool"], "arguments": json.dumps(step.get("args", {}))}}
            chunks = [chunk({"role": "assistant", "tool_calls": [call]}), chunk({}, "tool_calls", usage)]
        else:
            chunks = [chunk({"role": "assistant", "content": step["text"]}), chunk({}, "stop", usage)]
        if req.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            if "tool" in step:
                message = {"role": "assistant", "content": None, "tool_calls": [chunks[0]["choices"][0]["delta"]["tool_calls"][0]]}
                finish = "tool_calls"
            else:
                message = {"role": "assistant", "content": step["text"]}
                finish = "stop"
            body = json.dumps({"id": "c1", "object": "chat.completion", "model": "fake-model",
                               "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


HTTPServer(("127.0.0.1", 18080), H).serve_forever()
