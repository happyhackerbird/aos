"""The operator's channel (agents that talk, approved 2026-10-14 21:00): how the bus tells the
operator about a ledger row, and how her answer comes back to it.

Two methods make a channel. ``notify`` posts the notice for a row the request sweep found;
``match_reply`` turns an inbound update of that messenger into the row it answers and the
answer's text. Telegram is the one implementation (``telegram.TelegramChannel``); another
messenger is one more class with these two methods, plus its own loop that hands its updates to
``match_reply`` and sends ``answer``'s line back. The agent side, the ledger (``task_intents``)
and the operator's bearer route are the same whichever channel is used.
"""
from __future__ import annotations

from typing import Any, Protocol

from agent_os.store import Store, TransitionError


class OperatorChannel(Protocol):
    def notify(self, intent: dict[str, Any]) -> tuple[str, int] | None:
        """Post the notice for a swept row, whose ``notice`` key says why (``operator``,
        ``waiting``, ``undeliverable``, ``expired``). Returns the (channel, message id) it was
        posted as, which the ledger keeps for an ``operator`` row so a reply to it can be
        matched, or None when nothing was posted; the sweep offers that row again next tick."""
        ...

    def match_reply(self, update: dict[str, Any]) -> tuple[str, str] | None:
        """The (intent id, text) an inbound update answers, or None when it is not a reply to
        one of this channel's notices. Matching records nothing; ``answer`` does."""
        ...


def notice_text(intent: dict[str, Any]) -> str:
    """The notice for a swept row: a question to the operator, or a row nobody read in time."""
    if intent["notice"] == "operator":
        return (f"❓ {intent['task_id']} asks you ({intent['id']}):\n{intent['body'][:1500]}\n"
                "Reply to this message to answer.")
    # The row's kind: a finish notice nobody read reaches the operator as a notice (lane F, G1).
    return (f"⚠️ {intent['kind']} {intent['id']} {intent['task_id']} -> {intent['to_task_id']} "
            f"{intent['notice']}: {intent['body'][:200]}")


def answer(store: Store, intent_id: str, text: str) -> str:
    """The operator's answer to ``intent_id`` through the ledger, as on her bearer route, and the
    one line that tells her what happened."""
    try:
        row = store.create_operator_intent(reply_to=intent_id, body=text)
    except TransitionError as exc:
        return f"{intent_id} not answered: {exc}"
    except ValueError as exc:
        return f"not sent: {exc}"
    return f"answered {intent_id} ({row['id']})"
