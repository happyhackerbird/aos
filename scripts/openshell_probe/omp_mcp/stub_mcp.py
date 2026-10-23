#!/usr/bin/env python3
"""A logging MCP stdio server: one tool, every request appended to $STUB_LOG."""
import json, os, sys
log = open(os.environ.get("STUB_LOG", "/tmp/stub.log"), "a")
log.write(f"start argv={sys.argv[1:]}\n"); log.flush()
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    log.write(f"recv {msg.get('method')}\n"); log.flush()
    if "id" not in msg:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": msg["params"].get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "stub", "version": "0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "stub_ping", "description": "Ping the stub.",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        import time
        log.write(f"call start t={time.time():.0f}\n"); log.flush()
        time.sleep(float(os.environ.get("STUB_SLEEP", "0")))
        log.write(f"call end t={time.time():.0f}\n"); log.flush()
        result = {"content": [{"type": "text", "text": "pong"}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n"); sys.stdout.flush()
