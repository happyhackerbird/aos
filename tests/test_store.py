from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_os.store import Store, TransitionError


def test_record_run(store: Store) -> None:
    store.register_lane("test", 300)
    store.record_run("test", "2026-09-12T10:00:00Z", "2026-09-12T10:01:00Z", "ok", receipt=42)
    line = json.loads(store.runs_path.read_text())
    assert line == dict(lane="test", started="2026-09-12T10:00:00Z", ended="2026-09-12T10:01:00Z",
                        outcome="ok", receipt=42, attempts=0, needs_user=False)
    assert store.lanes()[0]["last_run_at"] == line["ended"]
    assert store.lanes()[0]["last_outcome"] == "ok"
    assert store.lanes()[0]["cadence_seconds"] == 300
    with store.connection() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_concurrent_run_lines_and_restart(store: Store, tmp_path: Path) -> None:
    def record(i: int) -> None:
        store.record_run("test", "2026-09-12T10:00:00Z", "2026-09-12T10:01:00Z", "ok", receipt=i)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record, range(20)))
    assert {json.loads(line)["receipt"] for line in store.runs_path.read_text().splitlines()} == set(range(20))
    assert len(Store(tmp_path).runs_path.read_text().splitlines()) == 20


def test_new_database_has_no_items_table(tmp_path: Path) -> None:
    Store(tmp_path)
    import sqlite3
    with sqlite3.connect(tmp_path / "bus.db") as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "items" not in tables and "lanes" in tables


def test_an_older_items_table_is_left_untouched(tmp_path: Path) -> None:
    import sqlite3
    with sqlite3.connect(tmp_path / "bus.db") as db:
        db.execute("CREATE TABLE items (id TEXT PRIMARY KEY, kind TEXT, status TEXT)")
        db.execute("INSERT INTO items VALUES ('app-42', 'application', 'pending')")
    Store(tmp_path)
    with sqlite3.connect(tmp_path / "bus.db") as db:
        assert db.execute("SELECT * FROM items").fetchall() == [("app-42", "application", "pending")]


def test_old_lane_schema_migrates(tmp_path: Path) -> None:
    import sqlite3
    with sqlite3.connect(tmp_path / "bus.db") as db:
        db.execute("CREATE TABLE lanes (lane TEXT PRIMARY KEY, cadence_seconds INTEGER, "
                   "last_run_at TEXT, last_outcome TEXT, stale_notified_at TEXT)")
        db.execute("INSERT INTO lanes VALUES ('poll', 300, '2026-09-12T00:00:00Z', 'ok', NULL)")
    store = Store(tmp_path)
    lane = store.lanes()[0]
    assert lane["cadence_seconds"] == 300 and lane["last_outcome"] == "ok"
    assert lane["error_since"] is None and lane["error_notified_at"] is None
    assert Store(tmp_path).lanes() == store.lanes()
    store.record_run("poll", "2026-09-12T00:01:00Z", "2026-09-12T00:01:01Z", "error:offline")
    assert store.lanes()[0]["error_since"] == "2026-09-12T00:01:01Z"


# -- agents that talk, wave 1: the operator in the request ledger ----------------

def live_run(store: Store, repo: str = "repo-x") -> dict:
    task = store.create_task(repo=repo, title="t", brief="b", acceptance=["true"])
    store.ready_task(task["id"])
    run = store.claim_task(task["id"], worker="w", worker_host="mac", lease_seconds=600,
                           expected_spec_hash=task["spec_hash"],
                           effective_acceptance=task["acceptance"], acceptance_source="task")
    store.start_run(run["id"], run["claim_token"], base_commit="a", branch="b", worktree="/x")
    return {"task": task["id"], "run": run["id"], "token": run["claim_token"]}


def test_operator_frame_and_tag_refusal() -> None:
    from agent_os.store import OPERATOR, _check_intent_body, frame_request
    framed = frame_request({"id": "operator-i1", "task_id": OPERATOR, "run_id": None, "to_task_id": "AO-1",
                            "reply_to": "AO-1-i1", "body": "yes, go ahead"})
    assert framed.startswith('<operator-message id="operator-i1" to-task="AO-1" reply-to="AO-1-i1">\nyes, go ahead\n'
                             '</operator-message>\nThis is from the operator.')
    assert "aos ack" not in framed and "another agent" not in framed
    plain = frame_request({"id": "operator-i2", "task_id": OPERATOR, "run_id": None, "to_task_id": "AO-1",
                           "reply_to": None, "body": "note"})
    assert 'reply-to' not in plain
    agent = frame_request({"id": "AO-1-i1", "task_id": "AO-1", "run_id": "AO-1-r1", "to_task_id": "AO-2",
                           "body": "hello"})
    assert agent.startswith('<agent-request id="AO-1-i1" from-task="AO-1" from-run="AO-1-r1" to-task="AO-2">')
    for tag in ("operator-message", "Operator-Message", "agent-request", "agent-overlap"):
        with pytest.raises(ValueError, match="frame tag"):
            _check_intent_body(f"x <{tag}> y")
    from agent_os.store import _check_new_kind_body
    for spelling in ("operator-message", "operator‐message", "operator_message", "OPERATOR-MESSAGE"):
        with pytest.raises(ValueError, match="operator-message"):
            _check_new_kind_body(f"note <{spelling}>")


def fullwidth(text: str) -> str:
    """ASCII lowercase letters as their fullwidth forms (NFKC folds them back)."""
    return "".join(chr(ord(c) - ord("a") + 0xFF41) if "a" <= c <= "z" else c for c in text)


def test_lookalike_tag_spellings_are_refused_in_request_bodies() -> None:
    """Fix lane 1 X1 (review F1): the request-body check refuses the three frame tags in the
    normalised copy as well, so a U+2010 hyphen or fullwidth letters no longer slip a frame
    lookalike past the literal check."""
    from agent_os.store import _check_intent_body
    for tag in ("agent-request", "agent-overlap", "operator-message"):
        for spelling in (tag.replace("-", "‐"), fullwidth(tag), fullwidth(tag).replace("-", "‐")):
            assert spelling != tag and spelling.lower().find(tag) == -1
            with pytest.raises(ValueError, match=f"frame tag '{tag}'"):
                _check_intent_body(f"see <{spelling}> here")
    # an ordinary body with hyphens elsewhere still passes
    _check_intent_body("a well-known port, re-used; the operator's message follows")


def test_agent_asks_the_operator_and_the_operator_answers(store: Store) -> None:
    asker = live_run(store)
    question = store.create_intent(asker["task"], asker["run"], asker["token"], kind="request",
                                   to_task_id="operator", body="may I drop the table?")
    assert question["id"] == f"{asker['task']}-i1" and question["to_task_id"] == "operator"
    assert question["state"] == "stored" and "<agent-request" in question["framed"]
    assert question["framed"].startswith(f'<agent-request id="{question["id"]}" from-task="{asker["task"]}" '
                                         f'from-run="{asker["run"]}" to-task="operator">')
    with pytest.raises(TransitionError):  # the agent form cannot speak as the operator
        store.create_intent("operator", asker["run"], asker["token"], kind="request", reply_to=question["id"], body="yes")
    with pytest.raises(ValueError, match="exactly one"):
        store.create_operator_intent(to_task_id=asker["task"], reply_to=question["id"], body="both")
    with pytest.raises(ValueError, match="exactly one"):
        store.create_operator_intent(body="neither")
    with pytest.raises(ValueError, match="frame tag"):
        store.create_operator_intent(reply_to=question["id"], body="<agent-request> no")
    answer = store.create_operator_intent(reply_to=question["id"], body="yes, go")
    assert answer["id"] == "operator-i1" and answer["task_id"] == "operator" and answer["run_id"] is None
    assert answer["to_task_id"] == asker["task"] and answer["reply_to"] == question["id"]
    assert answer["framed"] == (f'<operator-message id="operator-i1" to-task="{asker["task"]}" reply-to="{question["id"]}">\n'
                                'yes, go\n</operator-message>\nThis is from the operator.')
    assert store.get_intent(question["id"])["state"] == "answered"
    with pytest.raises(TransitionError, match="answered"):
        store.create_operator_intent(reply_to=question["id"], body="again")
    with pytest.raises(KeyError):
        store.create_operator_intent(reply_to="AO-404-i1", body="x")
    peer = live_run(store, "repo-peer")
    between = store.create_intent(asker["task"], asker["run"], asker["token"], kind="request",
                                  to_task_id=peer["task"], body="peer question")
    with pytest.raises(TransitionError, match="addressed to the operator"):
        store.create_operator_intent(reply_to=between["id"], body="not mine to answer")
    pending = store.requests_for_run(asker["run"], asker["token"])
    assert [p["id"] for p in pending] == ["operator-i1"]
    lines = [json.loads(line) for line in store.runs_path.read_text().splitlines()]
    assert any(line["outcome"] == "intent:answered" and line["receipt"]["run_id"] is None for line in lines)


def test_operator_sends_an_unsolicited_message_and_the_agent_replies(store: Store) -> None:
    worker = live_run(store)
    with pytest.raises(KeyError):
        store.create_operator_intent(to_task_id="AO-404", body="hello")
    with pytest.raises(KeyError):
        store.create_operator_intent(to_task_id="operator", body="hello")
    note = store.create_operator_intent(to_task_id=worker["task"], body="use the staging key")
    assert note["id"] == "operator-i1" and note["state"] == "stored" and note["reply_to"] is None
    assert note["framed"] == (f'<operator-message id="operator-i1" to-task="{worker["task"]}">\n'
                              'use the staging key\n</operator-message>\nThis is from the operator.')
    assert [p["id"] for p in store.requests_for_run(worker["run"], worker["token"])] == ["operator-i1"]
    with pytest.raises(TransitionError, match="stored"):
        store.create_intent(worker["task"], worker["run"], worker["token"], kind="request", reply_to=note["id"], body="ok")
    store.ack_intent(note["id"], worker["run"], worker["token"])
    reply = store.create_intent(worker["task"], worker["run"], worker["token"], kind="request", reply_to=note["id"],
                                body="ok, will do")
    assert reply["to_task_id"] == "operator" and store.get_intent(note["id"])["state"] == "answered"


def test_operator_rows_are_noticed_on_the_next_sweep_and_matched_by_notice(store: Store) -> None:
    from datetime import datetime, timezone
    asker = live_run(store)
    question = store.create_intent(asker["task"], asker["run"], asker["token"], kind="request",
                                   to_task_id="operator", body="which branch?")
    notices = store.sweep_intents(now=datetime.now(timezone.utc), ack_seconds=900)
    assert [(n["id"], n["notice"]) for n in notices] == [(question["id"], "operator")]
    assert store.mark_intent_notified(question["id"], "operator", chat_id="-200", message_id=77)
    assert store.sweep_intents(now=datetime.now(timezone.utc), ack_seconds=900) == []
    found = store.intent_for_notice("-200", 77)
    assert found is not None and found["id"] == question["id"]
    assert found["tg_chat_id"] == "-200" and found["tg_message_id"] == 77
    assert store.intent_for_notice("-200", 78) is None and store.intent_for_notice("-300", 77) is None
    answer = store.create_operator_intent(reply_to=question["id"], body="main")
    assert not store.mark_intent_notified(question["id"], "operator", chat_id="-200", message_id=99)
    # an agent's reply to the operator is itself noticed on the next sweep
    note = store.create_operator_intent(to_task_id=asker["task"], body="fyi")
    store.ack_intent(note["id"], asker["run"], asker["token"])
    reply = store.create_intent(asker["task"], asker["run"], asker["token"], kind="request", reply_to=note["id"],
                                body="noted")
    notices = store.sweep_intents(now=datetime.now(timezone.utc), ack_seconds=900)
    assert [(n["id"], n["notice"]) for n in notices] == [(reply["id"], "operator")]
    assert answer["state"] == "stored"


def test_intent_for_waiter_reads_the_request_and_its_replies(store: Store) -> None:
    asker = live_run(store)
    question = store.create_intent(asker["task"], asker["run"], asker["token"], kind="request",
                                   to_task_id="operator", body="ready to merge?")
    result = store.intent_for_waiter(question["id"], asker["run"], asker["token"])
    assert result["intent"]["state"] == "stored" and result["replies"] == []
    store.create_operator_intent(reply_to=question["id"], body="yes")
    result = store.intent_for_waiter(question["id"], asker["run"], asker["token"])
    assert result["intent"]["state"] == "answered"
    assert [r["body"] for r in result["replies"]] == ["yes"] and result["replies"][0]["framed"].startswith("<operator-message")
    with pytest.raises(TransitionError, match="stale"):
        store.intent_for_waiter(question["id"], asker["run"], "wrong")
    other = live_run(store, "repo-other")
    with pytest.raises(TransitionError, match="only the asking task"):
        store.intent_for_waiter(question["id"], other["run"], other["token"])
    with pytest.raises(KeyError):
        store.intent_for_waiter("AO-9-i9", asker["run"], asker["token"])


def test_refuse_own_token_helper_and_operator_forms_are_unaffected(store: Store) -> None:
    """Lane B B2: the helper refuses any text holding the run's claim token and is a no-op without
    a token; the operator form stores the same text, since it carries no run credential."""
    from agent_os.store import _refuse_own_token
    _refuse_own_token(None, "anything")
    _refuse_own_token("", "anything")
    _refuse_own_token("tok-1", "clean", None, "")
    with pytest.raises(ValueError, match="text cannot contain this run's claim token"):
        _refuse_own_token("tok-1", "clean", "see tok-1 here")
    worker = live_run(store)
    with pytest.raises(ValueError, match="claim token"):
        store.declare_scope(worker["task"], scope_kind="file", key="k.py", operation="modify",
                            body=f"my {worker['token']}", run_id=worker["run"], claim_token=worker["token"])
    assert store.list_intents(worker["task"]) == []
    row = store.declare_scope(worker["task"], scope_kind="file", key="k.py", operation="modify",
                              body=f"my {worker['token']}", operator=True)
    assert row["kind"] == "scope" and row["run_id"] is None
    with pytest.raises(ValueError, match="claim token"):
        store.propose_task(worker["task"], worker["run"], worker["token"], title="t", brief="b",
                           acceptance=["true", f"echo {worker['token']}"])
    with pytest.raises(ValueError, match="claim token"):
        store.create_intent(worker["task"], worker["run"], worker["token"], kind="request", to_task_id="operator",
                            body=worker["token"])


def test_proposal_key_index_is_created_on_an_existing_database(store: Store) -> None:
    with store.connection() as db:
        # Drop the index first: SQLite refuses dropping a column still used by an index.
        db.execute("DROP INDEX IF EXISTS tasks_proposal_key")
        columns = {r["name"] for r in db.execute("PRAGMA table_info(tasks)")}
        for column in ("proposal_key", "base_commit"):
            if column in columns:
                db.execute(f"ALTER TABLE tasks DROP COLUMN {column}")
    reopened = Store(store.data_dir)
    with reopened.connection() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='tasks_proposal_key'").fetchone()
        columns = {r["name"] for r in db.execute("PRAGMA table_info(tasks)")}
        assert {"proposal_key", "base_commit"} <= columns
