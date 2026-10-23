"""Local macOS Codex sandbox probe; no model, bus or Docker invocation.

Run with the project's virtualenv Python outside a containing sandbox.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import queue
import signal
from pathlib import Path

from agent_os.resources import RunResources


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    # Keep registry outside /tmp: workspace-write normally allows temporary paths.
    probe_root = repo / ".aos" / "probes"
    probe_root.mkdir(parents=True, exist_ok=True)
    registry = Path(tempfile.mkdtemp(prefix="resource-sandbox-", dir=probe_root))
    workspace = Path(tempfile.mkdtemp(prefix="aos-sandbox-workspace-", dir="/tmp"))
    env = dict(os.environ, AOS_RESOURCE_ROOT=str(registry), PATH="/usr/bin:/bin", PYTHONPATH=str(repo))
    owned = RunResources("AO-probe-r1", str(workspace), env)
    owned.record()
    neighbor = registry / "other-run"
    neighbor.mkdir()
    command = [sys.executable, "-m", "agent_os.cli", "own", "--", "/usr/bin/true"]
    daemon_log = (registry / "app-server.stderr.txt").open("w")
    daemon = subprocess.Popen(["/opt/homebrew/bin/codex", "app-server", "--listen", "stdio://"],
                              cwd=workspace, env=owned.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=daemon_log, text=True, start_new_session=True)
    messages = queue.Queue()
    def read():
        for line in daemon.stdout:
            messages.put(json.loads(line))
    threading.Thread(target=read, daemon=True).start()
    request_id = 0
    def rpc(method, params):
        nonlocal request_id
        request_id += 1
        daemon.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        daemon.stdin.flush()
        while True:
            message = messages.get(timeout=30)
            if message.get("id") == request_id:
                if "error" in message: raise RuntimeError(message["error"])
                return message["result"]
    rpc("initialize", {"clientInfo": {"name": "aos-sandbox-probe", "version": "1"}})
    daemon.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
    daemon.stdin.flush()
    results = []
    try:
        for grant in (False, True):
            roots = [str(workspace)] + ([str(owned.record_path.parent)] if grant else [])
            result = rpc("command/exec", {"command": command, "cwd": str(workspace), "env": owned.tags,
                         "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": roots}, "timeoutMs": 10000})
            results.append({"probe": "own", "grant": grant, "exit_code": result["exitCode"],
                            "stderr": result.get("stderr", "")[-2000:]})
        forbidden = rpc("command/exec", {"command": [sys.executable, "-c",
                          "from pathlib import Path; Path(" + repr(str(neighbor / "forbidden")) +
                          ").write_text('must be denied')"], "cwd": str(workspace),
                         "sandboxPolicy": {"type": "workspaceWrite", "writableRoots":
                                           [str(workspace), str(owned.record_path.parent)]}, "timeoutMs": 10000})
        results.append({"probe": "neighbor_write", "exit_code": forbidden["exitCode"],
                        "stderr": forbidden.get("stderr", "")[-2000:]})
    finally:
        try: os.killpg(daemon.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: daemon.wait(timeout=5)
        except subprocess.TimeoutExpired: daemon.kill(); daemon.wait()
        daemon_log.close()
    rows = owned.process_path.read_text().splitlines() if owned.process_path.exists() else []
    passed = results[0]["exit_code"] != 0 and results[1]["exit_code"] == 0 and forbidden["exitCode"] != 0 and bool(rows)
    report = {"passed": passed, "workspace": str(workspace), "registry": str(registry),
              "registered_identities": [json.loads(row) for row in rows], "results": results}
    report_path = registry / "probe-result.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"Evidence: {report_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
