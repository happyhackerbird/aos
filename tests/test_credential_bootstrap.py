from __future__ import annotations

import os
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil
import pytest

from agent_os import credential_bootstrap
from agent_os import cli, harness


@pytest.mark.parametrize("command, extra", [("run", []), ("integrate", [])], ids=["run", "integrate"])
def test_cli_auth_survives_exec_without_an_inspectable_startup_token(command, extra):
    marker = "synthetic-bootstrap-credential"
    observed = []
    child = None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert child is not None
            observed.append((self.headers.get("Authorization") == "Bearer " + marker,
                             "AOS_TOKEN" not in psutil.Process(child.pid).environ(),
                             marker not in " ".join(psutil.Process(child.pid).cmdline())))
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"missing task"}')

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = dict(os.environ) | {"AOS_TOKEN": marker,
                                      "AOS_URL": f"http://127.0.0.1:{server.server_port}",
                                      # the OpenShell route refuses before its first bus call without these
                                      "AOS_HOST": "server", "AOS_OPENSHELL_PLATFORM": "linux",
                                      "AOS_OPENSHELL_IMAGE": "aos-harness:test"}
            child = subprocess.Popen([sys.executable, "-m", "agent_os.cli", command, "AO-1", *extra],
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = child.communicate(timeout=20)
        finally:
            server.shutdown()
            thread.join()
    assert child.returncode == 1
    assert observed == [(True, True, True)]
    assert marker.encode() not in stdout + stderr


def test_library_guard_refuses_an_initial_token(monkeypatch):
    class Process:
        def environ(self):
            return {"AOS_TOKEN": "synthetic"}

    monkeypatch.setattr(credential_bootstrap.psutil, "Process", Process)
    with pytest.raises(ValueError, match="use the aos CLI credential handoff"):
        credential_bootstrap.require_clean_initial_environment()


def test_resource_root_is_absolute_for_harnesses_with_a_different_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert harness.env_from({"AOS_RESOURCE_ROOT": "resources"}).resource_root == str(tmp_path / "resources")
