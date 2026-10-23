"""H2/H3 bus contracts: proposal identity, family state, and combined submission."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agent_os.api import create_app
from agent_os.policy import Default
from agent_os.store import PROPOSALS_OPEN_MAX, Store
from agent_os.workflow import Workflow, execution_snapshot
from test_proposals import live_run, propose
from test_tasks_api import authorize, claim, create_task, ready


@pytest.fixture
def client(settings, store, telegram):
    client = TestClient(create_app(settings, store=store, telegram=telegram, background=False))
    authorize(client)
    return client


def parent_run(client):
    parent = create_task(client)
    response = client.patch(f"/tasks/{parent['id']}", json={"execution": "interactive"})
    assert response.status_code == 200, response.text
    run = live_run(client, parent["id"])
    return parent, run


def child_task(client, parent, run, **fields):
    response = propose(client, parent["id"], run, **fields)
    assert response.status_code == 200, response.text
    return response.json()


def finish(client, run, status="review", commit="b" * 40, report="child result"):
    response = client.post(f"/runs/{run['id']}/result", json={
        "claim_token": run["claim_token"], "status": status, "report": report,
        "result_commit": commit, "verdict": "passed" if status == "review" else "failed"})
    assert response.status_code == 200, response.text
    return response.json()


def family(client, run, **fields):
    return client.post(f"/runs/{run['id']}/family", json={"claim_token": run["claim_token"], **fields})


def import_result(client, parent_run, child, run, commit="b" * 40):
    return client.post(f"/runs/{parent_run['id']}/imports", json={
        "claim_token": parent_run["claim_token"], "task_id": child["id"], "run_id": run["id"], "commit": commit})


def imported(client, parent_run, child, run, commit="b" * 40):
    response = import_result(client, parent_run, child, run, commit)
    assert response.status_code == 200, response.text
    response = client.post(f"/runs/{parent_run['id']}/imports/{child['id']}/state", json={
        "run_id": run["id"], "commit": commit, "state": "imported", "ref": f"refs/aos/imports/{child['id']}"})
    assert response.status_code == 200, response.text
    return {"task_id": child["id"], "run_id": run["id"], "commit": commit}


def submit(client, run, children):
    return client.post(f"/runs/{run['id']}/submission", json={
        "claim_token": run["claim_token"], "commit": "c" * 40, "report": "assembled", "children": children})


def test_repeated_proposal_returns_the_same_child_and_the_family_read_lists_it(client, store):
    parent, run = parent_run(client)
    del client.headers["Authorization"]
    first = propose(client, parent["id"], run, request_id="child-1")
    assert first.status_code == 200, first.text
    child = first.json()
    assert child["repeated"] is False
    assert child["proposal_key"] == "child-1" and child["base_commit"] == "a" * 40
    repeat = propose(client, parent["id"], run, request_id="child-1")
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["id"] == child["id"] and repeat.json()["repeated"] is True
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM tasks WHERE proposed_by=?", (parent["id"],)).fetchone()[0] == 1
    records = [json.loads(line) for line in store.runs_path.read_text().splitlines()]
    assert sum(r["outcome"] == "task:proposed" for r in records) == 1
    response = family(client, run)
    assert response.status_code == 200, response.text
    listed = response.json()["children"]
    assert len(listed) == 1
    assert {key: listed[0][key] for key in ("id", "waiting", "proposal_key", "base_commit", "attempt", "run")} == {
        "id": child["id"], "waiting": "authorization", "proposal_key": "child-1", "base_commit": "a" * 40,
        "attempt": 0, "run": None}
    assert client.get(f"/tasks/{parent['id']}/family").status_code == 401
    authorize(client)
    assert client.get(f"/tasks/{parent['id']}/family").json()["children"] == listed
    _, sibling = parent_run(client)
    assert family(client, {**run, "claim_token": sibling["claim_token"]}).status_code in (403, 409)


def test_proposal_identity_is_keyed_on_the_parent_task_and_refuses_the_claim_token(client, store):
    parent, run = parent_run(client)
    child = child_task(client, parent, run, request_id="child-1")
    finish(client, run)
    assert client.post(f"/tasks/{parent['id']}/requeue", json={"to": "ready"}).status_code == 200
    second = claim(client, parent["id"]).json()
    repeated = propose(client, parent["id"], second, request_id="child-1")
    assert repeated.status_code == 200 and repeated.json()["id"] == child["id"] and repeated.json()["repeated"]
    refused = propose(client, parent["id"], second, request_id=second["claim_token"])
    assert refused.status_code in (409, 422) and "text cannot contain this run's claim token" in refused.text
    assert propose(client, parent["id"], second, request_id="bad identity").status_code == 422
    for i in range(PROPOSALS_OPEN_MAX - 1):
        child_task(client, parent, second, request_id=f"more-{i}")
    assert propose(client, parent["id"], second, request_id="child-1").json()["repeated"] is True
    assert propose(client, parent["id"], second).status_code == 409
    with store.connection() as db:
        db.execute("UPDATE tasks SET repo='foreign' WHERE id=?", (child["id"],))
    refused = propose(client, parent["id"], second, request_id="child-1")
    assert refused.status_code == 409 and "another repository" in refused.text


def test_proposal_base_must_be_an_exported_candidate(client):
    parent, run = parent_run(client)
    assert child_task(client, parent, run)["base_commit"] == "a" * 40
    refused = propose(client, parent["id"], run, base="b" * 40)
    assert refused.status_code == 422 and "is not an exported candidate" in refused.text
    response = client.post(f"/runs/{run['id']}/candidates", json={"claim_token": run["claim_token"], "commit": "b" * 40})
    assert response.status_code == 200, response.text
    assert propose(client, parent["id"], run, base="b" * 40).status_code == 422
    assert client.post(f"/runs/{run['id']}/candidates/{'b' * 40}/state", json={"state": "exported"}).status_code == 200
    assert child_task(client, parent, run, base="b" * 40)["base_commit"] == "b" * 40
    unstarted = create_task(client, repo="other")
    ready(client, unstarted["id"])
    unstarted_run = claim(client, unstarted["id"]).json()
    assert child_task(client, unstarted, unstarted_run)["base_commit"] is None


def test_candidate_request_refusals_and_repeat(client, store):
    _, run = parent_run(client)
    path = f"/runs/{run['id']}/candidates"
    body = {"claim_token": run["claim_token"], "commit": "a" * 40}
    refused = client.post(path, json=body)
    assert refused.status_code == 422 and "nothing to export" in refused.text
    body["commit"] = "b" * 40
    del client.headers["Authorization"]
    first = client.post(path, json=body)
    assert first.status_code == 200 and first.json()["state"] == "requested" and not first.json()["repeated"]
    assert client.post(path, json=body).json()["repeated"] is True
    state_path = f"{path}/{'b' * 40}/state"
    assert client.post(state_path, json={"state": "exported"}).status_code == 401
    authorize(client)
    for state in ("exported", "failed"):
        response = client.post(state_path, json={"state": state, "ref": "refs/aos/test", "reason": "test"})
        assert response.status_code == 200 and response.json()["state"] == state
        assert response.json()["ref"] == "refs/aos/test" and response.json()["reason"] == "test"
        assert response.json()["updated_at"]
    assert client.post(f"{path}/{'c' * 40}/state", json={"state": "failed"}).status_code == 422
    assert client.post(path, json={**body, "claim_token": "wrong"}).status_code == 409
    headless = create_task(client, repo="headless")
    other = live_run(client, headless["id"])
    refused = client.post(f"/runs/{other['id']}/candidates", json={"claim_token": other["claim_token"], "commit": "b" * 40})
    assert refused.status_code == 409 and "not interactive" in refused.text
    finish(client, run)
    assert client.post(path, json=body).status_code == 409
    with pytest.raises(ValueError, match="commit must"):
        store.request_candidate(run["id"], run["claim_token"], commit="bad")


def test_capacity_from_the_dispatcher_publish(client, store):
    parent, run = parent_run(client)
    cap = family(client, run).json()["capacity"]
    assert cap["parallel_children"] == 0 and "host capacity unknown" in cap["blocker"] and cap["note"] is None
    path = "/hosts/w1/capacity"
    del client.headers["Authorization"]
    assert client.post(path, json={"max_runs": 3, "live": 1}).status_code == 401
    authorize(client)
    published = client.post(path, json={"max_runs": 3, "live": 1})
    assert published.status_code == 200 and published.json()["at"]
    cap = family(client, run).json()["capacity"]
    assert (cap["repo_live"], cap["repo_cap"], cap["host_live"], cap["host_cap"], cap["parallel_children"]) == (1, 3, 1, 3, 2)
    now = datetime.now(timezone.utc)
    store.publish_capacity("w1", max_runs=3, live=1, now=now - timedelta(seconds=601))
    assert "unknown" in store.capacity(parent["repo"], "w1", max_live_per_repo=3, now=now)["blocker"]
    store.publish_capacity("w1", max_runs=1, live=1, now=now)
    cap = store.capacity(parent["repo"], "w1", max_live_per_repo=3, now=now)
    assert cap["parallel_children"] == 0 and "host w1 cap 1 is filled (live 1)" in cap["blocker"]
    store.publish_capacity("w1", max_runs=3, live=1, now=now)
    cap = store.capacity(parent["repo"], "w1", max_live_per_repo=1, now=now)
    assert cap["parallel_children"] == 0 and "repository repo-a cap 1 is filled" in cap["blocker"]
    assert client.post(path, json={"max_runs": 0, "live": -1}).status_code == 422


def test_family_read_lists_states_questions_notices_links_and_stale_imports(client, store):
    parent, run = parent_run(client)
    children = [child_task(client, parent, run, request_id=f"child-{i}") for i in range(5)]
    ready(client, children[1]["id"])
    reviewed = live_run(client, children[3]["id"])
    finish(client, reviewed, report="x" * 17000)
    store.annotate_run(reviewed["id"], pr_number=7, pr_url="https://git.test/pull/7", pr_head_sha="b" * 40)
    with store.connection() as db:
        db.execute("UPDATE task_runs SET remote=?,merge_commit=? WHERE id=?", ("origin", "d" * 40, reviewed["id"]))
    failed = live_run(client, children[4]["id"])
    finish(client, failed, status="failed")
    running = live_run(client, children[2]["id"])
    response = client.post(f"/tasks/{children[2]['id']}/intents", json={
        "run_id": running["id"], "claim_token": running["claim_token"], "to_task_id": parent["id"], "body": "q" * 250})
    assert response.status_code == 200, response.text
    imported(client, run, children[3], reviewed)
    assert family(client, run).json()["children"][3]["import"]["stale"] is False
    assert client.post(f"/tasks/{children[3]['id']}/requeue", json={"to": "ready"}).status_code == 200
    newer = claim(client, children[3]["id"]).json()
    # One live sibling is enough: finish the newer attempt before the listing.
    store.start_run(newer["id"], newer["claim_token"], base_commit="a" * 40, branch="b", worktree="/x")
    finish(client, newer, report="y" * 17000)
    with store.connection() as db:
        db.execute("UPDATE task_runs SET remote=?,pr_url=?,merge_commit=?,error=? WHERE id=?",
                   ("origin", "https://git.test/pull/8", "d" * 40, "detail error", newer["id"]))
    response = family(client, run)
    assert response.status_code == 200, response.text
    listed = response.json()["children"]
    assert [c["status"] for c in listed] == ["backlog", "ready", "running", "review", "failed"]
    assert [c["waiting"] for c in listed[:3]] == ["authorization", "capacity", "none"]
    assert len(listed[2]["questions"]) == 1 and len(listed[2]["questions"][0]["body"]) == 200
    assert listed[3]["notices"] == 2 and listed[3]["attempt"] == 2 and listed[3]["import"]["stale"] is True
    assert len(listed[3]["run"]["report"]) == 400
    assert listed[3]["run"]["pr_url"] == "https://git.test/pull/8" and listed[3]["run"]["remote"] == "origin"
    assert listed[3]["run"]["merge_commit"] == "d" * 40
    detail = family(client, run, task_id=children[3]["id"]).json()["children"]
    assert len(detail) == 1 and len(detail[0]["run"]["report"]) == 16000 and detail[0]["run"]["error"] == "detail error"
    assert client.get(f"/tasks/{parent['id']}/family", params={"child": children[3]["id"]}).json()["children"] == detail
    assert family(client, run, task_id=parent["id"]).status_code == 422


def test_family_read_hides_and_names_a_foreign_repository_child(client, store):
    parent, run = parent_run(client)
    child = child_task(client, parent, run)
    with store.connection() as db:
        db.execute("UPDATE tasks SET repo='foreign' WHERE id=?", (child["id"],))
    result = family(client, run).json()
    assert result["parent"]["foreign_children"] == [child["id"]] and result["children"] == []
    assert family(client, run, task_id=child["id"]).status_code == 422
    no_run = create_task(client, repo="empty")
    result = client.get(f"/tasks/{no_run['id']}/family").json()
    assert result["parent"]["run"] is None and result["capacity"] is None


def test_edit_task_refuses_to_move_a_family_member(client):
    parent, run = parent_run(client)
    child = child_task(client, parent, run)
    finish(client, run, status="failed")
    for task in (parent, child):
        response = client.patch(f"/tasks/{task['id']}", json={"repo": "elsewhere"})
        assert response.status_code == 409 and "cannot move a task that belongs to a family" in response.text
    unrelated = create_task(client)
    assert client.patch(f"/tasks/{unrelated['id']}", json={"repo": "elsewhere"}).json()["repo"] == "elsewhere"


def test_cascade_on_cancel_hold_and_lease_sweep(client, store):
    parent, run = parent_run(client)
    children = [child_task(client, parent, run) for _ in range(5)]
    ready(client, children[1]["id"])
    live = live_run(client, children[2]["id"])
    grandchild = child_task(client, children[2], live)
    terminal_grandchild = None
    for child in children[3:]:
        child_run = live_run(client, child["id"])
        if child == children[3]:
            terminal_grandchild = child_task(client, child, child_run)
        finish(client, child_run)
    store.complete_task(children[4]["id"])
    unrelated = create_task(client, repo="unrelated")
    response = client.post(f"/tasks/{parent['id']}/cancel")
    assert response.status_code == 200, response.text
    moved = {c["id"] for c in children[:3]} | {grandchild["id"], terminal_grandchild["id"]}
    assert set(response.json()["cascade"]) == moved
    assert all(store.get_task(t)["status"] == "cancelled" for t in moved)
    assert store.get_task(children[3]["id"])["status"] == "review"
    assert store.get_task(children[4]["id"])["status"] == "done"
    assert store.get_run(live["id"])["status"] == "cancelled"
    assert store.get_task(unrelated["id"])["status"] == "backlog"
    events = store.read_run_events(live["id"])["events"]
    assert events[-1]["payload"]["status"] == "cancelled"
    records = [json.loads(line) for line in store.runs_path.read_text().splitlines()]
    records = [r for r in records if r["outcome"] == "task:cascade"]
    assert {r["receipt"]["task_id"] for r in records} == moved
    assert all(r["receipt"]["parent_id"] == parent["id"] and r["receipt"]["mode"] == "cancel" for r in records)
    for status in ("failed", "blocked"):
        p, r = parent_run(client)
        pending = child_task(client, p, r)
        ready(client, pending["id"])
        active = child_task(client, p, r)
        ar = live_run(client, active["id"])
        finish(client, r, status=status)
        assert store.get_task(pending["id"])["status"] == "backlog"
        assert store.get_run(ar["id"])["status"] == "cancelled"
    p, r = parent_run(client)
    c = child_task(client, p, r)
    cr = live_run(client, c["id"])
    now = datetime.now(timezone.utc)
    with store.connection() as db:
        db.execute("UPDATE task_runs SET lease_until=? WHERE id IN (?,?)", ((now - timedelta(seconds=1)).isoformat(), r["id"], cr["id"]))
    assert store.expire_task_leases(now=now) == [r["id"]]
    assert store.get_run(r["id"])["status"] == "blocked" and store.get_run(cr["id"])["status"] == "cancelled"


def test_import_request_validates_family_run_status_and_commit(client, store):
    parent, run = parent_run(client)
    unrelated = create_task(client)
    unrelated_run = live_run(client, unrelated["id"])
    refused = import_result(client, run, unrelated, unrelated_run)
    assert refused.status_code == 409 and "is not a child" in refused.text
    finish(client, unrelated_run)
    child = child_task(client, parent, run)
    child_run = live_run(client, child["id"])
    refused = import_result(client, run, child, child_run)
    assert refused.status_code == 409 and "not a successful result" in refused.text
    finish(client, child_run)
    refused = import_result(client, run, child, child_run, "e" * 40)
    assert refused.status_code == 409 and "actual result differs" in refused.text
    with store.connection() as db:
        db.execute("UPDATE tasks SET repo='foreign' WHERE id=?", (child["id"],))
    assert "another repository" in import_result(client, run, child, child_run).text
    with store.connection() as db:
        db.execute("UPDATE tasks SET repo='repo-a' WHERE id=?", (child["id"],))
    first = import_result(client, run, child, child_run)
    assert first.status_code == 200 and not first.json()["repeated"]
    assert import_result(client, run, child, child_run).json()["repeated"] is True
    client.post(f"/tasks/{child['id']}/requeue", json={"to": "ready"})
    newer = claim(client, child["id"]).json()
    assert "stale selection" in import_result(client, run, child, child_run).text
    store.start_run(newer["id"], newer["claim_token"], base_commit="a" * 40, branch="b", worktree="/x")
    finish(client, newer)
    store.complete_task(child["id"])
    response = import_result(client, run, child, newer)
    assert response.status_code == 200 and not response.json()["repeated"] and response.json()["run_id"] == newer["id"]
    assert len(store.get_run(run["id"])["imports"]) == 2
    assert client.post(f"/runs/{run['id']}/imports/{child['id']}/state", json={
        "run_id": newer["id"], "commit": "b" * 40, "state": "failed", "reason": "transfer failed"}).json()["reason"] == "transfer failed"
    with pytest.raises(ValueError):
        store.request_import(run["id"], run["claim_token"], task_id="bad", child_run_id=newer["id"], commit="b" * 40)


def test_submit_run_requires_the_manifest_to_equal_the_imported_selection(client, store):
    parent, run = parent_run(client)
    child = child_task(client, parent, run)
    child_run = live_run(client, child["id"])
    finish(client, child_run)
    store.complete_task(child["id"])
    entry = imported(client, run, child, child_run)
    refused = submit(client, run, [])
    assert refused.status_code == 409 and f"missing {child['id']}" in refused.text
    extra = {"task_id": "AO-999", "run_id": "AO-999-r1", "commit": "b" * 40}
    refused = submit(client, run, [entry, extra])
    assert refused.status_code == 409 and "extra AO-999" in refused.text
    refused = submit(client, run, [entry, entry])
    assert refused.status_code == 409 and "manifest lists a child twice" in refused.text
    pending = child_task(client, parent, run)
    ready(client, pending["id"])
    refused = submit(client, run, [entry])
    assert refused.status_code == 409 and f"child {pending['id']} is still ready" in refused.text
    client.post(f"/tasks/{pending['id']}/cancel")
    accepted = submit(client, run, [entry])
    assert accepted.status_code == 200, accepted.text
    assert submit(client, run, [entry]).json()["repeated"] is True
    # Repeat validation must still check freshness, even after a submission exists.
    with store.connection() as db:
        db.execute("UPDATE task_runs SET result_commit=? WHERE id=?", ("e" * 40, child_run["id"]))
    refused = submit(client, run, [entry])
    assert refused.status_code == 409 and "stale selection" in refused.text


def test_submit_run_accepts_the_re_selected_newer_run_after_a_requeue(client, store):
    parent, run = parent_run(client)
    child = child_task(client, parent, run)
    first = live_run(client, child["id"])
    finish(client, first)
    old_entry = imported(client, run, child, first)
    assert client.post(f"/tasks/{child['id']}/requeue", json={"to": "ready"}).status_code == 200
    newer = claim(client, child["id"]).json()
    store.start_run(newer["id"], newer["claim_token"], base_commit="a" * 40, branch="b", worktree="/x")
    finish(client, newer, commit="c" * 40)
    entry = imported(client, run, child, newer, "c" * 40)
    accepted = submit(client, run, [entry])
    assert accepted.status_code == 200, accepted.text
    history = store.get_run(run["id"])["imports"]
    assert history[0]["state"] == "superseded" and history[0]["superseded_by"] == newer["id"]
    assert history[0]["updated_at"] and history[1]["state"] == "imported"
    block = family(client, run).json()["children"][0]["import"]
    assert block["run_id"] == newer["id"] and block["stale"] is False and block["superseded"] == 1
    response = client.post(f"/runs/{run['id']}/integration", json={
        "order": [child["id"]], "integrated_at": {child["id"]: "d" * 40}})
    assert response.status_code == 200, response.text
    history = store.get_run(run["id"])["imports"]
    assert "integrated_at" not in history[0] and history[1]["integrated_at"] == "d" * 40
    refused = submit(client, run, [old_entry])
    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        f"manifest differs from the imported selection: missing {child['id']}; extra {child['id']}")
    refused = submit(client, run, [old_entry, entry])
    assert refused.status_code == 409 and "manifest lists a child twice" in refused.text


def test_family_import_block_reads_the_newest_entry_and_names_the_superseded_one(client, store):
    parent, run = parent_run(client)
    child = child_task(client, parent, run)
    first = live_run(client, child["id"])
    finish(client, first)
    imported(client, run, child, first)
    store.requeue_task(child["id"], "ready")
    newer = claim(client, child["id"]).json()
    store.start_run(newer["id"], newer["claim_token"], base_commit="a" * 40, branch="b", worktree="/x")
    finish(client, newer, commit="c" * 40)
    assert import_result(client, run, child, newer, "c" * 40).status_code == 200
    path = f"/runs/{run['id']}/imports/{child['id']}/state"
    body = {"run_id": newer["id"], "commit": "c" * 40, "state": "failed", "reason": "transfer failed"}
    assert client.post(path, json=body).status_code == 200
    history = store.get_run(run["id"])["imports"]
    assert [e["state"] for e in history] == ["imported", "failed"]
    block = client.get(f"/tasks/{parent['id']}/family").json()["children"][0]["import"]
    assert block["run_id"] == newer["id"] and block["state"] == "failed" and block["superseded"] == 0
    assert block["stale"] is False and block["requeues"] == 1
    imported(client, run, child, newer, "c" * 40)
    block = client.get(f"/tasks/{parent['id']}/family").json()["children"][0]["import"]
    assert block["run_id"] == newer["id"] and block["commit"] == "c" * 40
    assert block["state"] == "imported" and block["stale"] is False
    assert block["superseded"] == 1 and block["requeues"] == 1
    history = store.get_run(run["id"])["imports"]
    assert [e["state"] for e in history] == ["superseded", "failed", "imported"]
    assert history[0]["superseded_by"] == newer["id"]


def test_import_state_transitions_refuse_a_failed_post_on_an_imported_entry(client, store):
    parent, run = parent_run(client)
    child = child_task(client, parent, run)
    first = live_run(client, child["id"])
    finish(client, first)
    imported(client, run, child, first)
    original = store.get_run(run["id"])["imports"][0]
    path = f"/runs/{run['id']}/imports/{child['id']}/state"
    body = {"run_id": first["id"], "commit": "b" * 40, "state": "failed"}
    refused = client.post(path, json=body)
    assert 400 <= refused.status_code < 500 and f"import {child['id']} {first['id']} {'b' * 12} is already imported" in refused.text
    repeated = client.post(path, json={**body, "state": "imported", "ref": "must not replace the ref"})
    assert repeated.status_code == 200 and repeated.json() == original
    assert store.get_run(run["id"])["imports"] == [original]
    store.requeue_task(child["id"], "ready")
    newer = claim(client, child["id"]).json()
    store.start_run(newer["id"], newer["claim_token"], base_commit="a" * 40, branch="b", worktree="/x")
    finish(client, newer, commit="c" * 40)
    entry = imported(client, run, child, newer, "c" * 40)
    history = store.get_run(run["id"])["imports"]
    for state in ("failed", "imported"):
        refused = client.post(path, json={**body, "state": state})
        assert 400 <= refused.status_code < 500 and "already superseded" in refused.text
    assert client.post(path, json={**body, "state": "superseded"}).status_code == 422
    assert store.get_run(run["id"])["imports"] == history
    assert submit(client, run, [entry]).status_code == 200


def test_requeue_without_a_new_run_makes_the_selection_stale_until_a_re_import(client, store):
    parent, run = parent_run(client)
    child = child_task(client, parent, run)
    first = live_run(client, child["id"])
    finish(client, first)
    entry = imported(client, run, child, first)
    response = client.post(f"/tasks/{child['id']}/requeue", json={"to": "ready"})
    assert response.status_code == 200 and response.json()["requeues"] == 1
    block = family(client, run).json()["children"][0]["import"]
    assert block["stale"] is True and block["requeues"] == 0
    for cancel in (False, True):
        if cancel:
            assert client.post(f"/tasks/{child['id']}/cancel").status_code == 200
        refused = submit(client, run, [entry])
        assert refused.status_code == 409 and f"stale selection: {child['id']} was requeued since the import" in refused.text
    request = import_result(client, run, child, first)
    assert request.status_code == 200 and request.json()["repeated"] is False
    assert request.json()["requeues"] == 1
    assert imported(client, run, child, first) == entry
    history = store.get_run(run["id"])["imports"]
    assert [e["state"] for e in history] == ["superseded", "imported"]
    assert history[0]["superseded_by"] == first["id"]
    assert [e["requeues"] for e in history] == [0, 1]
    assert family(client, run).json()["children"][0]["import"]["stale"] is False
    assert submit(client, run, [entry]).status_code == 200


def test_requeues_is_stored_and_returned(client, store):
    task = create_task(client)
    # Simulate a populated database from before the additions-map migration.
    with store.connection() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
        if "requeues" in columns:
            db.execute("ALTER TABLE tasks DROP COLUMN requeues")
    migrated = Store(store.data_dir)
    with migrated.connection() as db:
        column = next((row for row in db.execute("PRAGMA table_info(tasks)") if row["name"] == "requeues"), None)
        assert column is not None
        assert column["type"] == "INTEGER" and column["notnull"] == 1 and column["dflt_value"] == "0"
    assert migrated.get_task(task["id"])["requeues"] == 0
    assert client.get(f"/tasks/{task['id']}").json()["requeues"] == 0
    for generation, target in enumerate(("ready", "backlog"), 1):
        assert migrated.requeue_task(task["id"], target)["requeues"] == generation
        assert client.get(f"/tasks/{task['id']}").json()["requeues"] == generation
    assert Store(store.data_dir).get_task(task["id"])["requeues"] == 2


def test_composed_acceptance_source_claims_through_the_real_route(client):
    task = create_task(client, acceptance=["task-check"])
    client.patch(f"/tasks/{task['id']}", json={"execution": "interactive"})
    task = ready(client, task["id"])
    default = Default("local", "main", "a" * 40, "main@aaaaaaaaaaaa")
    workflow = Workflow("full-suite", (), None, "b" * 64, default.ref)
    snapshot = execution_snapshot(task, workflow, default)
    assert snapshot["effective_acceptance"] == ["task-check", "full-suite"]
    assert snapshot["acceptance_source"] == "task+workflow.test_command"
    response = claim(client, task["id"], **{**snapshot, "acceptance_source": "task"})
    assert response.status_code == 409
    response = claim(client, task["id"], **{**snapshot, "effective_acceptance": ["wrong", "full-suite"]})
    assert response.status_code == 409 and "followed by one workflow command" in response.text
    assert claim(client, task["id"], **{**snapshot, "workflow_commit": None}).status_code == 422
    response = claim(client, task["id"], **snapshot)
    assert response.status_code == 200, response.text
    assert all(response.json()[key] == value for key, value in snapshot.items())


def test_execution_snapshot_composes_the_task_acceptance_with_the_test_command_for_interactive_tasks():
    default = Default("local", "main", "a" * 40, "main@aaaaaaaaaaaa")
    workflow = Workflow("full-suite", ("other-check",), None, "b" * 64, default.ref)
    task = {"acceptance": ["task-check"], "execution": "interactive"}
    assert execution_snapshot(task, workflow, default)["effective_acceptance"] == ["task-check", "full-suite"]
    assert task["acceptance"] == ["task-check"]
    for task in ({"acceptance": ["task-check"], "execution": "headless"}, {"acceptance": ["full-suite"], "execution": "interactive"}):
        result = execution_snapshot(task, workflow, default)
        assert result["acceptance_source"] == "task" and result["effective_acceptance"] == task["acceptance"]


def test_integration_record_sets_order_and_integrated_at(client, store):
    parent, run = parent_run(client)
    path = f"/runs/{run['id']}/integration"
    body = {"order": [], "integrated_at": {}}
    assert client.post(path, json=body).status_code in (409, 422)
    children = [child_task(client, parent, run) for _ in range(2)]
    manifest = []
    for child in children:
        cr = live_run(client, child["id"])
        finish(client, cr)
        manifest.append(imported(client, run, child, cr))
    assert submit(client, run, manifest).status_code == 200
    body = {"order": [c["id"] for c in reversed(children)], "integrated_at": {c["id"]: sha * 40 for c, sha in zip(children, ("d", "e"))}}
    del client.headers["Authorization"]
    assert client.post(path, json=body).status_code == 401
    authorize(client)
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["integration_order"] == body["order"]
    recorded = store.get_run(run["id"])
    assert recorded["submission"]["integration_order"] == body["order"]
    assert {e["task_id"]: e["integrated_at"] for e in recorded["imports"]} == body["integrated_at"]


@pytest.mark.parametrize("surface", ["candidates", "imports", "family"])
def test_run_bound_surfaces_check_the_token_liveness_and_mode(client, surface):
    parent, run = parent_run(client)
    path = f"/runs/{run['id']}/{surface}"
    fields = {"commit": "b" * 40} if surface == "candidates" else {}
    if surface == "imports":
        fields = {"task_id": "AO-999", "run_id": "AO-999-r1", "commit": "b" * 40}
    assert client.post(path, json={"claim_token": "wrong", **fields}).status_code == 409
    finish(client, run)
    refused = client.post(path, json={"claim_token": run["claim_token"], **fields})
    assert refused.status_code == 409 and "run is not live (review)" in refused.text
    headless = create_task(client)
    hr = live_run(client, headless["id"])
    refused = client.post(f"/runs/{hr['id']}/{surface}", json={"claim_token": hr["claim_token"], **fields})
    if surface == "family":
        # D5's original route contract specifies this 403; the shared helper raises TransitionError.
        assert refused.status_code == 403 and "family read is interactive-only" in refused.text
    else:
        assert refused.status_code == 409 and "run is not interactive" in refused.text


def test_import_and_submission_recheck_the_newest_attempt_and_repository(client, store):
    parent, run = parent_run(client)
    child = child_task(client, parent, run)
    missing = import_result(client, run, child, {"id": f"{child['id']}-r1"})
    assert missing.status_code == 409 and "has no run" in missing.text
    missing = import_result(client, run, {"id": "AO-999"}, {"id": "AO-999-r1"})
    assert missing.status_code == 404
    cr = live_run(client, child["id"])
    finish(client, cr)
    entry = imported(client, run, child, cr)
    with store.connection() as db:
        db.execute("UPDATE tasks SET repo='foreign' WHERE id=?", (child["id"],))
    refused = submit(client, run, [entry])
    assert refused.status_code == 409 and "another repository" in refused.text
    with store.connection() as db:
        db.execute("UPDATE tasks SET repo='repo-a',proposed_by=NULL WHERE id=?", (child["id"],))
    refused = submit(client, run, [entry])
    assert refused.status_code == 409 and "is not a child" in refused.text
    with store.connection() as db:
        db.execute("UPDATE tasks SET proposed_by=? WHERE id=?", (parent["id"], child["id"]))
    client.post(f"/tasks/{child['id']}/requeue", json={"to": "ready"})
    newer = claim(client, child["id"]).json()
    refused = submit(client, run, [entry])
    assert refused.status_code == 409 and f"newer run {newer['id']}" in refused.text
    store.start_run(newer["id"], newer["claim_token"], base_commit="a" * 40, branch="b", worktree="/x")
    finish(client, newer)  # same commit, different run: an explicit import is required
    refused = submit(client, run, [{**entry, "run_id": newer["id"]}])
    assert refused.status_code == 409 and "manifest differs" in refused.text
    # Imported entries are retired only when a newer import succeeds.
    path = f"/runs/{run['id']}/imports/{child['id']}/state"
    body = {"run_id": cr["id"], "commit": "b" * 40, "state": "failed", "reason": "superseded"}
    del client.headers["Authorization"]
    assert client.post(path, json=body).status_code == 401
    authorize(client)
    refused = client.post(path, json=body)
    assert refused.status_code == 422 and "already imported" in refused.text
    entry = imported(client, run, child, newer)
    assert submit(client, run, [entry]).status_code == 200


def test_proposal_identity_serializes_concurrent_requests(client, store):
    from concurrent.futures import ThreadPoolExecutor

    parent, run = parent_run(client)

    def request(_):
        return store.propose_task(parent["id"], run["id"], run["claim_token"], title="child", brief="work",
                                  acceptance=["true"], request_id="concurrent")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, range(2)))
    assert results[0]["id"] == results[1]["id"]
    assert sorted(r["repeated"] for r in results) == [False, True]
    records = [json.loads(line) for line in store.runs_path.read_text().splitlines()]
    assert sum(r["outcome"] == "task:proposed" for r in records) == 1
