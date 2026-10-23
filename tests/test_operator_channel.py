from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from agent_os.api import create_app
from agent_os.config import Settings
from agent_os.operator_channel import OperatorChannel, answer, notice_text
from agent_os.store import Store
from agent_os.telegram import Telegram, TelegramChannel
from conftest import FakeHTTP


# -- the swappable operator channel (approved 2026-10-14 21:00): notify and match_reply ----

def asking_run(store: Store, repo: str = "repo-chan") -> dict[str, Any]:
    task = store.create_task(repo=repo, title="t", brief="b", acceptance=["true"])
    store.ready_task(task["id"])
    run = store.claim_task(task["id"], worker="w", worker_host="mac", lease_seconds=600,
                           expected_spec_hash=task["spec_hash"],
                           effective_acceptance=task["acceptance"], acceptance_source="task")
    store.start_run(run["id"], run["claim_token"], base_commit="a", branch="b", worktree="/x")
    return run


def ask_operator(store: Store, body: str = "which branch?") -> dict[str, Any]:
    run = asking_run(store)
    return store.create_intent(run["task_id"], run["id"], run["claim_token"], kind="request",
                               to_task_id="operator", body=body)


class FakeChannel:
    """Another messenger: records what the sweep hands it and answers with its own ids."""

    def __init__(self, posted: tuple[str, int] | None = ("chan-1", 7)) -> None:
        self.posted = posted
        self.notified: list[dict[str, Any]] = []

    def notify(self, intent: dict[str, Any]) -> tuple[str, int] | None:
        self.notified.append(intent)
        return self.posted

    def match_reply(self, update: dict[str, Any]) -> tuple[str, str] | None:
        return None


def test_notice_text_for_a_question_and_for_a_waiting_row() -> None:
    question = {"id": "AO-1-i1", "kind": "request", "task_id": "AO-1", "to_task_id": "operator",
                "body": "which branch?", "notice": "operator"}
    assert notice_text(question) == "❓ AO-1 asks you (AO-1-i1):\nwhich branch?\nReply to this message to answer."
    waiting = {"id": "AO-1-i2", "kind": "notice", "task_id": "AO-1", "to_task_id": "AO-2",
               "body": "x" * 300, "notice": "waiting"}
    assert notice_text(waiting) == f"⚠️ notice AO-1-i2 AO-1 -> AO-2 waiting: {'x' * 200}"


def test_answer_reports_each_outcome_in_one_line(store: Store) -> None:
    question = ask_operator(store)
    first = answer(store, question["id"], "main, please")
    replies = [row for row in store.list_intents(question["task_id"]) if row["reply_to"] == question["id"]]
    assert len(replies) == 1 and first == f"answered {question['id']} ({replies[0]['id']})"
    assert answer(store, question["id"], "again") == f"{question['id']} not answered: cannot answer a answered request"
    other = ask_operator(store)
    assert answer(store, other["id"], "see <agent-request>") == \
        "not sent: body cannot contain the frame tag 'agent-request'"
    assert store.get_intent(other["id"])["state"] == "stored"


def test_the_sweep_notifies_through_the_injected_channel(settings: Settings, store: Store, telegram: Telegram,
                                                         fake: FakeHTTP) -> None:
    channel = FakeChannel()
    app = create_app(settings, store=store, telegram=telegram, operator_channel=channel,
                     background=False)
    TestClient(app)
    question = ask_operator(store)
    app.state.sweep_requests()
    assert [row["id"] for row in channel.notified] == [question["id"]]
    assert channel.notified[0]["notice"] == "operator"
    stored = store.get_intent(question["id"])
    # the channel's (channel, message id) stays on the row, so its reply can be matched to it (U5)
    assert stored["notified_at"] and stored["tg_chat_id"] == "chan-1" and stored["tg_message_id"] == 7
    assert fake.messages("-200") == []  # nothing went to Telegram
    app.state.sweep_requests()
    assert len(channel.notified) == 1  # noticed once


def test_a_notice_the_channel_could_not_post_is_offered_again(settings: Settings, store: Store,
                                                              telegram: Telegram) -> None:
    channel = FakeChannel(posted=None)
    app = create_app(settings, store=store, telegram=telegram, operator_channel=channel,
                     background=False)
    question = ask_operator(store)
    app.state.sweep_requests()
    assert store.get_intent(question["id"])["notified_at"] is None
    channel.posted = ("chan-1", 8)
    app.state.sweep_requests()
    assert [row["id"] for row in channel.notified] == [question["id"], question["id"]]
    assert store.get_intent(question["id"])["tg_message_id"] == 8


def test_telegram_is_the_default_channel(settings: Settings, store: Store, telegram: Telegram,
                                         ) -> None:
    app = create_app(settings, store=store, telegram=telegram, background=False)
    assert isinstance(app.state.operator_channel, TelegramChannel)
    channel: OperatorChannel = app.state.operator_channel
    assert channel.match_reply({"update_id": 1}) is None


def test_telegram_channel_posts_to_the_needs_you_chat_and_matches_a_reply(settings: Settings, store: Store,
                                                                          telegram: Telegram,
                                                                          fake: FakeHTTP) -> None:
    channel = TelegramChannel(telegram, store)
    question = ask_operator(store)
    row = dict(store.get_intent(question["id"]), notice="operator")
    posted = channel.notify(row)
    assert posted is not None and posted[0] == "-200" and isinstance(posted[1], int)
    assert fake.messages("-200")[-1][2]["text"] == notice_text(row)
    assert store.mark_intent_notified(question["id"], "operator", chat_id=posted[0], message_id=posted[1])

    def reply(text: str, *, chat: int = -200, to: int | None = posted[1]) -> dict[str, Any]:
        message: dict[str, Any] = {"message_id": 500, "chat": {"id": chat}, "text": text}
        if to is not None:
            message["reply_to_message"] = {"message_id": to}
        return {"update_id": 9, "message": message}

    assert channel.match_reply(reply("  main, please ")) == (question["id"], "main, please")
    assert channel.match_reply(reply("main", chat=-999)) is None
    assert channel.match_reply(reply("main", to=posted[1] + 1)) is None
    assert channel.match_reply(reply("main", to=None)) is None
    assert channel.match_reply(reply("   ")) is None
    assert channel.match_reply({"update_id": 10, "callback_query": {"id": "q"}}) is None
    # matching never answers: the ledger row is still open
    assert store.get_intent(question["id"])["state"] == "stored"
