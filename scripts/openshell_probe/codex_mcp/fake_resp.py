#!/usr/bin/env python3
"""A fake OpenAI Responses endpoint on 127.0.0.1:18080 for codex exec: logs each request's tools (MCP-related entries
in full), answers text, or with FAKE_CALL (a JSON function_call item) calls a tool once first."""
import json, os, time
from http.server import BaseHTTPRequestHandler, HTTPServer
LOG = os.environ.get("FAKE_LOG", "/tmp/fake.log")

def sse(handler, events):
    handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
    for e in events:
        handler.wfile.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps({"data": [{"id": "fake-model", "object": "model"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        tools = req.get("tools") or []
        names = [t.get("name") or f"<{t.get('type')}>" for t in tools]
        outputs = [i for i in req.get("input") or [] if isinstance(i, dict) and i.get("type", "").endswith("_output")]
        with open(LOG, "a") as f:
            f.write(f"POST {self.path} t={time.time():.0f} tools={names}\n")
            for t in tools:
                if "mcp" in json.dumps(t) or not t.get("name"):
                    f.write(f"  TOOL {json.dumps(t)[:900]}\n")
            for o in outputs:
                f.write(f"  OUTPUT {json.dumps(o)[:400]}\n")
        call = os.environ.get("FAKE_CALL")
        rid = f"r{time.time_ns()}"
        if call and not outputs:
            item = json.loads(call)
            sse(self, [{"type": "response.created", "response": {"id": rid}},
                       {"type": "response.output_item.done", "output_index": 0, "item": item},
                       {"type": "response.completed", "response": {"id": rid, "usage": {
                           "input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}])
            return
        msg = {"type": "message", "role": "assistant", "id": "m1",
               "content": [{"type": "output_text", "text": "hi", "annotations": []}]}
        sse(self, [{"type": "response.created", "response": {"id": rid}},
                   {"type": "response.output_item.done", "output_index": 0, "item": msg},
                   {"type": "response.completed", "response": {"id": rid, "usage": {
                       "input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}])

HTTPServer(("127.0.0.1", 18080), H).serve_forever()
