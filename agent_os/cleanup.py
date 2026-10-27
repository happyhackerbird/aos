"""`aos cleanup`: sweep the compute of terminal runs recorded on this host (moved out of the retired
host runner on 2026-10-13, MVP decision 6; `aos integrate --backend host` still writes such records
for its per-run database container and acceptance processes).
"""
from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import IO

import httpx
import psutil

from agent_os.harness import env_from
from agent_os.resources import RunResources, cleanup_pending, lock_worktree


def cleanup_runs(client: httpx.Client, *, environ: dict[str, str], out: IO[str], err: IO[str]) -> int:
    """Sweep terminal runs across tasks using local records and authoritative bus state.

    Compute only: a sweep never prunes caches or retires a worktree.
    """
    try:
        env = env_from(environ)
    except ValueError as exc:
        print(str(exc), file=err)
        return 1
    root = Path(env.worktree_root).resolve()
    registry = Path(env.resource_root).resolve()
    candidates = sorted(registry.glob("*/resources.json")) + sorted(registry.glob("*.json"))
    candidates += sorted(root.glob("*/*/.aos/resources-*.json"))
    seen = set()
    failures = 0
    for path in candidates:
        lease = None
        try:
            anchor = registry if path.is_relative_to(registry) else root
            relative_path = path.relative_to(anchor)
            if path.is_symlink() or any((anchor / p).is_symlink() for p in relative_path.parents):
                print(f"cleanup skipped symlink record {path}", file=out)
                continue
            data = json.loads(path.read_text())
            worktree = Path(data["worktree"]).resolve()
            run_id = data["run_id"]
            key = (str(worktree), run_id)
            if key in seen:
                continue
            seen.add(key)
            relative = worktree.relative_to(root)
            if len(relative.parts) != 2 or data.get("host") != socket.gethostname():
                print(f"cleanup skipped nonlocal record {run_id}", file=out)
                continue
            task_id = data.get("task_id") or relative.parts[1]
            if task_id != relative.parts[1]:
                raise ValueError("record task/worktree mismatch")
            if data.get("bus_url") not in (None, str(client.base_url).rstrip("/")):
                print(f"cleanup skipped {run_id}: another bus", file=out)
                continue
            if (data.get("closed") and data.get("docker_status") not in ("pending", "unavailable")
                    and not cleanup_pending(data.get("events", []))):
                continue
            try:
                lease = lock_worktree(str(worktree), env.resource_root)
            except BlockingIOError:
                print(f"cleanup skipped {run_id}: workspace in use", file=out)
                continue
            response = client.get(f"/tasks/{task_id}")
            response.raise_for_status()
            task = response.json()
            expected = root / task["repo"] / task["id"]
            run = next((r for r in task.get("runs", []) if r["id"] == run_id), None)
            if expected.resolve() != worktree or run is None:
                raise ValueError("bus does not confirm resource owner")
            if run["status"] in ("claimed", "running"):
                print(f"cleanup skipped {run_id}: run is live", file=out)
                continue
            if not data.get("bus_url") and (not run.get("worktree")
                                             or Path(run["worktree"]).resolve() != worktree):
                raise ValueError("legacy record lacks confirmed worktree")
            if not data.get("closed") and data.get("runner_pid"):
                try:
                    owner = psutil.Process(data["runner_pid"])
                    if (owner.create_time() == data.get("runner_created") and owner.is_running()
                            and owner.status() != psutil.STATUS_ZOMBIE):
                        print(f"cleanup skipped {run_id}: runner is still alive", file=out)
                        continue
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    # Gone, or the PID now belongs to a process this user cannot inspect:
                    # either way it is not the runner that wrote the record.
                    pass
            previous = RunResources(run_id, str(worktree), dict(environ) | {"AOS_RESOURCE_ROOT": env.resource_root})
            previous.task_id = task_id
            previous.bus_url = str(client.base_url).rstrip("/")
            previous.started = data["started"]
            # Legacy registries remain usable; migrate the result to the host registry.
            previous.record_path.parent.mkdir(parents=True, exist_ok=True)
            previous.import_registry(path.with_suffix(".processes.jsonl"))
            previous.restore_docker(data)
            summary = previous.close([], 0)
            print(f"cleanup {run_id}\n{summary}", file=out)
            if previous.docker_status in ("pending", "unavailable") or cleanup_pending(previous.events):
                failures += 1
        except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError, psutil.Error) as exc:
            failures += 1
            print(f"cleanup failed for {path.name}: {type(exc).__name__}: {exc}", file=err)
        finally:
            if lease is not None:
                lease.close()
    return 1 if failures else 0
