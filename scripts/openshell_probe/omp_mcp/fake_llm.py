#!/usr/bin/env python3
"""A fake OpenAI chat-completions endpoint on 127.0.0.1:18080: logs each request's tool names, answers 'hi'."""
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
LOG = os.environ.get("FAKE_LOG", "/tmp/fake.log")

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        with open(LOG, "a") as f: f.write(f"GET {self.path}\n")
        body = json.dumps({"data": [{"id": "fake-model", "object": "model"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        names = [t.get("function", {}).get("name") or t.get("name") for t in req.get("tools") or []]
        with open(LOG, "a") as f:
            f.write(f"POST {self.path} stream={req.get('stream')} tools={names}\n")
        import time as _t
        tool_msgs = [m for m in req.get("messages") or [] if m.get("role") == "tool"]
        with open(LOG, "a") as f:
            f.write(f"  t={_t.time():.0f} tool results={[str(m.get('content'))[:160] for m in tool_msgs]}\n")
        call = os.environ.get("FAKE_CALL")
        if call and not tool_msgs:
            first = {"id": "c1", "object": "chat.completion.chunk", "model": "fake-model",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                         {"index": 0, "id": "call_1", "type": "function",
                          "function": {"name": call, "arguments": "{}"}}]}, "finish_reason": None}]}
            done = {"id": "c1", "object": "chat.completion.chunk", "model": "fake-model",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for c in (first, done): self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        chunks = [{"id": "c1", "object": "chat.completion.chunk", "model": "fake-model",
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": None}]},
                  {"id": "c1", "object": "chat.completion.chunk", "model": "fake-model",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}]
        if req.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for c in chunks: self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            body = json.dumps({"id": "c1", "object": "chat.completion", "model": "fake-model",
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                                            "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

HTTPServer(("127.0.0.1", 18080), H).serve_forever()
