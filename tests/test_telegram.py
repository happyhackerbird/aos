from __future__ import annotations

import threading
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from agent_os.config import Settings
from agent_os.store import Store, TransitionError
from agent_os.telegram import TRUNCATION, Telegram, branch_url, hash8, render_code, truncate
from conftest import FakeHTTP


def callback(data: str, chat: int = -200) -> dict[str, Any]:
    return {"id": "q1", "message": {"chat": {"id": chat}}, "data": data}


def test_truncate() -> None:
    assert len(truncate("a" * 4001)) == 4000
    assert truncate("a" * 4001).endswith(TRUNCATION)
    assert truncate("a" * 4000) == "a" * 4000
    assert truncate("a\u2014b\u2013c") == "a-b-c"


def test_merge_and_requeue_callbacks_reach_the_code_lane(telegram: Telegram, store: Store, fake: FakeHTTP) -> None:
    code = Mock()
    code.merge_callback.return_value = {"runs": [{"status": "passed", "merge_commit": "d" * 40}]}
    telegram.dispatch(callback("mg:AO-1-r1:cccccccc"), store, code)
    code.merge_callback.assert_called_once_with("AO-1-r1", "cccccccc")
    telegram.dispatch(callback("rq:AO-1:AO-1-r1"), store, code)
    code.requeue.assert_called_once_with("AO-1", run_id="AO-1-r1")
    answers = [call[2]["text"] for call in fake.method("answerCallbackQuery")]
    assert answers == ["merged dddddddd", "requeued"]


@pytest.mark.parametrize("data", ["ap:app-42:aaaaaaaa", "sk:app-42", "dn:app-42", "zz"])
def test_retired_and_unknown_callbacks_are_answered_unknown(telegram: Telegram, store: Store, fake: FakeHTTP,
                                                            data: str) -> None:
    code = Mock()
    telegram.dispatch(callback(data), store, code)
    assert code.mock_calls == []
    assert [call[2]["text"] for call in fake.method("answerCallbackQuery")] == ["unknown action"]


def test_callback_without_a_code_lane(telegram: Telegram, store: Store, fake: FakeHTTP) -> None:
    telegram.dispatch(callback("rq:AO-1:AO-1-r1"), store, None)
    assert fake.method("answerCallbackQuery")[-1][2]["text"] == "no code lane configured"


def test_unknown_chat_ignored(telegram: Telegram, store: Store, fake: FakeHTTP) -> None:
    code = Mock()
    telegram.dispatch(callback("rq:AO-1:AO-1-r1", chat=999), store, code)
    assert fake.calls == [] and code.mock_calls == []


def test_callback_error_answered_once(telegram: Telegram, store: Store, fake: FakeHTTP) -> None:
    code = Mock()
    code.requeue.side_effect = TransitionError("Forbidden transition done -> ready")
    telegram.dispatch(callback("rq:AO-1:AO-1-r1"), store, code)
    answers = fake.method("answerCallbackQuery")
    assert len(answers) == 1 and answers[0][2]["text"] == "Forbidden transition done -> ready"


def test_disabled(settings: Settings, fake: FakeHTTP, store: Store) -> None:
    settings.TELEGRAM_BOT_TOKEN = ""
    telegram = Telegram(settings, httpx.Client(transport=httpx.MockTransport(fake.handle)))
    assert telegram.send("-200", "hello") is None
    telegram.edit("-200", 1, "text")
    assert telegram.start(threading.Event(), store, None) is None
    assert fake.calls == []


def test_http_error_is_logged_and_swallowed(telegram: Telegram, fake: FakeHTTP, caplog: pytest.LogCaptureFixture) -> None:
    fake.telegram_error = True
    assert telegram.send("-200", "hello") is None
    telegram.edit("-200", 1, "hello")
    assert "request failed" in caplog.text and "fake-token" not in caplog.text


def test_long_poll_continues_and_offsets(settings: Settings, store: Store) -> None:
    stop = threading.Event()
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(500)
        if len(requests) == 2:
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 7}]})
        stop.set()
        return httpx.Response(200, json={"ok": True, "result": []})

    telegram = Telegram(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    telegram.loop(stop, store, None)
    assert requests[-1] == {"timeout": 50, "offset": 8, "allowed_updates": ["callback_query", "message"]}


# -- agents that talk, wave 1 (U5): a text reply to an operator-question notice ----

def operator_question(store: Store) -> dict[str, Any]:
    task = store.create_task(repo="repo-tg", title="t", brief="b", acceptance=["true"])
    store.ready_task(task["id"])
    run = store.claim_task(task["id"], worker="w", worker_host="mac", lease_seconds=600,
                           expected_spec_hash=task["spec_hash"],
                           effective_acceptance=task["acceptance"], acceptance_source="task")
    store.start_run(run["id"], run["claim_token"], base_commit="a", branch="b", worktree="/x")
    question = store.create_intent(task["id"], run["id"], run["claim_token"], kind="request",
                                   to_task_id="operator", body="which branch?")
    assert store.mark_intent_notified(question["id"], "operator", chat_id="-200", message_id=55)
    return question


def text_update(update_id: int, text: str, *, chat: int = -200, reply_to: int | None = 55) -> dict[str, Any]:
    message: dict[str, Any] = {"message_id": update_id + 100, "chat": {"id": chat}, "text": text,
                               "from": {"id": 1, "first_name": "the operator"}}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to, "chat": {"id": chat}}
    return {"update_id": update_id, "message": message}


def run_loop(settings: Settings, store: Store, updates: list[dict[str, Any]]) -> list[Any]:
    stop = threading.Event()
    calls: list[Any] = []

    def handle(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        calls.append((request.url.path.rsplit("/", 1)[-1], body))
        if request.url.path.endswith("getUpdates"):
            if any(path == "getUpdates" for path, _ in calls[:-1]):
                stop.set()
                return httpx.Response(200, json={"ok": True, "result": []})
            return httpx.Response(200, json={"ok": True, "result": updates})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 900 + len(calls)}})

    Telegram(settings, httpx.Client(transport=httpx.MockTransport(handle))).loop(stop, store, None)
    return [body for path, body in calls if path == "sendMessage"]


def test_reply_to_the_notice_answers_the_intent_once(settings: Settings, store: Store) -> None:
    question = operator_question(store)
    sent = run_loop(settings, store, [text_update(1, "main, please")])
    answered = store.get_intent(question["id"])
    assert answered["state"] == "answered"
    answer = [row for row in store.list_intents(question["task_id"]) if row["reply_to"] == question["id"]]
    assert len(answer) == 1 and answer[0]["body"] == "main, please" and answer[0]["task_id"] == "operator"
    assert len(sent) == 1 and sent[0]["chat_id"] == "-200"
    assert sent[0]["text"] == f"answered {question['id']} ({answer[0]['id']})"
    # a second reply to the same notice: the request is already answered; one refusal line, no new row
    sent = run_loop(settings, store, [text_update(2, "actually develop")])
    assert len([row for row in store.list_intents(question["task_id"]) if row["reply_to"] == question["id"]]) == 1
    assert len(sent) == 1 and sent[0]["text"] == f"{question['id']} not answered: cannot answer a answered request"


def test_other_chat_other_message_and_plain_text_are_ignored(settings: Settings, store: Store) -> None:
    question = operator_question(store)
    sent = run_loop(settings, store, [
        text_update(1, "from the wrong chat", chat=-999),
        text_update(2, "reply to something else", reply_to=56),
        text_update(3, "no reply at all", reply_to=None),
        text_update(4, "   ", reply_to=55),
        {"update_id": 5, "message": {"message_id": 105, "chat": {"id": -200}, "sticker": {"file_id": "x"},
                                     "reply_to_message": {"message_id": 55}}},
    ])
    assert store.get_intent(question["id"])["state"] == "stored" and sent == []
    assert len(store.list_intents(question["task_id"])) == 1


def test_forged_frame_in_a_reply_is_refused_with_one_line(settings: Settings, store: Store) -> None:
    question = operator_question(store)
    sent = run_loop(settings, store, [text_update(1, "see <agent-request> no")])
    assert store.get_intent(question["id"])["state"] == "stored"
    assert len(sent) == 1 and sent[0]["text"] == "not sent: body cannot contain the frame tag 'agent-request'"


def test_callback_path_still_dispatches_beside_text_replies(settings: Settings, store: Store,
                                                            fake: FakeHTTP) -> None:
    code = Mock()
    stop = threading.Event()
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        seen.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("getUpdates"):
            if seen.count("getUpdates") > 1:
                stop.set()
                return httpx.Response(200, json={"ok": True, "result": []})
            assert body["allowed_updates"] == ["callback_query", "message"]
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 1, "callback_query": callback("rq:AO-1:AO-1-r1")}]})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    Telegram(settings, httpx.Client(transport=httpx.MockTransport(handle))).loop(stop, store, code)
    code.requeue.assert_called_once_with("AO-1", run_id="AO-1-r1")
    assert "answerCallbackQuery" in seen


def test_not_modified_edit_is_not_a_failure(settings: Settings, caplog) -> None:
    import httpx
    from agent_os.telegram import Telegram

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "error_code": 400,
                                         "description": "Bad Request: message is not modified"})

    tg = Telegram(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    with caplog.at_level("WARNING"):
        tg.edit("-200", 1, "same text")
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


# -- render_code integration line (parallel step 3, unit U3) ------------------

def _code_run(**overrides: Any) -> dict[str, Any]:
    run = {"id": "r1", "verdict": "pass", "pr_head_sha": "c" * 40, "result_commit": "c" * 40,
           "pr_number": 5, "pr_url": "https://github.test/o/r/pull/5", "report": "did it",
           "integration": None}
    run.update(overrides)
    return run


def test_render_code_integration_passed_line_names_base_and_sha() -> None:
    task = {"id": "AO-9", "title": "Fix"}
    run = _code_run(integration={"head_sha": "c" * 40, "status": "passed",
                                 "base_branch": "main", "base_sha": "b" * 40})
    text = render_code(task, run, pr_state="opened")
    assert f"integration: passed onto main {hash8('b' * 40)}" in text.splitlines()


@pytest.mark.parametrize("status", ["failed", "conflict", "error"])
def test_render_code_integration_other_statuses_render_bare_status(status: str) -> None:
    task = {"id": "AO-9", "title": "Fix"}
    run = _code_run(integration={"head_sha": "c" * 40, "status": status,
                                 "base_branch": "main", "base_sha": "b" * 40})
    text = render_code(task, run, pr_state="opened")
    assert f"integration: {status}" in text.splitlines()


def test_render_code_integration_none_when_no_check() -> None:
    task = {"id": "AO-9", "title": "Fix"}
    run = _code_run(integration=None)
    text = render_code(task, run, pr_state="opened")
    assert "integration: none, run aos integrate AO-9" in text.splitlines()


def test_render_code_integration_none_when_head_sha_differs_from_pr_head() -> None:
    task = {"id": "AO-9", "title": "Fix"}
    run = _code_run(integration={"head_sha": "d" * 40, "status": "passed",
                                 "base_branch": "main", "base_sha": "b" * 40})
    text = render_code(task, run, pr_state="opened")
    assert "integration: none, run aos integrate AO-9" in text.splitlines()


# -- the branch URL on the card (task-flow Part C) --------------------------------

@pytest.mark.parametrize("remote, expected", [
    ("git@github.com:o/r.git", "https://github.com/o/r/tree/ao/AO-9"),
    ("ssh://git@github.com/o/r.git", "https://github.com/o/r/tree/ao/AO-9"),
    ("https://github.com/o/r.git", "https://github.com/o/r/tree/ao/AO-9"),
    ("https://github.com/o/r", "https://github.com/o/r/tree/ao/AO-9"),
    ("https://gitlab.com/o/r.git", None),
    ("/srv/git/r.git", None),
    (None, None),
])
def test_branch_url_for_github_remotes_only(remote: str | None, expected: str | None) -> None:
    assert branch_url(remote, "AO-9") == expected


@pytest.mark.parametrize("pr_state", ["opened", "pending", "off"])
def test_render_code_branch_line_follows_the_run_line_for_a_github_remote(pr_state: str) -> None:
    task = {"id": "AO-9", "title": "Fix"}
    lines = render_code(task, _code_run(remote="git@github.com:o/r.git"), pr_state=pr_state).splitlines()
    assert lines[1].startswith("run r1 · verdict pass")
    assert lines[2] == "branch https://github.com/o/r/tree/ao/AO-9"
    plain = render_code(task, _code_run(remote=None), pr_state=pr_state)
    assert "branch " not in plain
    other = render_code(task, _code_run(remote="https://gitlab.com/o/r.git"), pr_state=pr_state)
    assert "branch " not in other


@pytest.mark.parametrize("pr_state", ["pending", "off"])
def test_render_code_no_integration_line_unless_pr_opened(pr_state: str) -> None:
    task = {"id": "AO-9", "title": "Fix"}
    run = _code_run(integration={"head_sha": "c" * 40, "status": "passed",
                                 "base_branch": "main", "base_sha": "b" * 40})
    text = render_code(task, run, pr_state=pr_state)
    assert "integration:" not in text


# -- U6: evidence warning on the code card ---------------------------------------

def test_render_code_warns_on_flagged_evidence() -> None:
    task = {"id": "AO-9", "title": "Fix"}
    run = _code_run(evidence={"flags": ["first flag", "second", "third"]})
    text = render_code(task, run, pr_state="opened")
    assert "⚠️ evidence flagged: first flag (+2 more)" in text.splitlines()
    one = render_code(task, _code_run(evidence={"flags": ["only"]}), pr_state="opened")
    assert "⚠️ evidence flagged: only" in one.splitlines()


@pytest.mark.parametrize("evidence", [None, {"flags": []}, {"file_count": 2, "flags": []}])
def test_render_code_no_warning_for_a_clean_run(evidence: Any) -> None:
    task = {"id": "AO-9", "title": "Fix"}
    text = render_code(task, _code_run(evidence=evidence), pr_state="opened")
    assert "evidence flagged" not in text
