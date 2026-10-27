from __future__ import annotations

import logging
import threading
from typing import Any, Protocol

import httpx

from agent_os.config import Settings
from agent_os.evidence import flags_of
from agent_os.github import parse_remote
from agent_os.operator_channel import answer as operator_answer, notice_text
from agent_os.store import Store, TransitionError

logger = logging.getLogger("agent_os.telegram")
TRUNCATION = "\n…(truncated, open the review link)"
Buttons = list[list[dict[str, str]]]


def plain(text: str) -> str:
    return text.replace("\u2014", "-").replace("\u2013", "-")


def truncate(text: str) -> str:
    text = plain(text)
    return text if len(text) <= 4000 else text[:4000 - len(TRUNCATION)] + TRUNCATION


def hash8(digest: str | None) -> str:
    return (digest or "").removeprefix("sha256:")[:8]


def branch_url(remote: str | None, task_id: str) -> str | None:
    """The GitHub page of the run's branch `ao/<task>` when the run's remote parses as GitHub, else None
    (task-flow Part C: the runner posts `remote` once it has pushed the branch)."""
    parsed = parse_remote(remote)
    if parsed is None:
        return None
    owner, repo = parsed
    return f"https://github.com/{owner}/{repo}/tree/ao/{task_id}"


def render_code(task: dict[str, Any], run: dict[str, Any], prefix: str | None = None, *,
                pr_state: str = "off", pr_error: str = "") -> str:
    pr_head_sha, result_commit = run.get("pr_head_sha"), run.get("result_commit")
    sha8 = hash8(pr_head_sha) if pr_head_sha else hash8(result_commit)
    lines = [
        prefix or f"🔀 {task['id']} · {task['title']}",
        f"run {run['id']} · verdict {run.get('verdict')} · {sha8}",
    ]
    branch = branch_url(run.get("remote"), task["id"])
    if branch is not None:
        lines.append(f"branch {branch}")
    if pr_state == "opened":
        lines.append(f"PR #{run.get('pr_number')} {run.get('pr_url')}")
        integration = run.get("integration")
        if integration is not None and integration.get("head_sha") == pr_head_sha:
            status = integration["status"]
            if status == "passed":
                lines.append(f"integration: passed onto {integration['base_branch']} "
                             f"{hash8(integration['base_sha'])}")
            else:
                lines.append(f"integration: {status}")
        else:
            lines.append(f"integration: none, run aos integrate {task['id']}")
    elif pr_state == "pending":
        line = "PR pending (retrying)"
        if pr_error:
            line += f" · {pr_error[:100]}"
        lines.append(line)
    else:
        lines.append("no PR: close with aos accept")
    if pr_head_sha and result_commit and pr_head_sha != result_commit:
        lines.append(f"⚠️ PR head {hash8(pr_head_sha)} is not the acceptance commit {hash8(result_commit)}")
    run_flags = flags_of(run)
    if run_flags:
        warning = f"⚠️ evidence flagged: {run_flags[0]}"
        if len(run_flags) > 1:
            warning += f" (+{len(run_flags) - 1} more)"
        lines.append(warning)
    report = run.get("report") or ""
    status_line = next((line for line in report.splitlines() if line.startswith("Status:")), None)
    if status_line is not None:
        lines.append(status_line)
    lines.append("")
    lines.append(report[:600])
    return truncate("\n".join(lines))


def code_buttons(task: dict[str, Any], run: dict[str, Any]) -> Buttons:
    if task["status"] != "review":
        return []
    row: list[dict[str, str]] = []
    pr_head_sha, result_commit = run.get("pr_head_sha"), run.get("result_commit")
    if run.get("pr_number") is not None and pr_head_sha and pr_head_sha == result_commit:
        merge = {"text": "Merge", "callback_data": f"mg:{run['id']}:{hash8(pr_head_sha)}"}
        if len(merge["callback_data"].encode()) > 64:
            raise ValueError("Telegram callback exceeds 64 bytes")
        row.append(merge)
    requeue = {"text": "Requeue", "callback_data": f"rq:{task['id']}:{run['id']}"}
    if len(requeue["callback_data"].encode()) > 64:
        raise ValueError("Telegram callback exceeds 64 bytes")
    row.append(requeue)
    return [row]


class CodeCallbacks(Protocol):
    """What a code card's buttons call: the code lane (``lanes.code.CodeLane``)."""

    def merge_callback(self, run_id: str, sha8: str) -> dict[str, Any]: ...

    def requeue(self, task_id: str, *, run_id: str | None = None) -> Any: ...


class Telegram:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=60)
        self.enabled = bool(settings.TELEGRAM_BOT_TOKEN)

    def request(self, method: str, body: dict[str, Any]) -> Any:
        if not self.enabled:
            return None
        try:
            response = self.client.post(
                f"{self.settings.TELEGRAM_API_BASE.rstrip('/')}/bot{self.settings.TELEGRAM_BOT_TOKEN}/{method}",
                json=body,
            )
            result = response.json()
            if not result.get("ok"):
                # An edit that changes nothing is a 400 "message is not modified":
                # the card already shows the right thing, so it is not a failure.
                if "not modified" in str(result.get("description", "")).lower():
                    logger.debug("Telegram %s: message not modified", method)
                    return {}
                logger.warning("Telegram %s returned an unsuccessful response (HTTP %s)",
                               method, response.status_code)
                return None
            response.raise_for_status()
            return result.get("result")
        except (httpx.HTTPError, ValueError):
            # Do not log exception URLs: they contain the bot token.
            logger.warning("Telegram %s request failed", method)
            return None

    def send(self, chat_id: str, text: str, buttons: Buttons | None = None) -> int | None:
        body: dict[str, Any] = {"chat_id": chat_id, "text": truncate(text)}
        if buttons is not None:
            body["reply_markup"] = {"inline_keyboard": buttons}
        result = self.request("sendMessage", body)
        return result.get("message_id") if result else None

    def edit(self, chat_id: str, message_id: int, text: str, buttons: Buttons | None = None) -> None:
        self.request("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                     "text": truncate(text), "reply_markup": {"inline_keyboard": buttons or []}})

    def answer_callback(self, callback_query_id: str, text: str) -> None:
        self.request("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": plain(text)})

    def edit_card(self, run: dict[str, Any], text: str, keyboard: Buttons) -> bool:
        if run.get("tg_chat_id") is None or run.get("tg_message_id") is None:
            return False
        result = self.request("editMessageText", {
            "chat_id": run["tg_chat_id"], "message_id": run["tg_message_id"],
            "text": truncate(text), "reply_markup": {"inline_keyboard": keyboard},
        })
        return result is not None

    def dispatch(self, query: dict[str, Any], store: Store, code: CodeCallbacks | None) -> None:
        chat = str(query.get("message", {}).get("chat", {}).get("id", ""))
        chats = {self.settings.TELEGRAM_CHAT_NEEDS_YOU, self.settings.TELEGRAM_CHAT_CODE}
        if chat not in chats or not chat:
            logger.warning("Ignoring callback from unconfigured chat")
            return
        answer = "unknown action"
        try:
            data = query.get("data", "")
            if data.startswith("mg:"):
                run_id, sha8 = data[3:].rsplit(":", 1)
                if code is None:
                    answer = "no code lane configured"
                else:
                    result_task = code.merge_callback(run_id, sha8)
                    newest_passed = next(
                        (r for r in result_task["runs"] if r["status"] == "passed"), None)
                    answer = f"merged {hash8(newest_passed['merge_commit'] if newest_passed else None)}"
            elif data.startswith("rq:"):
                task_id, run_id = data[3:].rsplit(":", 1)
                if code is None:
                    answer = "no code lane configured"
                else:
                    code.requeue(task_id, run_id=run_id)
                    answer = "requeued"
        except TransitionError as exc:
            answer = str(exc)
        except httpx.HTTPError:
            answer = "GitHub unreachable, try again"
        except (KeyError, ValueError):
            answer = "task or callback unavailable"
        except Exception:
            logger.exception("Callback action failed")
            answer = "action failed; check the bus log"
        finally:
            self.answer_callback(query["id"], answer)

    def loop(self, stop: threading.Event, store: Store, code: CodeCallbacks | None) -> None:
        operator = TelegramChannel(self, store)
        offset = 0
        while not stop.is_set():
            updates = self.request("getUpdates", {"timeout": 50, "offset": offset,
                                                   "allowed_updates": ["callback_query", "message"]})
            if updates is None:
                stop.wait(1)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    if "callback_query" in update:
                        self.dispatch(update["callback_query"], store, code)
                    elif "message" in update:
                        match = operator.match_reply(update)
                        if match is not None:
                            self.send(self.settings.TELEGRAM_CHAT_NEEDS_YOU, operator_answer(store, *match))
                except Exception:
                    logger.exception("Malformed Telegram update")

    def start(self, stop: threading.Event, store: Store, code: CodeCallbacks | None) -> threading.Thread | None:
        if not self.enabled:
            return None
        thread = threading.Thread(target=self.loop, args=(stop, store, code), daemon=True,
                                  name="agent-os-telegram")
        thread.start()
        return thread


class TelegramChannel:
    """The operator's channel on Telegram (``operator_channel.OperatorChannel``): notices go to
    the needs-you chat, and a text reply there to one of them is the answer (U5). ``Telegram.loop``
    hands its message updates to ``match_reply``."""

    def __init__(self, telegram: Telegram, store: Store) -> None:
        self.telegram, self.store = telegram, store

    def notify(self, intent: dict[str, Any]) -> tuple[str, int] | None:
        chat = self.telegram.settings.TELEGRAM_CHAT_NEEDS_YOU
        message_id = self.telegram.send(chat, notice_text(intent))
        return None if message_id is None else (chat, message_id)

    def match_reply(self, update: dict[str, Any]) -> tuple[str, str] | None:
        """A text message in the needs-you chat that replies to an operator-question notice
        answers that request (U1 S9). Anything else (another chat, no text, no reply, a reply to
        another card, not a message at all) matches nothing."""
        message = update.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        if not chat or chat != self.telegram.settings.TELEGRAM_CHAT_NEEDS_YOU:
            logger.debug("Ignoring a text message from an unconfigured chat")
            return None
        text = (message.get("text") or "").strip()
        message_id = (message.get("reply_to_message") or {}).get("message_id")
        if not text or message_id is None:
            logger.debug("Ignoring a text message that is not a reply")
            return None
        row = self.store.intent_for_notice(chat, int(message_id))
        return None if row is None else (row["id"], text)
