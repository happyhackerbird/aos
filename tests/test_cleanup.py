"""`aos cleanup` (agent_os.cleanup.cleanup_runs): the sweep of terminal runs recorded on this host.
Moved out of the host runner's tests on 2026-10-13; the records come from the bus API and the
resource ledger directly, the way `aos integrate --backend host` still produces them."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest
from fastapi.testclient import TestClient

from agent_os import cleanup, resources
from agent_os.api import create_app
from agent_os.config import Settings
from agent_os.store import Store
from agent_os.telegram import Telegram


@pytest.fixture
def client(settings: Settings, store: Store, telegram: Telegram) -> TestClient:
    test_client = TestClient(create_app(settings, store=store, telegram=telegram, background=False))
    test_client.headers["Authorization"] = "Bearer test-bus-token"
    return test_client


@pytest.fixture
def worktree_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "worktrees"
    monkeypatch.setenv("AOS_WORKTREE_ROOT", str(root))
    monkeypatch.setenv("AOS_RESOURCE_ROOT", str(tmp_path / "resources"))
    monkeypatch.setenv("AOS_HOST", "mac")
    return root


def _terminal_run(client: TestClient, worktree_root: Path, **overrides: Any) -> tuple[dict, dict, Path, tuple[Path, Path]]:
    """A task with one finished run on the bus and a crashed-looking resource record for its worktree."""
    body: dict[str, Any] = {"repo": "repo-a", "title": "Task", "brief": "do the thing", "acceptance": ["true"]}
    body.update(overrides)
    task = client.post("/tasks", json=body).json()
    assert client.post(f"/tasks/{task['id']}/ready").status_code == 200
    run = client.post(f"/tasks/{task['id']}/claim", json={
        "worker": "test", "worker_host": "mac", "expected_spec_hash": task["spec_hash"],
        "effective_acceptance": task["acceptance"], "acceptance_source": "task",
    }).json()
    worktree = worktree_root / task["repo"] / task["id"]
    worktree.mkdir(parents=True)
    token = run["claim_token"]
    assert client.post(f"/runs/{run['id']}/start", json={"claim_token": token, "base_commit": "0" * 40,
                                                        "branch": f"ao/{task['id']}", "worktree": str(worktree)}).status_code == 200
    assert client.post(f"/runs/{run['id']}/result", json={"claim_token": token, "status": "review",
                                                         "verdict": "pass", "report": "done"}).status_code == 200
    record = resources.RunResources(run["id"], str(worktree), dict(os.environ))
    record.task_id = task["id"]
    record.bus_url = str(client.base_url).rstrip("/")
    record.record()
    paths = resources.record_paths(str(worktree), run["id"], os.environ)
    data = json.loads(paths[0].read_text())
    data.update(closed=False, closing=False, runner_pid=999999999, runner_created=1, events=[])
    paths[0].write_text(json.dumps(data))
    return task, client.get(f"/tasks/{task['id']}").json()["runs"][0], worktree, paths


def _sweep(client: TestClient) -> tuple[int, io.StringIO, io.StringIO]:
    out, err = io.StringIO(), io.StringIO()
    code = cleanup.cleanup_runs(client, environ=dict(os.environ), out=out, err=err)
    return code, out, err


def test_cleanup_provenance_file_at_resource_root_is_ignored(client: TestClient, worktree_root: Path) -> None:
    worktree = worktree_root / "repo-a" / "AO-provenance"
    worktree.mkdir(parents=True)
    resources.write_provenance(str(worktree), os.environ["AOS_RESOURCE_ROOT"], "/admin")
    path = resources.provenance_path(str(worktree), os.environ["AOS_RESOURCE_ROOT"])
    code, _out, err = _sweep(client)
    assert code == 0
    assert path.exists()
    assert err.getvalue() == ""


def test_cleanup_sweep_without_task_retry_or_worktree(client: TestClient, worktree_root: Path) -> None:
    task, run, worktree, paths = _terminal_run(client, worktree_root)
    previous = resources.RunResources(run["id"], str(worktree), dict(os.environ))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             cwd=worktree, env=previous.env, start_new_session=True)
    try:
        previous.register(child.pid)
        shutil.rmtree(worktree)  # Host ownership record must survive workspace loss.
        code, out, err = _sweep(client)
        assert code == 0, err.getvalue()
        child.wait(timeout=3)
        assert not psutil.pid_exists(child.pid)
        assert f"stopped owned process {child.pid}" in out.getvalue()
        assert json.loads(paths[0].read_text())["closed"]
        assert len(client.get(f"/tasks/{task['id']}").json()["runs"]) == 1
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_cleanup_sweep_preserves_original_docker_endpoint_across_retries(
        client: TestClient, worktree_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task, run, worktree, paths = _terminal_run(client, worktree_root)
    context = {"name": "A"}
    original_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda cmd, **kw: "docker" if cmd == "docker" else original_which(cmd, **kw))
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)

    def docker(self, *args):
        return context["name"] if args == ("context", "show") else ""

    monkeypatch.setattr(resources.RunResources, "_docker", docker)
    monkeypatch.setattr(resources.RunResources, "stop_processes", lambda self: None)
    original = resources.RunResources(run["id"], str(worktree), dict(os.environ))
    original.task_id = task["id"]
    original.bus_url = str(client.base_url).rstrip("/")
    original.runner_pid = 999999999
    original.record()
    endpoint = original.docker_endpoint
    context["name"] = "B"
    for _ in range(2):
        assert _sweep(client)[0] == 1
        saved = json.loads(paths[0].read_text())
        assert saved["docker_endpoint"] == endpoint and saved["cleanup_complete"] is False
        assert saved["docker_status"] == "pending"
    context["name"] = "A"
    assert _sweep(client)[0] == 0
    saved = json.loads(paths[0].read_text())
    assert saved["docker_endpoint"] == endpoint and saved["cleanup_complete"] is True


@pytest.mark.parametrize("reason", ["live", "locked", "bus_down"])
def test_cleanup_sweep_preserves_unconfirmed_or_active_resources(
        client: TestClient, worktree_root: Path, monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    task, run, worktree, paths = _terminal_run(client, worktree_root)
    previous = resources.RunResources(run["id"], str(worktree), dict(os.environ))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=previous.env, cwd=worktree)
    lease = None
    try:
        previous.register(child.pid)
        if reason == "locked":
            lease = resources.lock_worktree(str(worktree), os.environ["AOS_RESOURCE_ROOT"])
        elif reason == "live":
            original_get = client.get

            def get_live(url, **kwargs):
                response = original_get(url, **kwargs)
                payload = response.json()
                payload["runs"][0]["status"] = "running"
                return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

            monkeypatch.setattr(client, "get", get_live)
        else:
            def unavailable(*args, **kwargs):
                raise httpx.ConnectError("offline")

            monkeypatch.setattr(client, "get", unavailable)
        code, _out, _err = _sweep(client)
        assert child.poll() is None
        assert code == (1 if reason == "bus_down" else 0)
        assert not json.loads(paths[0].read_text())["closed"]
    finally:
        if lease is not None:
            lease.close()
        child.kill()
        child.wait()
