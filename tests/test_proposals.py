"""Agents that talk, U7: a coordinator run proposes tasks in its own repository.

The run token authorises; the proposal inherits repo, harness, tier and host; it waits in
the backlog unless the repository's switch starts proposals ready; the cap bounds a loop."""
from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_os.api import create_app
from agent_os.config import Settings
from agent_os.store import PROPOSALS_OPEN_MAX, Store
from agent_os.telegram import Telegram
from test_tasks_api import authorize, claim, create_task, ready


@pytest.fixture
def client(settings: Settings, store: Store, telegram: Telegram) -> TestClient:
    client = TestClient(create_app(settings, store=store, telegram=telegram, background=False))
    authorize(client)
    return client


def live_run(client: TestClient, task_id: str) -> dict[str, Any]:
    ready(client, task_id)
    run = claim(client, task_id).json()
    started = client.post(f"/runs/{run['id']}/start", json={
        "claim_token": run["claim_token"], "base_commit": "a" * 40, "branch": f"ao/{task_id}", "worktree": "/x"})
    assert started.status_code == 200, started.text
    return run


def propose(client: TestClient, task_id: str, run: dict[str, Any], **overrides: Any) -> httpx.Response:
    body: dict[str, Any] = {"run_id": run["id"], "claim_token": run["claim_token"],
                            "title": "Child task", "brief": "do the child thing", "acceptance": ["true"]}
    body.update(overrides)
    return client.post(f"/tasks/{task_id}/proposals", json=body)


def test_live_run_proposes_a_backlog_task_in_its_own_repo(client: TestClient) -> None:
    parent = create_task(client, repo="repo-a", tier=2, host="mac")
    run = live_run(client, parent["id"])
    del client.headers["Authorization"]  # a run-token route: the bearer is not needed
    response = propose(client, parent["id"], run, constraints="stay in src/")
    assert response.status_code == 200, response.text
    child = response.json()
    assert child["id"] != parent["id"]
    assert child["proposed_by"] == parent["id"]
    assert child["status"] == "backlog"
    assert (child["repo"], child["harness"], child["tier"], child["host"]) == ("repo-a", "claude", 2, "mac")
    assert child["title"] == "Child task" and child["brief"] == "do the child thing"
    assert child["acceptance"] == ["true"] and child["constraints"] == "stay in src/"
    authorize(client)
    listed = client.get("/tasks", params={"status": "backlog"}).json()
    assert [t["id"] for t in listed] == [child["id"]]
    shown = client.get(f"/tasks/{child['id']}").json()
    assert shown["proposed_by"] == parent["id"]


def test_proposal_needs_a_live_run_of_the_named_task(client: TestClient) -> None:
    parent = create_task(client)
    other = create_task(client, title="Other")
    run = live_run(client, parent["id"])
    assert propose(client, other["id"], run).status_code == 409        # the run is not this task's
    assert propose(client, parent["id"], run, claim_token="nope").status_code == 409
    assert propose(client, "AO-999", run).status_code in (404, 409)
    ended = client.post(f"/runs/{run['id']}/result", json={
        "claim_token": run["claim_token"], "status": "review", "report": "ok"})
    assert ended.status_code == 200, ended.text
    assert propose(client, parent["id"], run).status_code == 409        # the run is no longer live


def test_proposal_validation(client: TestClient) -> None:
    parent = create_task(client)
    run = live_run(client, parent["id"])
    assert propose(client, parent["id"], run, title="   ").status_code == 422
    assert propose(client, parent["id"], run, brief="").status_code == 422
    assert propose(client, parent["id"], run, acceptance=[]).status_code == 422
    assert propose(client, parent["id"], run, brief="see <agent-request> above").status_code == 422
    assert propose(client, parent["id"], run, title="<operator-message>").status_code == 422
    assert propose(client, parent["id"], run, constraints="agent-overlap here").status_code == 422
    assert propose(client, parent["id"], run, extra="field").status_code == 422
    assert propose(client, parent["id"], run).status_code == 200


def test_proposal_text_carrying_the_runs_own_token_is_refused(client: TestClient) -> None:
    """Lane B B2: title, brief, an acceptance item or constraints holding the run's claim token
    is 422 and creates no task; the same proposal without the token passes."""
    parent = create_task(client)
    run = live_run(client, parent["id"])
    token = run["claim_token"]
    for field, value in (("title", f"Child {token}"), ("brief", f"use {token}"),
                         ("acceptance", ["true", f"echo {token}"]), ("constraints", f"never print {token}")):
        response = propose(client, parent["id"], run, **{field: value})
        assert response.status_code == 422, (field, response.text)
        assert response.json()["detail"] == "text cannot contain this run's claim token", field
    assert client.get("/tasks", params={"status": "backlog"}).json() == []
    assert propose(client, parent["id"], run, constraints="never print the token").status_code == 200


def test_repo_switch_makes_proposals_ready(client: TestClient) -> None:
    put = client.put("/repos/repo-a", json={"default_host": "mac", "auto_ready_proposals": True})
    assert put.status_code == 200, put.text
    assert put.json()["auto_ready_proposals"] is True
    parent = create_task(client, repo="repo-a")
    run = live_run(client, parent["id"])
    child = propose(client, parent["id"], run).json()
    assert child["status"] == "ready"
    # Absent keeps the stored value; the listing shows it; off turns it off again.
    kept = client.put("/repos/repo-a", json={"default_host": "mac"}).json()
    assert kept["auto_ready_proposals"] is True
    assert [r["auto_ready_proposals"] for r in client.get("/repos").json()] == [True]
    off = client.put("/repos/repo-a", json={"default_host": "mac", "auto_ready_proposals": False}).json()
    assert off["auto_ready_proposals"] is False
    assert propose(client, parent["id"], run, title="Second").json()["status"] == "backlog"


def test_new_repo_defaults_to_backlog_proposals(client: TestClient) -> None:
    created = client.put("/repos/repo-b", json={"default_host": "server"}).json()
    assert created["auto_ready_proposals"] is False
    parent = create_task(client, repo="repo-b", host="any")  # repo-b defaults to server; the claim is from mac
    run = live_run(client, parent["id"])
    assert propose(client, parent["id"], run).json()["status"] == "backlog"


def test_open_proposals_per_parent_are_capped(client: TestClient) -> None:
    parent = create_task(client)
    run = live_run(client, parent["id"])
    ids = [propose(client, parent["id"], run, title=f"Child {i}").json()["id"] for i in range(PROPOSALS_OPEN_MAX)]
    refused = propose(client, parent["id"], run, title="One too many")
    assert refused.status_code == 409, refused.text
    assert str(PROPOSALS_OPEN_MAX) in refused.json()["detail"]
    cancelled = client.post(f"/tasks/{ids[0]}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert propose(client, parent["id"], run, title="Fits again").status_code == 200


def test_proposal_is_recorded_in_runs_jsonl(client: TestClient, store: Store, settings: Settings) -> None:
    parent = create_task(client)
    run = live_run(client, parent["id"])
    child = propose(client, parent["id"], run).json()
    lines = [line for line in (settings.BUS_DATA_DIR / "runs.jsonl").read_text().splitlines()
             if '"task:proposed"' in line]
    assert len(lines) == 1
    assert child["id"] in lines[0] and parent["id"] in lines[0] and run["id"] in lines[0]
    assert '"needs_user": true' in lines[0]


# -- lane V: a task may name the task it reviews, in its own repository ----------------------------------------

def test_task_names_the_task_it_reviews_in_its_own_repository(client: TestClient) -> None:
    reviewed = create_task(client, repo="repo-a")
    response = client.post("/tasks", json={"repo": "repo-a", "title": "Review it", "reviews": reviewed["id"]})
    assert response.status_code == 200, response.text
    reviewer = response.json()
    assert reviewer["reviews"] == reviewed["id"]
    assert client.get(f"/tasks/{reviewer['id']}").json()["reviews"] == reviewed["id"]
    assert create_task(client, repo="repo-a")["reviews"] is None
    other = create_task(client, repo="repo-b")
    before = len(client.get("/tasks").json())
    for value, detail in (("AO-999", "reviews: no task AO-999"),
                          (reviewed["id"].lower(), f"reviews: no task {reviewed['id'].lower()}"),
                          (other["id"], f"reviews: task {other['id']} is in repository repo-b, not repo-a")):
        refused = client.post("/tasks", json={"repo": "repo-a", "title": "Review", "reviews": value})
        assert refused.status_code == 422 and refused.json() == {"detail": detail}, refused.text
    assert len(client.get("/tasks").json()) == before
    # Never changed after creation: the edit body has no such field.
    assert client.patch(f"/tasks/{reviewer['id']}", json={"reviews": other["id"]}).status_code == 422
    assert client.get(f"/tasks/{reviewer['id']}").json()["reviews"] == reviewed["id"]


def test_proposal_names_the_task_it_reviews_in_the_parents_repository(client: TestClient) -> None:
    parent = create_task(client, repo="repo-a")
    sibling = create_task(client, repo="repo-a", title="Sibling")
    other = create_task(client, repo="repo-b", title="Elsewhere")
    run = live_run(client, parent["id"])
    for reviewed in (parent["id"], sibling["id"]):  # a coordinator may propose a review of its own task
        child = propose(client, parent["id"], run, title=f"Review {reviewed}", reviews=reviewed)
        assert child.status_code == 200, child.text
        assert child.json()["reviews"] == reviewed and child.json()["proposed_by"] == parent["id"]
    for value, detail in (("AO-999", "reviews: no task AO-999"),
                          (other["id"], f"reviews: task {other['id']} is in repository repo-b, not repo-a")):
        refused = propose(client, parent["id"], run, title="Again", reviews=value)
        assert refused.status_code == 422 and refused.json() == {"detail": detail}, refused.text
    assert propose(client, parent["id"], run, reviews="").status_code == 422
    assert propose(client, parent["id"], run, reviews="A" * 65).status_code == 422
    assert propose(client, parent["id"], run, title="Plain").json()["reviews"] is None
