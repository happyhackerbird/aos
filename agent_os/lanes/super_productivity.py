"""The Super Productivity source lane: the operator's tasks from the app's sync files onto the bus.

A reader only; it never writes a sync file. Mapping (task-flow directive, Part A):

    external id   the app's task id, kept as source="sp" / external_id on the bus task (the dedupe key)
    repo          a `repo: <name>` line in the notes (or `repo:` inside `## AOS`; the first in
                  document order wins), which must be a registered repo; without such a line,
                  exactly one tag whose title is a registered repo name
    title         the task title
    brief         the notes without the `## Acceptance` and `## AOS` sections and the `repo:` line
                  (the task title when nothing else is left), subtask titles appended
    acceptance    the explicit `## Acceptance` lines (list markers and backticks stripped);
                  repository defaults are resolved by the worker before claiming
    harness, host, tier   `## AOS` lines `harness:`, `host:`, `tier:` (0 to 2; 3 is ignored with a
                  notice); defaults claude, the repo's host, 1
    readiness     released at the next poll; with AOS_SP_READY_TAG set, only a task carrying that
                  tag; a task carrying the AOS_SP_HOLD_TAG tag is never released, and goes back
                  to backlog if it was ready before a claim
    cancellation  done or gone in the app while the task is backlog or ready

Before a claim the bus follows the app; from the claim on the app's state is ignored. Every poll
that changed something is one line in the bus log and one Telegram notice on the code chat; the
lists of tasks held back (no repo, tier 3, not released) are notified when they
change.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Callable

import httpx

from agent_os import sp_sync
from agent_os.config import Settings
from agent_os.store import HARNESSES, HOSTS, Store, TransitionError, normalize_repo, utcnow
from agent_os.telegram import Telegram

logger = logging.getLogger("agent_os.lanes.super_productivity")
LANE = "sp.poll"
SOURCE = "sp"
# meta key: the held-back lists as last notified, so a notice goes out only when they change
NOTICE_LISTS = "sp_notice_lists"
CHANGES = ("created", "updated", "released", "unreleased", "cancelled")
LISTS = ("no_repo", "tier3", "not_released")
LABELS = {
    "created": "created", "updated": "updated", "released": "released", "unreleased": "unreleased",
    "cancelled": "cancelled", "no_repo": "no repo (not created)",
    "tier3": "tier 3 ignored (tier 1 used)",
    "not_released": "not released",
}
OPEN = ("backlog", "ready")  # the app is followed only while the task is here
_HEADING = re.compile(r"^##\s+(.+?)\s*$")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_REPO_LINE = re.compile(r"^\s*(?:[-*+]\s+)?repo\s*:\s*(.*?)\s*$", re.IGNORECASE)


def parse_notes(notes: str) -> tuple[str, list[str], dict[str, str]]:
    """(brief, acceptance commands, `## AOS` keys) from the task notes.

    `## Acceptance` holds one command per line, list markers and surrounding backticks
    stripped, fence lines skipped. `## AOS` holds `key: value` lines. Any other heading
    stays in the brief. Outside those sections and outside a ``` fence, a `repo: <name>`
    line is removed from the brief and recorded as aos["repo"]; the first non-empty repo
    in document order wins, whether from such a line or from `## AOS`."""
    brief_lines: list[str] = []
    acceptance: list[str] = []
    aos: dict[str, str] = {}
    section: str | None = None
    in_fence = False
    for line in notes.splitlines():
        if section is None and line.strip().startswith("```"):
            in_fence = not in_fence
            brief_lines.append(line)
            continue
        if in_fence:
            brief_lines.append(line)
            continue
        heading = _HEADING.match(line)
        if heading:
            name = heading.group(1).strip().lower()
            if name in ("acceptance", "aos"):
                section = name
                continue
            section = None
        if section == "acceptance":
            text = _LIST_MARKER.sub("", line, count=1).strip()
            if text.startswith("```"):
                continue
            text = text.strip("`").strip()
            if text:
                acceptance.append(text)
        elif section == "aos":
            key, separator, value = _LIST_MARKER.sub("", line, count=1).partition(":")
            name = key.strip().lower()
            if separator and name == "repo":
                if value.strip() and not aos.get("repo"):
                    aos["repo"] = value.strip()
            elif separator and name:
                aos[name] = value.strip()
        else:
            match = _REPO_LINE.match(line)
            if match:
                if match.group(1) and not aos.get("repo"):
                    aos["repo"] = match.group(1)
            else:
                brief_lines.append(line)
    return "\n".join(brief_lines).strip(), acceptance, aos


def new_outcome() -> dict[str, Any]:
    outcome: dict[str, Any] = {key: [] for key in CHANGES + LISTS}
    outcome["error"] = None
    return outcome


def _label(task: dict[str, Any]) -> str:
    return f"{task['id']} {task['title']}"


class SuperProductivityLane:
    def __init__(self, store: Store, telegram: Telegram, settings: Settings, *,
                 fetch: Callable[[], dict[str, Any]] | None = None,
                 client: httpx.Client | None = None) -> None:
        self.store, self.telegram, self.settings = store, telegram, settings
        self.lock = threading.RLock()
        self._fetch = fetch
        self._client = client
        self._dropbox: sp_sync.Dropbox | None = None

    # ── reading ────────────────────────────────────────────────────────
    def fetch_state(self) -> dict[str, Any]:
        if self._fetch is not None:
            return self._fetch()
        settings = self.settings
        if self._dropbox is None:
            self._dropbox = sp_sync.Dropbox(
                settings.AOS_SP_DROPBOX_APP_KEY, settings.AOS_SP_DROPBOX_APP_SECRET,
                settings.AOS_SP_DROPBOX_REFRESH_TOKEN, client=self._client,
                api_base=settings.AOS_SP_DROPBOX_API_BASE, content_base=settings.AOS_SP_DROPBOX_CONTENT_BASE)
        return sp_sync.fetch_state(self._dropbox, settings.AOS_SP_DROPBOX_PATH,
                                   settings.AOS_SP_SYNC_PASSWORD or None)

    def poll(self) -> dict[str, Any]:
        """One read of the sync files applied to the bus; the outcome lists what changed."""
        started = utcnow()
        outcome = new_outcome()
        with self.lock:
            try:
                state = self.fetch_state()
                self.apply(state, outcome)
                self.notify(outcome)
                result = "ok"
            except Exception as exc:
                outcome["error"] = str(exc)
                result = f"error:{exc}"
                logger.warning("Super Productivity poll failed: %s", exc)
            finally:
                receipt = {key: len(outcome[key]) for key in CHANGES + LISTS}
                self.store.record_run(LANE, started, utcnow(), result, receipt=receipt)
        return outcome

    # ── mapping ────────────────────────────────────────────────────────
    def map_record(self, record: dict[str, Any], repos: dict[str, dict[str, Any]]
                   ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """The bus spec of an app task, or None with the reason it cannot be created."""
        brief, acceptance, aos = parse_notes(record["notes"])
        if aos.get("repo"):
            if normalize_repo(aos["repo"]) not in repos:
                return None, {"no_repo": f"unknown repo '{aos['repo']}' in the notes"}
            repo = repos[normalize_repo(aos["repo"])]
        else:
            repo_tags = [tag for tag in record["tags"] if tag.strip() and normalize_repo(tag) in repos]
            if len(repo_tags) != 1:
                reason = ("no repo: add a line 'repo: <name>' to the notes" if not repo_tags
                          else f"{len(repo_tags)} repo tags: {', '.join(repo_tags)}")
                return None, {"no_repo": reason}
            repo = repos[normalize_repo(repo_tags[0])]
        if not brief.strip():
            brief = record["title"].strip()
        if record["subtasks"]:
            brief = (brief + "\n\nSubtasks:\n" + "\n".join(f"- {title}" for title in record["subtasks"])).strip()
        reasons: dict[str, Any] = {}
        tier = 1
        if "tier" in aos:
            try:
                wanted = int(aos["tier"])
            except ValueError:
                wanted = None
            if wanted == 3:
                reasons["tier3"] = True
            elif wanted in (0, 1, 2):
                tier = wanted
        harness = aos.get("harness", "").strip().lower()
        host = aos.get("host", "").strip().lower()
        execution = aos.get("execution", "").strip().lower()
        spec = {
            "repo": repo["name"], "title": record["title"].strip() or "(untitled)", "brief": brief,
            "acceptance": acceptance, "tier": tier,
            "harness": harness if harness in HARNESSES else "claude",
            "host": host if host in HOSTS else None,
            "execution": "interactive" if execution == "interactive" else "headless",
        }
        return spec, reasons

    # ── bus writes ─────────────────────────────────────────────────────
    def apply(self, state: dict[str, Any], outcome: dict[str, Any]) -> None:
        settings = self.settings
        scoped = sp_sync.extract_tasks(state, project=settings.AOS_SP_PROJECT or None,
                                       tag=settings.AOS_SP_TAG or None)
        # Every id the app still holds, nested ones included: a task dragged under another task
        # is not gone, it is out of the lane's reach until it is top-level again (like a task
        # moved out of the project), and its bus task waits as it is.
        present = set(sp_sync.task_ids(state))
        repos = {repo["name"]: repo for repo in self.store.repos()}
        on_bus = {task["external_id"]: task for task in self.store.list_tasks(source=SOURCE)}
        for record in scoped:
            self.sync_record(record, repos, on_bus.get(record["id"]), outcome)
        for external_id, task in on_bus.items():
            if external_id not in present and task["status"] in OPEN:
                self.store.cancel_task(task["id"])
                outcome["cancelled"].append(f"{_label(task)} (gone from the app)")

    def sync_record(self, record: dict[str, Any], repos: dict[str, dict[str, Any]],
                    task: dict[str, Any] | None, outcome: dict[str, Any]) -> None:
        spec, reasons = self.map_record(record, repos)
        ready_tag, hold_tag = self.settings.AOS_SP_READY_TAG, self.settings.AOS_SP_HOLD_TAG.strip().lower()
        held = bool(hold_tag) and any(tag.strip().lower() == hold_tag for tag in record["tags"])
        wants_ready = (not ready_tag or ready_tag in record["tags"]) and not held
        if task is None:
            if record["is_done"]:
                return
            if spec is None:
                outcome["no_repo"].append(f"{record['title']} ({reasons['no_repo']})")
                return
            task = self.store.create_task(source=SOURCE, external_id=record["id"], **spec)
            outcome["created"].append(_label(task))
        else:
            if task["status"] not in OPEN:
                return  # claimed or beyond: the bus owns the task now
            if record["is_done"]:
                self.store.cancel_task(task["id"])
                outcome["cancelled"].append(f"{_label(task)} (done in the app)")
                return
            if spec is None:
                outcome["no_repo"].append(f"{record['title']} ({reasons['no_repo']})")
                return
            changes = {key: value for key, value in spec.items() if value is not None and task.get(key) != value}
            if changes:
                if task["status"] == "ready" and not spec["brief"].strip():
                    task = self.store.unready_task(task["id"])
                    outcome["unreleased"].append(_label(task))
                task = self.store.edit_task(task["id"], **changes)
                outcome["updated"].append(_label(task))
        label = _label(task)
        if reasons.get("tier3"):
            outcome["tier3"].append(label)
        if wants_ready:
            if task["status"] == "backlog":
                try:
                    self.store.ready_task(task["id"])
                    outcome["released"].append(label)
                except TransitionError as exc:
                    outcome["not_released"].append(f"{label}: {exc}")
        elif task["status"] == "ready":
            self.store.unready_task(task["id"])
            outcome["unreleased"].append(label)

    # ── notices ────────────────────────────────────────────────────────
    def notify(self, outcome: dict[str, Any]) -> None:
        changed = any(outcome[key] for key in CHANGES)
        lists = {key: outcome[key] for key in LISTS}
        stored = json.loads(self.store.meta_get(NOTICE_LISTS) or "{}")
        previous = {key: stored.get(key, []) for key in LISTS}
        if lists != previous:
            self.store.meta_set(NOTICE_LISTS, json.dumps(lists))
        if not changed and lists == previous:
            return
        summary = ", ".join(f"{len(outcome[key])} {key}" for key in CHANGES if outcome[key]) or "nothing changed on the bus"
        lines = [f"📥 Super Productivity: {summary}"]
        for key in CHANGES + LISTS:
            lines.extend(f"{LABELS[key]}: {entry}" for entry in outcome[key])
        self.telegram.send(self.settings.code_chat, "\n".join(lines))
