from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_os import cli
from agent_os.api import create_app
from agent_os.config import Settings
from agent_os.lanes.super_productivity import LANE, SOURCE, SuperProductivityLane, parse_notes
from agent_os.sp_sync import SyncFileError
from agent_os.store import Store
from agent_os.telegram import Telegram
from conftest import FakeHTTP

CODE_CHAT = "-400"
TAGS = {"go": "t-go", "hold": "t-hold", "Hold": "t-hold-cap", "agent-os": "t-agent-os", "freelance_bot": "t-fb", "misc": "t-misc", "aos-in": "t-aos-in"}
PROJECTS = {"p-aos": "AOS", "p-other": "Other"}

NOTES = "\n".join([
    "Make the poller retry once.",
    "",
    "## Context",
    "The poller gives up on the first error.",
    "",
    "## Acceptance",
    "- `uv run pytest -q tests/test_poller.py`",
    "```",
    "make lint",
    "```",
    "2. `` make check ``",
    "",
    "## AOS",
    "harness: codex",
    "- tier: 2",
    "host: server",
    "",
])


def task(task_id: str, title: str, *, notes: str = "", tags: tuple[str, ...] = (), project: str = "p-aos",
         done: bool = False, subtasks: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """The app's task entity plus one entity per subtask title."""
    subs = [{"id": f"{task_id}-sub{i}", "title": sub, "parentId": task_id, "projectId": project, "tagIds": [],
             "subTaskIds": [], "isDone": False} for i, sub in enumerate(subtasks, 1)]
    main = {"id": task_id, "title": title, "notes": notes, "projectId": project, "tagIds": [TAGS[t] for t in tags],
            "subTaskIds": [s["id"] for s in subs], "isDone": done}
    return [main, *subs]


def make_state(*groups: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = [t for group in groups for t in group]
    return {
        "task": {"ids": [t["id"] for t in tasks], "entities": {t["id"]: t for t in tasks}},
        "project": {"ids": list(PROJECTS), "entities": {pid: {"id": pid, "title": title, "taskIds": []}
                                                        for pid, title in PROJECTS.items()}},
        "tag": {"ids": list(TAGS.values()), "entities": {tid: {"id": tid, "title": title, "taskIds": []}
                                                         for title, tid in TAGS.items()}},
    }


class FakeSource:
    def __init__(self) -> None:
        self.state = make_state()
        self.error: Exception | None = None
        self.calls = 0

    def fetch(self) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.state)


@pytest.fixture
def sp_settings(settings: Settings) -> Settings:
    settings.AOS_SP_DROPBOX_APP_KEY = "key"
    settings.AOS_SP_DROPBOX_APP_SECRET = "secret"
    settings.AOS_SP_DROPBOX_REFRESH_TOKEN = "refresh"
    settings.AOS_SP_DROPBOX_PATH = "/Apps/sp"
    settings.AOS_SP_PROJECT = "AOS"
    settings.TELEGRAM_CHAT_CODE = CODE_CHAT
    return settings


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


@pytest.fixture
def sp(store: Store, telegram: Telegram, sp_settings: Settings, source: FakeSource) -> SuperProductivityLane:
    store.set_repo("agent-os", default_host="server")
    store.set_repo("freelance_bot", default_host="any")
    return SuperProductivityLane(store, telegram, sp_settings, fetch=source.fetch)


def only_task(store: Store) -> dict[str, Any]:
    tasks = store.list_tasks()
    assert len(tasks) == 1
    return tasks[0]


def notices(fake: FakeHTTP) -> list[str]:
    return [call[2]["text"] for call in fake.messages(CODE_CHAT)]


# -- creation and mapping --------------------------------------------------------

def test_create_in_backlog_with_source_and_external_id(sp: SuperProductivityLane, store: Store, source: FakeSource,
                                                        fake: FakeHTTP) -> None:
    source.state = make_state(task("t1", "Add retry", notes=NOTES, tags=("agent-os",),
                                   subtasks=("write the test", "wire the retry")))
    outcome = sp.poll()
    created = only_task(store)
    assert created["source"] == SOURCE and created["external_id"] == "t1" and created["status"] == "ready"
    assert created["repo"] == "agent-os" and created["title"] == "Add retry"
    assert created["brief"] == ("Make the poller retry once.\n\n## Context\nThe poller gives up on the first error."
                                "\n\nSubtasks:\n- write the test\n- wire the retry")
    assert created["acceptance"] == ["uv run pytest -q tests/test_poller.py", "make lint", "make check"]
    assert created["tier"] == 2 and created["harness"] == "codex" and created["host"] == "server"
    assert store.task_by_source(SOURCE, "t1")["id"] == created["id"]
    assert store.task_by_source(SOURCE, "t9") is None and store.task_by_source("jira", "t1") is None
    assert store.list_tasks(source=SOURCE) == [created] and store.list_tasks(source="jira") == []
    assert outcome["created"] == [f"{created['id']} Add retry"] and outcome["error"] is None
    assert outcome["released"] == [f"{created['id']} Add retry"]
    assert store.lanes()[-1]["lane"] == LANE and store.lanes()[-1]["last_outcome"] == "ok"
    run = json.loads(store.runs_path.read_text().splitlines()[-1])
    assert run["lane"] == LANE and run["receipt"]["created"] == 1
    assert len(notices(fake)) == 1 and f"created: {created['id']} Add retry" in notices(fake)[0]

    again = sp.poll()
    assert only_task(store)["spec_hash"] == created["spec_hash"] and only_task(store)["title"] == "Add retry"
    assert again["created"] == [] and again["updated"] == [] and len(notices(fake)) == 1


def test_repo_defaults_fill_host_without_injecting_checks(sp: SuperProductivityLane, store: Store, source: FakeSource) -> None:
    source.state = make_state(task("t2", "Plain", notes="Just do it.", tags=("agent-os",)),
                              task("t3", "Bare", tags=("freelance_bot",)))
    sp.poll()
    by_ext = {t["external_id"]: t for t in store.list_tasks()}
    plain, bare = by_ext["t2"], by_ext["t3"]
    assert plain["acceptance"] == [] and plain["host"] == "server"
    assert plain["tier"] == 1 and plain["harness"] == "claude" and plain["brief"] == "Just do it."
    assert plain["status"] == "ready" and bare["status"] == "ready"
    # empty notes: the title is the brief
    assert bare["repo"] == "freelance_bot" and bare["acceptance"] == [] and bare["host"] == "any" and bare["brief"] == "Bare"


def test_notes_sections_are_parsed_and_tier_three_is_ignored(sp: SuperProductivityLane, store: Store,
                                                              source: FakeSource, fake: FakeHTTP) -> None:
    brief, acceptance, aos = parse_notes(NOTES)
    assert brief == "Make the poller retry once.\n\n## Context\nThe poller gives up on the first error."
    assert acceptance == ["uv run pytest -q tests/test_poller.py", "make lint", "make check"]
    assert aos == {"harness": "codex", "tier": "2", "host": "server"}
    assert parse_notes("") == ("", [], {})
    assert parse_notes("## acceptance\n\n   \n## AOS\nnonsense line\n") == ("", [], {})

    source.state = make_state(task("t4", "Tier three", notes="Body.\n\n## AOS\ntier: 3\nharness: magic\nhost: moon\n",
                                   tags=("agent-os", "go")))
    outcome = sp.poll()
    created = only_task(store)
    assert created["tier"] == 1 and created["harness"] == "claude" and created["host"] == "server"
    assert created["status"] == "ready" and created["brief"] == "Body."
    assert outcome["tier3"] == [f"{created['id']} Tier three"]
    assert "tier 3 ignored" in notices(fake)[0]


# -- edits, readiness, cancellation ------------------------------------------------

def test_edits_propagate_before_a_claim_and_not_after(sp: SuperProductivityLane, store: Store, source: FakeSource,
                                                      fake: FakeHTTP, sp_settings: Settings) -> None:
    source.state = make_state(task("t1", "First title", notes="v1\n\n## Acceptance\nmake a", tags=("agent-os",)))
    sp.poll()
    created = only_task(store)
    entity = source.state["task"]["entities"]["t1"]
    entity["title"], entity["notes"] = "Second title", "v2\n\n## Acceptance\nmake b\nmake c"
    outcome = sp.poll()
    edited = only_task(store)
    assert edited["title"] == "Second title" and edited["brief"] == "v2" and edited["acceptance"] == ["make b", "make c"]
    assert edited["spec_hash"] != created["spec_hash"] and outcome["updated"] == [f"{created['id']} Second title"]
    assert len(notices(fake)) == 2 and f"updated: {created['id']} Second title" in notices(fake)[1]

    entity["tagIds"].append("t-go")
    sp.poll()
    assert only_task(store)["status"] == "ready"
    run = store.claim_task(edited["id"], worker="w", worker_host="server", lease_seconds=600,
                           expected_spec_hash=edited["spec_hash"], effective_acceptance=edited["acceptance"],
                           acceptance_source="task")
    assert run["status"] == "claimed"
    entity["title"] = "Third title"
    entity["tagIds"].remove("t-go")
    entity["isDone"] = True
    outcome = sp.poll()
    after = only_task(store)
    assert after["title"] == "Second title" and after["status"] == "claimed"
    assert outcome["updated"] == [] and outcome["cancelled"] == [] and outcome["unreleased"] == []
    del source.state["task"]["entities"]["t1"]
    source.state["task"]["ids"].remove("t1")
    sp.poll()
    assert only_task(store)["status"] == "claimed"


def test_readiness_tag_on_and_off(sp: SuperProductivityLane, store: Store, source: FakeSource, fake: FakeHTTP,
                                  sp_settings: Settings) -> None:
    sp_settings.AOS_SP_READY_TAG = "go"
    source.state = make_state(task("t1", "Go task", notes="Do it.\n\n## Acceptance\nmake a", tags=("freelance_bot", "go")))
    outcome = sp.poll()
    created = only_task(store)
    assert created["status"] == "ready" and outcome["released"] == [f"{created['id']} Go task"]
    assert outcome["created"] == [f"{created['id']} Go task"]
    assert "released:" in notices(fake)[0]

    source.state["task"]["entities"]["t1"]["tagIds"].remove("t-go")
    outcome = sp.poll()
    assert only_task(store)["status"] == "backlog" and outcome["unreleased"] == [f"{created['id']} Go task"]
    assert "unreleased:" in notices(fake)[1]

    source.state["task"]["entities"]["t1"]["tagIds"].append("t-go")
    outcome = sp.poll()
    assert only_task(store)["status"] == "ready" and outcome["released"] == [f"{created['id']} Go task"]

    # Removing explicit checks preserves authorization; the worker resolves repo defaults.
    source.state["task"]["entities"]["t1"]["notes"] = "Do it."
    outcome = sp.poll()
    assert only_task(store)["status"] == "ready" and only_task(store)["acceptance"] == []
    assert outcome["unreleased"] == [] and outcome["updated"] == [f"{created['id']} Go task"]
    assert "no acceptance" not in notices(fake)[-1]


def test_done_or_deleted_cancels_before_a_claim_and_is_ignored_after(sp: SuperProductivityLane, store: Store,
                                                                    source: FakeSource, fake: FakeHTTP) -> None:
    source.state = make_state(task("t1", "Backlog one", notes="a", tags=("agent-os",)),
                              task("t2", "Ready one", notes="b", tags=("agent-os", "go")),
                              task("t3", "Claimed one", notes="c", tags=("agent-os", "go")),
                              task("t4", "Done before", notes="d", tags=("agent-os",), done=True))
    sp.poll()
    by_ext = {t["external_id"]: t for t in store.list_tasks()}
    assert set(by_ext) == {"t1", "t2", "t3"}
    store.claim_task(by_ext["t3"]["id"], worker="w", worker_host="server", lease_seconds=600,
                     expected_spec_hash=by_ext["t3"]["spec_hash"], effective_acceptance=["make check"],
                     acceptance_source="workflow.test_command", workflow_commit="a" * 40,
                     workflow_ref="refs/heads/main", workflow_sha="b" * 64)

    source.state["task"]["entities"]["t1"]["isDone"] = True
    del source.state["task"]["entities"]["t2"]
    source.state["task"]["ids"].remove("t2")
    source.state["task"]["entities"]["t3"]["isDone"] = True
    outcome = sp.poll()
    by_ext = {t["external_id"]: t for t in store.list_tasks()}
    assert by_ext["t1"]["status"] == "cancelled" and by_ext["t2"]["status"] == "cancelled"
    assert by_ext["t3"]["status"] == "claimed"
    assert sorted(outcome["cancelled"]) == sorted([f"{by_ext['t1']['id']} Backlog one (done in the app)",
                                                   f"{by_ext['t2']['id']} Ready one (gone from the app)"])
    assert "cancelled:" in notices(fake)[-1]
    outcome = sp.poll()
    assert outcome["cancelled"] == [] and len(store.list_tasks()) == 3


def test_a_task_dragged_under_another_is_not_gone(sp: SuperProductivityLane, store: Store,
                                                  source: FakeSource) -> None:
    """A synced task made a subtask of another one leaves the lane's view but still exists: its bus
    task waits as it is, the parent's brief lists it, and the lane follows it again once it is
    top-level. Only deletion cancels it."""
    source.state = make_state(task("t1", "Parent", notes="repo: agent-os\nthe big one"),
                              task("t2", "Child to be", notes="repo: agent-os\nthe small one", tags=("go",)))
    sp.poll()
    by_ext = {t["external_id"]: t for t in store.list_tasks()}
    assert set(by_ext) == {"t1", "t2"} and by_ext["t2"]["status"] == "ready"
    entities = source.state["task"]["entities"]
    entities["t2"]["parentId"] = "t1"
    entities["t1"]["subTaskIds"] = ["t2"]
    outcome = sp.poll()
    by_ext = {t["external_id"]: t for t in store.list_tasks()}
    assert outcome["cancelled"] == [] and by_ext["t2"]["status"] == "ready"
    assert by_ext["t1"]["brief"] == "the big one\n\nSubtasks:\n- Child to be"
    assert outcome["updated"] == [f"{by_ext['t1']['id']} Parent"]
    del entities["t2"]["parentId"]
    entities["t1"]["subTaskIds"] = []
    entities["t2"]["title"] = "Child again"
    outcome = sp.poll()
    by_ext = {t["external_id"]: t for t in store.list_tasks()}
    assert outcome["cancelled"] == [] and by_ext["t2"]["title"] == "Child again" and by_ext["t2"]["status"] == "ready"
    assert by_ext["t1"]["brief"] == "the big one"
    del entities["t2"]
    source.state["task"]["ids"].remove("t2")
    outcome = sp.poll()
    assert outcome["cancelled"] == [f"{by_ext['t2']['id']} Child again (gone from the app)"]


def test_no_repo_blocks_import_but_missing_checks_allow_authorization(sp: SuperProductivityLane, store: Store,
                                                                    source: FakeSource, fake: FakeHTTP) -> None:
    source.state = make_state(task("t1", "No repo", notes="x", tags=("go",)),
                              task("t2", "Two repos", notes="x", tags=("agent-os", "freelance_bot", "go")),
                              task("t3", "No acceptance", notes="x", tags=("freelance_bot", "go")))
    outcome = sp.poll()
    created = only_task(store)
    assert created["external_id"] == "t3" and created["status"] == "ready" and created["acceptance"] == []
    no_repo = "No repo (no repo: add a line 'repo: <name>' to the notes)"
    assert outcome["no_repo"] == [no_repo, "Two repos (2 repo tags: agent-os, freelance_bot)"]
    assert outcome["released"] == [f"{created['id']} No acceptance"]
    text = notices(fake)[0]
    assert "no repo (not created)" in text and no_repo in text and "Two repos" in text
    assert "no acceptance (stays in backlog)" not in text

    assert sp.poll()["no_repo"] == outcome["no_repo"] and len(notices(fake)) == 1
    del source.state["task"]["entities"]["t1"]
    source.state["task"]["ids"].remove("t1")
    sp.poll()
    assert len(notices(fake)) == 2 and no_repo not in notices(fake)[1] and "Two repos" in notices(fake)[1]
    assert only_task(store)["status"] == "ready"


def test_out_of_scope_tasks_are_left_alone(sp: SuperProductivityLane, store: Store, source: FakeSource,
                                           sp_settings: Settings) -> None:
    source.state = make_state(task("t1", "Elsewhere", notes="x", tags=("agent-os",), project="p-other"),
                              task("t2", "In scope", notes="y", tags=("agent-os",)))
    sp.poll()
    created = only_task(store)
    assert created["external_id"] == "t2"
    source.state["task"]["entities"]["t2"]["projectId"] = "p-other"
    source.state["task"]["entities"]["t2"]["title"] = "Moved out"
    outcome = sp.poll()
    assert only_task(store)["status"] == "ready" and only_task(store)["title"] == "In scope"
    assert outcome["cancelled"] == [] and outcome["updated"] == []

    sp_settings.AOS_SP_PROJECT, sp_settings.AOS_SP_TAG = "", "aos-in"
    source.state["task"]["entities"]["t1"]["tagIds"].append("t-aos-in")
    sp.poll()
    by_ext = {t["external_id"]: t for t in store.list_tasks()}
    assert set(by_ext) == {"t1", "t2"} and by_ext["t1"]["title"] == "Elsewhere"


def test_decode_error_changes_nothing_and_records_the_lane_error(sp: SuperProductivityLane, store: Store,
                                                                 source: FakeSource, fake: FakeHTTP) -> None:
    source.state = make_state(task("t1", "Go task", notes="Do it.", tags=("agent-os", "go")))
    sp.poll()
    created = only_task(store)
    source.error = SyncFileError("the sync file is encrypted (pf_E prefix) and AOS_SP_SYNC_PASSWORD is not set")
    outcome = sp.poll()
    assert outcome["error"] == str(source.error) and outcome["created"] == [] and outcome["cancelled"] == []
    assert only_task(store) == created
    assert store.lanes()[-1]["last_outcome"] == f"error:{source.error}"
    assert len(notices(fake)) == 1
    source.error = None
    sp.poll()
    assert store.lanes()[-1]["last_outcome"] == "ok"


@pytest.mark.parametrize("invalid_state", [
    {}, {"task": {"entities": []}},
    {"task": {"ids": ["t-ready", "missing"], "entities": {
        "t-ready": task("t-ready", "Must not update", notes="Changed", tags=("agent-os",))[0],
    }}},
    {"task": {"ids": ["t-ready", "bad"], "entities": {
        "t-ready": task("t-ready", "Must not update", notes="Changed", tags=("agent-os",))[0],
        "bad": None,
    }}},
])
def test_malformed_snapshot_preserves_queued_specs_and_live_run_through_recovery(
        sp: SuperProductivityLane, store: Store, source: FakeSource, fake: FakeHTTP,
        sp_settings: Settings, invalid_state: Any) -> None:
    sp_settings.AOS_SP_READY_TAG = "go"
    source.state = make_state(
        task("t-ready", "Ready", notes="Ready brief", tags=("agent-os", "go")),
        task("t-backlog", "Backlog", notes="Backlog brief", tags=("agent-os",)),
        task("t-live", "Claimed", notes="Claimed brief", tags=("agent-os", "go")),
    )
    healthy = copy.deepcopy(source.state)
    assert sp.poll()["error"] is None
    by_ext = {t["external_id"]: t for t in store.list_tasks()}
    assert by_ext["t-ready"]["status"] == "ready" and by_ext["t-backlog"]["status"] == "backlog"
    run = store.claim_task(
        by_ext["t-live"]["id"], worker="w", worker_host="server", lease_seconds=600,
        expected_spec_hash=by_ext["t-live"]["spec_hash"], effective_acceptance=["make check"],
        acceptance_source="workflow.test_command", workflow_commit="a" * 40,
        workflow_ref="refs/heads/main", workflow_sha="b" * 64,
    )
    before_tasks, before_run = store.list_tasks(), store.get_run(run["id"])
    before_notices = notices(fake)
    source.state = copy.deepcopy(invalid_state)
    source.state["project"] = copy.deepcopy(healthy["project"])
    source.state["tag"] = copy.deepcopy(healthy["tag"])
    outcome = sp.poll()
    assert outcome["error"]
    assert all(value == [] for key, value in outcome.items() if key != "error")
    assert store.list_tasks() == before_tasks
    assert store.get_run(run["id"]) == before_run
    assert notices(fake) == before_notices
    receipt = json.loads(store.runs_path.read_text().splitlines()[-1])
    assert receipt["lane"] == LANE and receipt["outcome"].startswith("error:")
    assert all(count == 0 for count in receipt["receipt"].values())

    source.state = healthy
    recovered = sp.poll()
    assert recovered["error"] is None
    assert all(value == [] for key, value in recovered.items() if key != "error")
    assert store.list_tasks() == before_tasks
    assert store.get_run(run["id"]) == before_run
    assert notices(fake) == before_notices
    assert store.lanes()[-1]["last_outcome"] == "ok"


@pytest.mark.parametrize("empty_task_model", [{"entities": {}}, {"ids": [], "entities": {}}])
def test_explicit_empty_snapshot_cancels_only_preclaim_tasks(
        sp: SuperProductivityLane, store: Store, source: FakeSource,
        sp_settings: Settings, empty_task_model: dict[str, Any]) -> None:
    sp_settings.AOS_SP_READY_TAG = "go"
    source.state = make_state(
        task("t-ready", "Ready", notes="Ready brief", tags=("agent-os", "go")),
        task("t-backlog", "Backlog", notes="Backlog brief", tags=("agent-os",)),
        task("t-live", "Claimed", notes="Claimed brief", tags=("agent-os", "go")),
    )
    assert sp.poll()["error"] is None
    by_ext = {t["external_id"]: t for t in store.list_tasks()}
    assert by_ext["t-ready"]["status"] == "ready" and by_ext["t-backlog"]["status"] == "backlog"
    run = store.claim_task(
        by_ext["t-live"]["id"], worker="w", worker_host="server", lease_seconds=600,
        expected_spec_hash=by_ext["t-live"]["spec_hash"], effective_acceptance=["make check"],
        acceptance_source="workflow.test_command", workflow_commit="a" * 40,
        workflow_ref="refs/heads/main", workflow_sha="b" * 64,
    )
    before_run = store.get_run(run["id"])
    source.state = {"task": empty_task_model}
    outcome = sp.poll()
    after = {t["external_id"]: t for t in store.list_tasks()}
    assert outcome["error"] is None and len(outcome["cancelled"]) == 2
    assert after["t-ready"]["status"] == after["t-backlog"]["status"] == "cancelled"
    assert after["t-live"]["status"] == "claimed"
    assert store.get_run(run["id"]) == before_run


# -- the repo line, automatic release, the hold tag ----------------------------------

ACC = "\n\n## Acceptance\nmake check\n"


def test_repo_line_names_the_repo_and_is_stripped_from_the_brief(sp: SuperProductivityLane, store: Store,
                                                                 source: FakeSource) -> None:
    brief, acceptance, aos = parse_notes("Do it.\nrepo: agent-os\nMore.")
    assert brief == "Do it.\nMore." and aos == {"repo": "agent-os"} and acceptance == []
    source.state = make_state(task("t1", "Line task", notes="Do it.\nrepo: agent-os\nMore." + ACC))
    outcome = sp.poll()
    created = only_task(store)
    assert created["repo"] == "agent-os" and created["brief"] == "Do it.\nMore." and outcome["no_repo"] == []


def test_repo_inside_the_aos_section(sp: SuperProductivityLane, store: Store, source: FakeSource) -> None:
    notes = "Do it." + ACC + "\n## AOS\nrepo: freelance_bot\ntier: 0\n"
    assert parse_notes(notes)[2] == {"repo": "freelance_bot", "tier": "0"}
    source.state = make_state(task("t1", "Aos task", notes=notes))
    sp.poll()
    assert only_task(store)["repo"] == "freelance_bot" and only_task(store)["tier"] == 0


def test_repo_line_in_a_fence_is_ignored_and_kept(sp: SuperProductivityLane, store: Store, source: FakeSource) -> None:
    notes = "Example:\n```\nrepo: freelance_bot\n```\nrepo: agent-os"
    brief, _, aos = parse_notes(notes)
    assert brief == "Example:\n```\nrepo: freelance_bot\n```" and aos == {"repo": "agent-os"}
    brief, _, aos = parse_notes("```\nrepo: agent-os\n```")
    assert brief == "```\nrepo: agent-os\n```" and aos == {}
    source.state = make_state(task("t1", "Fenced", notes=notes + ACC))
    sp.poll()
    assert only_task(store)["repo"] == "agent-os" and "repo: freelance_bot" in only_task(store)["brief"]


def test_repo_key_is_case_insensitive_and_takes_a_list_marker(sp: SuperProductivityLane) -> None:
    assert parse_notes("Body\nRepo: agent-os") == ("Body", [], {"repo": "agent-os"})
    assert parse_notes("Body\n- repo: agent-os") == ("Body", [], {"repo": "agent-os"})
    assert parse_notes("Body\n  * REPO :  agent-os  ") == ("Body", [], {"repo": "agent-os"})
    assert parse_notes("Body\nrepo:") == ("Body", [], {})
    assert parse_notes("repo: first\nrepo: second")[2] == {"repo": "first"}
    assert parse_notes("repo: first\n## AOS\nrepo: second")[2] == {"repo": "first"}
    assert parse_notes("## AOS\nrepo: second\n\n## Context\nrepo: first")[2] == {"repo": "second"}


def test_unknown_repo_in_the_line_is_no_repo_with_the_reason(sp: SuperProductivityLane, store: Store,
                                                              source: FakeSource, fake: FakeHTTP) -> None:
    source.state = make_state(task("t1", "Nowhere", notes="repo: nope" + ACC, tags=("agent-os",)))
    outcome = sp.poll()
    assert store.list_tasks() == []
    assert outcome["no_repo"] == ["Nowhere (unknown repo 'nope' in the notes)"]
    assert "no repo (not created)" in notices(fake)[0]


def test_no_repo_at_all_names_the_line_to_add(sp: SuperProductivityLane, source: FakeSource) -> None:
    source.state = make_state(task("t1", "Bare", notes="x" + ACC))
    assert sp.poll()["no_repo"] == ["Bare (no repo: add a line 'repo: <name>' to the notes)"]


def test_the_line_beats_a_conflicting_repo_tag(sp: SuperProductivityLane, store: Store, source: FakeSource) -> None:
    source.state = make_state(task("t1", "Both", notes="repo: freelance_bot" + ACC, tags=("agent-os",)),
                              task("t2", "Two tags", notes="repo: agent-os" + ACC, tags=("agent-os", "freelance_bot")))
    sp.poll()
    by_ext = {t["external_id"]: t for t in store.list_tasks()}
    assert by_ext["t1"]["repo"] == "freelance_bot" and by_ext["t2"]["repo"] == "agent-os"


def test_title_only_task_uses_the_title_as_brief_and_is_released(sp: SuperProductivityLane, store: Store,
                                                                  source: FakeSource) -> None:
    source.state = make_state(task("t1", "Fix the flaky poller test", notes="repo: agent-os", subtasks=("look at CI",)),
                              task("t2", "Title only", notes="", tags=("agent-os",)))
    outcome = sp.poll()
    by_ext = {t["external_id"]: t for t in store.list_tasks()}
    assert by_ext["t1"]["brief"] == "Fix the flaky poller test\n\nSubtasks:\n- look at CI"
    assert by_ext["t2"]["brief"] == "Title only"
    assert by_ext["t1"]["status"] == "ready" and by_ext["t2"]["status"] == "ready" and len(outcome["released"]) == 2


def test_released_automatically_without_tags(sp: SuperProductivityLane, store: Store, source: FakeSource) -> None:
    source.state = make_state(task("t1", "Auto", notes="repo: agent-os" + ACC))
    outcome = sp.poll()
    assert only_task(store)["status"] == "ready" and outcome["released"] == [f"{only_task(store)['id']} Auto"]


def test_hold_tag_keeps_a_task_back_in_any_case(sp: SuperProductivityLane, store: Store, source: FakeSource) -> None:
    source.state = make_state(task("t1", "Held", notes="repo: agent-os" + ACC, tags=("hold",)),
                              task("t2", "Held cap", notes="repo: agent-os" + ACC, tags=("Hold",)))
    outcome = sp.poll()
    assert {t["status"] for t in store.list_tasks()} == {"backlog"} and len(store.list_tasks()) == 2
    assert outcome["released"] == []


def test_hold_added_to_a_ready_task_unreleases_it_before_a_claim(sp: SuperProductivityLane, store: Store,
                                                                  source: FakeSource) -> None:
    source.state = make_state(task("t1", "Soon held", notes="repo: agent-os" + ACC))
    sp.poll()
    created = only_task(store)
    assert created["status"] == "ready"
    source.state["task"]["entities"]["t1"]["tagIds"].append("t-hold")
    outcome = sp.poll()
    assert only_task(store)["status"] == "backlog" and outcome["unreleased"] == [f"{created['id']} Soon held"]
    source.state["task"]["entities"]["t1"]["tagIds"].remove("t-hold")
    assert sp.poll()["released"] == [f"{created['id']} Soon held"]
    store.claim_task(created["id"], worker="w", worker_host="server", lease_seconds=600,
                     expected_spec_hash=created["spec_hash"], effective_acceptance=created["acceptance"],
                     acceptance_source="task")
    source.state["task"]["entities"]["t1"]["tagIds"].append("t-hold")
    assert sp.poll()["unreleased"] == [] and only_task(store)["status"] == "claimed"


def test_ready_tag_setting_restores_the_tag_gate(sp: SuperProductivityLane, store: Store, source: FakeSource,
                                                 sp_settings: Settings) -> None:
    sp_settings.AOS_SP_READY_TAG = "go"
    source.state = make_state(task("t1", "Gated", notes="repo: agent-os" + ACC),
                              task("t2", "Open", notes="repo: agent-os" + ACC, tags=("go",)),
                              task("t3", "Open but held", notes="repo: agent-os" + ACC, tags=("go", "hold")))
    sp.poll()
    by_ext = {t["external_id"]: t for t in store.list_tasks()}
    assert by_ext["t1"]["status"] == "backlog" and by_ext["t2"]["status"] == "ready" and by_ext["t3"]["status"] == "backlog"


# -- wiring --------------------------------------------------------------------

def test_api_route_health_and_cli(sp: SuperProductivityLane, sp_settings: Settings, store: Store, telegram: Telegram,
                                  source: FakeSource, fake: FakeHTTP, capsys: pytest.CaptureFixture[str]) -> None:
    source.state = make_state(task("t1", "Go task", notes="Do it.", tags=("agent-os", "go")))
    app = create_app(sp_settings, store=store, telegram=telegram, sp_lane=sp, background=False)
    client = TestClient(app)
    assert app.state.sp_lane is sp
    assert client.post("/sources/sp/poll").status_code == 401
    client.headers["Authorization"] = "Bearer test-bus-token"
    response = client.post("/sources/sp/poll")
    created = only_task(store)
    assert response.status_code == 200 and response.json()["created"] == [f"{created['id']} Go task"]
    assert response.json()["released"] == [f"{created['id']} Go task"]
    lanes = {row["lane"]: row for row in client.get("/health").json()["lanes"]}
    assert LANE in lanes and not lanes[LANE]["stale"]
    assert {row["lane"]: row["cadence_seconds"] for row in store.lanes()}[LANE] == sp_settings.AOS_SP_POLL_SECONDS

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/sources/sp/poll"
        assert request.headers["Authorization"] == "Bearer t"
        return httpx.Response(200, json={"created": [], "released": ["AO-1 x"]})
    code = cli.main(["sp", "poll"], client=httpx.Client(base_url="http://bus.test",
                                                         headers={"Authorization": "Bearer t"},
                                                         transport=httpx.MockTransport(handle)))
    assert code == 0 and json.loads(capsys.readouterr().out)["released"] == ["AO-1 x"]


def test_lane_off_without_settings(settings: Settings, store: Store, telegram: Telegram) -> None:
    app = create_app(settings, store=store, telegram=telegram, background=False)
    assert app.state.sp_lane is None and LANE not in {row["lane"] for row in store.lanes()}
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer test-bus-token"
    response = client.post("/sources/sp/poll")
    assert response.status_code == 404 and "AOS_SP_" in response.json()["detail"]


def test_sp_enabled_needs_the_credentials_the_folder_and_a_scope(tmp_path) -> None:
    base = dict(_env_file=None, BUS_TOKEN="t", BUS_DATA_DIR=tmp_path)
    creds = dict(AOS_SP_DROPBOX_APP_KEY="k", AOS_SP_DROPBOX_APP_SECRET="s", AOS_SP_DROPBOX_REFRESH_TOKEN="r")
    assert not Settings(**base).sp_enabled
    assert not Settings(**base, **creds, AOS_SP_DROPBOX_PATH="/").sp_enabled
    assert not Settings(**base, **creds, AOS_SP_PROJECT="AOS").sp_enabled
    assert Settings(**base, **creds, AOS_SP_DROPBOX_PATH="/", AOS_SP_PROJECT="AOS").sp_enabled
    assert Settings(**base, **creds, AOS_SP_DROPBOX_PATH="/Apps/sp", AOS_SP_TAG="aos").sp_enabled
    on = Settings(**base, **creds, AOS_SP_DROPBOX_PATH="/", AOS_SP_TAG="aos")
    assert on.AOS_SP_READY_TAG == "" and on.AOS_SP_HOLD_TAG == "hold" and on.AOS_SP_POLL_SECONDS == 3600 and on.AOS_SP_SYNC_PASSWORD == ""
    assert on.AOS_SP_DROPBOX_API_BASE == "https://api.dropboxapi.com"
    assert on.AOS_SP_DROPBOX_CONTENT_BASE == "https://content.dropboxapi.com"
    with pytest.raises(ValueError):
        Settings(**base, AOS_SP_POLL_SECONDS=0)


def test_lane_builds_its_dropbox_client_from_the_settings(store: Store, telegram: Telegram, sp_settings: Settings) -> None:
    sp_settings.AOS_SP_DROPBOX_API_BASE = "https://api.test"
    sp_settings.AOS_SP_DROPBOX_CONTENT_BASE = "https://content.test"
    sp_settings.AOS_SP_SYNC_PASSWORD = "pw"
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "api.test":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 14400})
        return httpx.Response(409, json={"error_summary": "path/not_found/"})
    lane = SuperProductivityLane(store, telegram, sp_settings, client=httpx.Client(transport=httpx.MockTransport(handle)))
    outcome = lane.poll()
    assert outcome["error"].startswith("no sync files in Dropbox folder /Apps/sp")
    assert seen == ["https://api.test/oauth2/token", "https://content.test/2/files/download",
                    "https://content.test/2/files/download"]



def test_execution_key_in_the_aos_section_maps_to_the_task(
        sp: SuperProductivityLane, store: Store, source: FakeSource) -> None:
    source.state = make_state(task("t1", "Interactive", notes="Body.\n\n## AOS\nexecution: Interactive",
                                   tags=("agent-os",)))
    sp.poll()
    created = only_task(store)
    assert created["execution"] == "interactive"
    entity = source.state["task"]["entities"]["t1"]
    entity["notes"] = "Body."
    sp.poll()
    edited = only_task(store)
    assert edited["execution"] == "headless" and edited["spec_hash"] != created["spec_hash"]
    entity["notes"] = "Body.\n\n## AOS\nexecution: later"
    sp.poll()
    assert only_task(store)["execution"] == "headless"
