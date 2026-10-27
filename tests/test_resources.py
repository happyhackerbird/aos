from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from agent_os import resources


def _git_dir(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-dir"],
        check=True, capture_output=True, text=True).stdout.strip()


def test_empty_resource_root_uses_host_registry_default(tmp_path):
    from agent_os.harness import env_from
    env = env_from({"AOS_RESOURCE_ROOT": "", "AOS_WORKTREE_ROOT": str(tmp_path / "worktrees")})
    assert env.resource_root == str(tmp_path / "resources")


@pytest.fixture
def owned(tmp_path, monkeypatch):
    monkeypatch.delenv("AOS_RESOURCE_ROOT", raising=False)
    monkeypatch.setattr(resources.shutil, "which", lambda *a, **kw: None)
    return resources.RunResources("AO-900-r1", str(tmp_path), dict(os.environ))


def test_detached_owned_process_stopped_unowned_same_cwd_retained(owned, tmp_path):
    children = []
    try:
        for env in (owned.env, {k: v for k, v in os.environ.items() if k not in owned.tags}):
            children.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                            cwd=tmp_path, env=env, start_new_session=True))
        time.sleep(0.1)
        owned.record()
        summary = owned.close([], 0)
        children[0].wait(timeout=3)  # psutil may already have reaped the test's child
        assert not psutil.pid_exists(children[0].pid)
        assert children[1].poll() is None
        assert f"stopped owned process {children[0].pid}" in summary
        assert f"retained unexplained process {children[1].pid}" in summary
        log = json.loads((tmp_path / ".aos/resources-AO-900-r1.json").read_text())
        assert log["closed"] is True
        assert "AOS_TOKEN" not in json.dumps(log)
        assert owned.close([], 0) == summary
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()


def test_process_identity_reuse_is_not_killed(owned, monkeypatch):
    class Reused:
        pid = 999999
        def create_time(self): return 1
        def environ(self): return owned.tags
        def name(self): return "server"
        def terminate(self): raise psutil.NoSuchProcess(self.pid)
    monkeypatch.setattr(resources.psutil, "process_iter", lambda: iter([Reused()]))
    owned.stop_processes()
    assert not any("stopped owned" in e for e in owned.events)


def test_shared_colima_is_retained_even_if_tagged(owned, monkeypatch):
    class Shared:
        pid = 999999
        def create_time(self): return 1
        def environ(self): return owned.tags
        def name(self): return "colima"
        def children(self, recursive=False): return []
        def terminate(self): pytest.fail("must retain shared Colima")
    monkeypatch.setattr(resources.psutil, "process_iter", lambda: iter([Shared()]))
    owned.stop_processes()
    assert "retained shared service" in owned.events[0]


def test_tagged_shared_service_helpers_are_retained(owned, monkeypatch):
    class Helper:
        pid = 999997
        def name(self): return "ssh"
        def create_time(self): return 1
        def environ(self): return owned.tags
        def terminate(self): pytest.fail("shared service helper must survive")
    helper = Helper()
    class Shared:
        pid = 999998
        def name(self): return "colima"
        def create_time(self): return 1
        def environ(self): return owned.tags
        def children(self, recursive=False): return [helper]
        def terminate(self): pytest.fail("shared service must survive")
    monkeypatch.setattr(resources.psutil, "process_iter", lambda: iter([Shared(), helper]))
    owned.stop_processes()
    assert any("retained shared-service subtrees" in event for event in owned.events)


def test_descendant_tracking_does_not_claim_shared_subtrees(owned, monkeypatch):
    class Shared:
        def name(self): return "colima"
        def children(self): pytest.fail("do not enter a shared subtree")
    class Root:
        def name(self): return "harness"
        def children(self): return [Shared()]
    owned.record()
    monkeypatch.setattr(owned, "registered_processes", lambda: {1: Root()})
    owned.snapshot_descendants()
    assert not owned.process_path.exists()


def test_docker_teardown_only_labelled_resources_and_never_volumes(owned, monkeypatch):
    owned.docker = "docker"
    calls = []
    def docker(*args):
        calls.append(args)
        if args[:2] == ("ps", "-aq"):
            return "owned-container\n"
        if args[:3] == ("network", "ls", "-q"):
            return "owned-network\n"
        if len(args) > 1 and args[1] == "inspect":
            labels = {"aos.run": owned.run_id}
            return json.dumps([{"Config": {"Labels": labels}, "Labels": labels}])
        return ""
    monkeypatch.setattr(owned, "_docker", docker)
    owned.stop_docker()
    assert ("rm", "-f", "owned-container") in calls
    assert ("network", "rm", "owned-network") in calls
    assert sum(c == ("rm", "-f", "owned-container") for c in calls) == 1
    assert all("volume" not in c and "prune" not in c for c in calls)
    assert any("label=com.docker.compose.project=aos-ao-900-r1" in c for c in calls)
    assert any("label=aos.run=AO-900-r1" in c for c in calls)


@pytest.mark.parametrize("labels, reason", [({"aos.run": "AO-900-r1", "aos.shared": "true"}, "shared Docker"),
                                          ({"aos.run": "another-run"}, "ownership changed")])
def test_shared_or_unowned_docker_resources_survive(owned, monkeypatch, labels, reason):
    owned.docker = "docker"
    calls = []
    def docker(*args):
        calls.append(args)
        if args[:2] == ("ps", "-aq"): return "candidate\n"
        if args[:2] == ("container", "inspect"):
            return json.dumps([{"Config": {"Labels": labels}}])
        return ""
    monkeypatch.setattr(owned, "_docker", docker)
    owned.stop_docker()
    assert not any(c[:1] == ("rm",) for c in calls)
    assert any(reason in event for event in owned.events)


def test_tracked_detached_descendant_survives_parent_exit_but_not_cleanup(owned, tmp_path):
    # Observe a child while ancestry is available; then it detaches, clears env,
    # changes cwd and outlives the parent. Cleanup must use persisted identity.
    pid_file = tmp_path / "descendant.pid"
    script = ("import subprocess,time,pathlib; "
              "p=subprocess.Popen(['sleep','60'], env={}, cwd='/tmp', start_new_session=True); "
              f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(.5)")
    owned.record()
    owned.start_tracking()
    parent = subprocess.Popen([sys.executable, "-c", script], cwd=tmp_path, env=owned.env)
    child = None
    try:
        owned.register(parent.pid)
        parent.wait(timeout=5)
        child = psutil.Process(int(pid_file.read_text()))
        assert child.is_running()
        identities = [json.loads(line) for line in owned.process_path.read_text().splitlines()]
        assert any(row["pid"] == child.pid and row["created"] == child.create_time() for row in identities)
        summary = owned.close([], 0)
        assert f"stopped owned process {child.pid}" in summary
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    finally:
        owned.stop_tracking()
        if parent.poll() is None: parent.kill()
        parent.wait()
        if child is not None and child.is_running() and child.status() != psutil.STATUS_ZOMBIE: child.kill()


def test_workspace_lock_excludes_another_run_or_sweep(tmp_path):
    lease = resources.lock_worktree(str(tmp_path / "worktree"), str(tmp_path / "resources"))
    try:
        with pytest.raises(BlockingIOError):
            resources.lock_worktree(str(tmp_path / "worktree"), str(tmp_path / "resources"))
    finally:
        lease.close()
    resources.lock_worktree(str(tmp_path / "worktree"), str(tmp_path / "resources")).close()


def test_docker_failure_is_reported(owned, monkeypatch):
    owned.docker = "docker"
    def fail(*args): raise subprocess.TimeoutExpired("docker", 30)
    monkeypatch.setattr(owned, "_docker", fail)
    owned.stop_docker()
    assert "Docker resource inspection failed" in owned.events


def test_repeated_docker_recovery_keeps_original_endpoint(owned, monkeypatch):
    context = {"name": "A"}
    calls = []
    monkeypatch.setattr(resources.shutil, "which", lambda *args, **kw: "docker")
    def docker(self, *args):
        calls.append((context["name"], args))
        return context["name"] if args == ("context", "show") else ""
    monkeypatch.setattr(resources.RunResources, "_docker", docker)
    monkeypatch.setattr(resources.RunResources, "stop_processes", lambda self: None)
    env = {k: v for k, v in os.environ.items() if k not in ("DOCKER_HOST", "DOCKER_CONTEXT")}
    previous = resources.RunResources(owned.run_id, owned.worktree, env)
    previous.runner_created -= 1  # The recorded runner has exited; this PID was reused.
    previous.record()
    endpoint = previous.docker_endpoint
    context["name"] = "B"
    current = resources.RunResources("AO-900-r2", owned.worktree, env)
    for _ in range(2):
        resources.recover_runs([{"id": previous.run_id, "status": "blocked"}], current)
        saved = json.loads(previous.record_path.read_text())
        assert saved["docker_endpoint"] == endpoint
        assert saved["docker_status"] == "pending"
        assert saved["cleanup_complete"] is False
    assert not any(name == "B" and args != ("context", "show") for name, args in calls)
    context["name"] = "A"
    current = resources.RunResources("AO-900-r3", owned.worktree, env)
    resources.recover_runs([{"id": previous.run_id, "status": "blocked"}], current)
    saved = json.loads(previous.record_path.read_text())
    assert saved["docker_endpoint"] == endpoint
    assert saved["docker_status"] == "complete"
    assert saved["cleanup_complete"] is True


def test_docker_discovery_failure_never_records_cleanup_complete(owned, monkeypatch):
    monkeypatch.setattr(resources.shutil, "which", lambda *args, **kw: "docker")
    def fail(self, *args): raise subprocess.CalledProcessError(1, "docker")
    monkeypatch.setattr(resources.RunResources, "_docker", fail)
    monkeypatch.setattr(resources.RunResources, "stop_processes", lambda self: None)
    env = {k: v for k, v in os.environ.items() if k not in ("DOCKER_HOST", "DOCKER_CONTEXT")}
    previous = resources.RunResources(owned.run_id, owned.worktree, env)
    previous.close([], 0)
    saved = json.loads(previous.record_path.read_text())
    assert saved["docker_endpoint"] is None
    assert saved["docker_status"] == "unavailable"
    assert saved["cleanup_complete"] is False
    current = resources.RunResources("AO-900-r2", owned.worktree, env)
    assert resources.recover_runs([{"id": previous.run_id, "status": "blocked"}], current)
    assert json.loads(previous.record_path.read_text())["cleanup_complete"] is False


def test_registered_reused_pid_is_retained(owned, tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path)
    try:
        directory = tmp_path / ".aos"
        directory.mkdir()
        identity = {"pid": child.pid, "created": psutil.Process(child.pid).create_time() - 1,
                    "run_id": owned.run_id, "host": resources.socket.gethostname()}
        (directory / f"resources-{owned.run_id}.processes.jsonl").write_text(json.dumps(identity) + "\n")
        owned.stop_processes()
        assert child.poll() is None
        assert f"retained reused PID {child.pid}" in owned.events
    finally:
        child.kill()
        child.wait()


def test_unfinished_previous_run_recovered_after_new_claim(owned, tmp_path):
    owned.runner_created -= 1  # Simulate an abandoned record, not a live runner.
    owned.record()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             cwd=tmp_path, env=owned.env, start_new_session=True)
    try:
        time.sleep(.1)
        current = resources.RunResources("AO-900-r2", str(tmp_path), dict(os.environ))
        events = resources.recover_runs([{"id": owned.run_id, "status": "blocked"}], current)
        child.wait(timeout=3)
        assert not psutil.pid_exists(child.pid)
        assert any(f"stopped owned process {child.pid}" in e for e in events)
        assert json.loads((tmp_path / f".aos/resources-{owned.run_id}.json").read_text())["closed"]
    finally:
        if child.poll() is None: child.kill()
        child.wait()


def test_active_previous_run_is_never_recovered(owned):
    owned.record()
    current = resources.RunResources("AO-900-r2", owned.worktree, dict(os.environ))
    assert resources.recover_runs([{"id": owned.run_id, "status": "running"}], current) == []


@pytest.mark.parametrize("status", ["claimed", "running", "blocked"])
def test_recovery_retains_live_runner_resources(owned, tmp_path, status):
    owned.record()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             cwd=tmp_path, env={}, start_new_session=True)
    try:
        owned.register(child.pid)
        before = owned.record_path.read_bytes()
        current = resources.RunResources("AO-900-r2", owned.worktree, owned.env)
        with resources.lock_worktree(owned.worktree, str(tmp_path / "locks")):
            resources.recover_runs([{"id": owned.run_id, "status": status}], current)
        assert child.poll() is None
        assert owned.record_path.read_bytes() == before
    finally:
        if child.poll() is None: child.kill()
        child.wait()


@pytest.mark.parametrize("owner_state", ["zombie", "denied"])
@pytest.mark.parametrize("recovery", ["same-task", "sweep"])
def test_unverifiable_runner_does_not_block_owned_child_recovery(tmp_path, monkeypatch, recovery, owner_state):
    """An exited-but-unreaped runner, or a recorded runner PID whose identity cannot be
    inspected, no longer owns anything: both recovery paths stop its registered children."""
    import io
    import httpx
    from agent_os.cleanup import cleanup_runs

    worktree = tmp_path / "worktrees" / "repo-a" / "AO-900"
    worktree.mkdir(parents=True)
    env = {"PATH": "", "AOS_WORKTREE_ROOT": str(tmp_path / "worktrees"),
           "AOS_RESOURCE_ROOT": str(tmp_path / "registry")}
    previous = resources.RunResources("AO-900-r1", str(worktree), env)
    children = []
    try:
        for _ in range(3):
            children.append(subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=tmp_path, env={}, start_new_session=True))
        runner, owned, unrelated = children
        owner = psutil.Process(runner.pid)
        previous.runner_pid = owner.pid
        previous.runner_created = owner.create_time()
        previous.task_id = "AO-900"
        previous.bus_url = "http://bus.test"
        previous.record()
        previous.register(owned.pid)
        if owner_state == "zombie":
            runner.kill()
            deadline = time.monotonic() + 5
            while owner.status() != psutil.STATUS_ZOMBIE:
                assert time.monotonic() < deadline, "Runner did not exit"
                time.sleep(.01)
        else:
            # The recorded PID is now held by a process this user may not inspect: PID reuse.
            create_time = psutil.Process.create_time

            def denied(self):
                if self.pid == runner.pid:
                    raise psutil.AccessDenied(pid=self.pid)
                return create_time(self)

            monkeypatch.setattr(psutil.Process, "create_time", denied)
        run = {"id": previous.run_id, "status": "blocked", "worktree": str(worktree)}
        if recovery == "same-task":
            current = resources.RunResources("AO-900-r2", str(worktree), env)
            resources.recover_runs([run], current)
        else:
            task = {"id": "AO-900", "repo": "repo-a", "runs": [run]}
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json=task))
            with httpx.Client(base_url="http://bus.test", transport=transport) as client:
                out, err = io.StringIO(), io.StringIO()
                assert cleanup_runs(client, environ=env, out=out, err=err) == 0, err.getvalue()
        owned.wait(timeout=5)
        assert not psutil.pid_exists(owned.pid)
        assert unrelated.poll() is None
        if owner_state == "zombie":
            assert owner.status() == psutil.STATUS_ZOMBIE
        else:
            assert runner.poll() is None, "the uninspectable process at the recorded PID is not ours to stop"
        assert json.loads(previous.record_path.read_text())["cleanup_complete"]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()


@pytest.mark.parametrize("damage", ["truncated", "encoding", "symlink"])
def test_unreadable_ledger_stays_pending_until_repaired(owned, tmp_path, damage):
    owned.record()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             cwd=tmp_path, env={}, start_new_session=True)
    try:
        owned.register(child.pid)
        identity = owned.process_path.read_bytes()
        if damage == "symlink":
            target = tmp_path / "ledger-target"
            target.write_bytes(identity)
            owned.process_path.unlink()
            owned.process_path.symlink_to(target)
        else:
            owned.process_path.write_bytes(identity[:-12] if damage == "truncated" else b"\xff")
        summary = owned.close([], 0)
        assert child.poll() is None
        assert resources.cleanup_pending(owned.events)
        assert json.loads(owned.record_path.read_text())["cleanup_complete"] is False
        assert owned.close([], 0) == summary
        # Cleanup must release the registration gate even for invalid UTF-8.
        with resources.resource_gate(owned.process_path):
            pass
        current = resources.RunResources("AO-900-r2", owned.worktree, owned.env)
        resources.recover_runs([{"id": owned.run_id, "status": "blocked"}], current)
        assert child.poll() is None
        assert json.loads(owned.record_path.read_text())["cleanup_complete"] is False
        if damage == "symlink":
            assert target.read_bytes() == identity
            owned.process_path.unlink()
        owned.process_path.write_bytes(identity)
        resources.recover_runs([{"id": owned.run_id, "status": "blocked"}], current)
        child.wait(timeout=3)
        assert not psutil.pid_exists(child.pid)
        assert json.loads(owned.record_path.read_text())["cleanup_complete"] is True
        assert resources.recover_runs([{"id": owned.run_id, "status": "blocked"}], current) == []
    finally:
        if child.poll() is None: child.kill()
        child.wait()


def test_legacy_ledger_migration_does_not_discard_unresolved_ownership(owned, tmp_path):
    owned.runner_created -= 1
    owned.record()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             cwd=tmp_path, env={}, start_new_session=True)
    try:
        owned.register(child.pid)
        identity = owned.process_path.read_bytes()
        owned.process_path.write_bytes(identity[:-12])
        current = resources.RunResources("AO-900-r2", owned.worktree,
                                         owned.env | {"AOS_RESOURCE_ROOT": str(tmp_path / "registry")})
        migrated, _ = resources.record_paths(owned.worktree, owned.run_id, current.env)
        for _ in range(2):
            events = resources.recover_runs([{"id": owned.run_id, "status": "blocked"}], current)
            assert resources.cleanup_pending(events)
            assert child.poll() is None
            assert not migrated.exists()
            assert json.loads(owned.record_path.read_text())["closed"] is False
        owned.process_path.write_bytes(identity)
        resources.recover_runs([{"id": owned.run_id, "status": "blocked"}], current)
        child.wait(timeout=3)
        assert not psutil.pid_exists(child.pid)
        assert json.loads(migrated.read_text())["cleanup_complete"] is True
    finally:
        if child.poll() is None: child.kill()
        child.wait()


def test_valid_ledger_entries_are_cleaned_despite_a_truncated_append(owned, tmp_path):
    owned.record()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             cwd=tmp_path, env={}, start_new_session=True)
    try:
        owned.register(child.pid)
        with owned.process_path.open("a") as stream:
            stream.write('{"pid":')
        owned.close([], 0)
        child.wait(timeout=3)
        assert not psutil.pid_exists(child.pid)
        assert json.loads(owned.record_path.read_text())["cleanup_complete"] is False
    finally:
        if child.poll() is None: child.kill()
        child.wait()


def test_own_registers_before_exec_without_bus_token(owned, monkeypatch, tmp_path):
    from agent_os.cli import main
    owned.record()
    for key, value in owned.tags.items(): monkeypatch.setenv(key, value)
    monkeypatch.delenv("AOS_TOKEN", raising=False)
    captured = []
    monkeypatch.setattr(os, "execvpe", lambda *args: captured.append(args))
    assert main(["own", "--", "sleep", "60"]) == 127
    assert captured[0][:2] == ("sleep", ["sleep", "60"])
    identity = json.loads((tmp_path / f".aos/resources-{owned.run_id}.processes.jsonl").read_text())
    assert identity["pid"] == os.getpid()
    assert identity["created"] == psutil.Process().create_time()


def test_own_refuses_closed_run(owned, monkeypatch):
    owned.closed = True
    owned.record()
    for key, value in owned.tags.items(): monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="active local run"):
        resources.own_command(["sleep", "60"])


@pytest.fixture
def cache_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".angular/\ndist/\n")
    cache = tmp_path / ".angular"
    cache.mkdir()
    (cache / "cache").write_bytes(b"x" * 4096)
    return tmp_path


def test_caches_retained_by_default_and_pruned_when_oversize(cache_repo):
    cache = cache_repo / ".angular"
    admin = _git_dir(cache_repo)
    assert resources.prune_caches(str(cache_repo), [{"path": ".angular"}], 0, admin)[0].startswith("retained cache")
    assert cache.exists()
    events = resources.prune_caches(str(cache_repo), [{"path": ".angular", "max_bytes": 10}], 0, admin)
    assert not cache.exists()
    assert "free-space increase" in events[0]


def test_caches_unverified_without_git_dir_and_pruned_with_pinned_admin(cache_repo):
    cache = cache_repo / ".angular"
    unverified = resources.prune_caches(
        str(cache_repo), [{"path": ".angular", "max_bytes": 10}], 0, git_dir=None)
    assert cache.exists()
    assert unverified == ["retained cache .angular: worktree git state unverified"]

    admin = _git_dir(cache_repo)
    events = resources.prune_caches(
        str(cache_repo), [{"path": ".angular", "max_bytes": 10}], 0, git_dir=admin)
    assert not cache.exists()
    assert "pruned cache .angular:" in events[0]


def test_provenance_round_trip_rejects_missing_unreadable_and_foreign_worktree(tmp_path):
    root = tmp_path / "resources"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git_dir = str(tmp_path / "admin")

    assert resources.read_provenance(str(worktree), str(root)) is None
    resources.write_provenance(str(worktree), str(root), git_dir)
    path = resources.provenance_path(str(worktree), str(root))
    digest = hashlib.sha256(str(worktree.resolve()).encode()).hexdigest()
    assert path == root / f"worktree-{digest}.provenance"
    assert path.with_name(f"workspace-{digest}.lock").parent == path.parent
    assert path.stat().st_mode & 0o777 == 0o600
    assert resources.read_provenance(str(worktree), str(root)) == git_dir

    path.write_bytes(b"\xff")
    assert resources.read_provenance(str(worktree), str(root)) is None
    path.write_text(json.dumps({"worktree": str(tmp_path / "other"), "git_dir": git_dir}))
    assert resources.read_provenance(str(worktree), str(root)) is None


def test_cache_pressure_and_age(cache_repo, monkeypatch):
    monkeypatch.setattr(resources, "disk_free", lambda path: 0)
    resources.prune_caches(str(cache_repo), [{"path": ".angular"}], 1, _git_dir(cache_repo))
    assert not (cache_repo / ".angular").exists()
    cache = cache_repo / "dist"
    cache.mkdir()
    (cache / "out").write_text("output")
    for path in (cache / "out", cache): os.utime(path, (1, 1))
    resources.prune_caches(str(cache_repo), [{"path": "dist", "max_age_seconds": 10}], 0, _git_dir(cache_repo))
    assert not cache.exists()


def test_tracked_deliverables_and_symlinks_are_never_pruned(cache_repo, tmp_path_factory):
    cache = cache_repo / ".angular"
    subprocess.run(["git", "-C", str(cache_repo), "add", "-f", ".angular/cache"], check=True)
    result = resources.prune_caches(str(cache_repo), [{"path": ".angular", "max_bytes": 1}], 0, _git_dir(cache_repo))
    assert cache.exists()
    assert "tracked files" in result[0]
    outside = tmp_path_factory.mktemp("deliverable")
    (cache_repo / "dist").symlink_to(outside, target_is_directory=True)
    result = resources.prune_caches(str(cache_repo), [{"path": "dist", "max_bytes": 1}], 0, _git_dir(cache_repo))
    assert outside.exists()
    assert "symbolic link" in result[0]


def test_pruning_revalidates_containment_even_without_policy_loader(cache_repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("protected-cache")
    sentinel = outside / "sentinel"
    sentinel.write_text("deliverable")
    relative = os.path.relpath(outside, cache_repo)
    events = resources.prune_caches(str(cache_repo), [{"path": relative, "max_bytes": 1}], 0)
    assert sentinel.read_text() == "deliverable"
    assert "unsafe cache path" in events[0]


@pytest.mark.parametrize("path", ["../outside", "/tmp/elsewhere", ".", ".git/objects", ".aos"])
def test_unsafe_policy_rejected(tmp_path, path):
    (tmp_path / ".aos").mkdir()
    (tmp_path / ".aos/cleanup.toml").write_text(f'[[cache]]\npath = "{path}"\n')
    with pytest.raises(ValueError): resources.load_policy(str(tmp_path))
