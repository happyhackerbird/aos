#!/usr/bin/env python3
"""aos-agent: the in-sandbox side of agents that talk (agent-comms U2).

One standard-library file, installed in the harness image as /usr/local/bin/aos-agent.
Entry points: mcp (mode-filtered MCP tools), inbox --hook (automatic delivery),
stop --hook (one finalization opportunity), receiver-start/end --hook, version, summary, and peek.

Honest receipts (SU3): every automatic path -- inbox tool, PostToolUse hook, wait result,
interactive inbox wait -- PREPARES a message into the model's context and reports exactly
that to the bus (`prepared`, with its source). Nothing here acknowledges: the agent's reply
is its explicit act, recorded by the bus. A message is prepared once and leaves the queue,
as a Claude Code user message is shown once at a turn boundary.

It reads the run credential from /sandbox/aos-run.json (or AOS_AGENT_RUN_FILE): run_id,
task_id, claim_token, bus_url. The claim token is never printed, logged or returned.
State is an append-only JSON-lines ledger per run under /tmp/aos-agent (or
AOS_AGENT_STATE_DIR), shared by the server and the hook processes. Wire shapes follow the
"Bound interfaces" block of docs/directives/2026-10-14-agent-comms-u2.md; the MCP framing
follows the MCP specification's stdio transport (newline-delimited JSON-RPC 2.0, nothing
else on stdout).
"""
from __future__ import annotations

import fcntl
import json
import queue
import threading
from collections import Counter
import signal
import socket
from contextlib import contextmanager
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

RUN_FILE_DEFAULT = "/sandbox/aos-run.json"
STATE_DIR_DEFAULT = "/tmp/aos-agent"
NO_CREDENTIAL = "aos-agent: no run credential (/sandbox/aos-run.json)"
HOOK_TIMEOUT = 3        # seconds: a dead bus must not stall the agent's tool calls
HOOK_DEADLINE = 7.0     # seconds from hook start, under the 10 s hook timeout U3 writes (fix lane 1 X5)
TOOL_TIMEOUT = 30
WAIT_MARGIN = 2.0      # a wait call's hold ends this long before the aos_wait budget (AO-23)
WAIT_DEFAULT = 900      # P1: a silent stdio call is cut at the 30-minute idle default; 15 min stays below
WAIT_MAX = 900
LEDGER_BODY_CHARS = 200
LEDGER_LINE_BYTES = 512  # U2 S3: one event per line, each under 512 bytes as encoded (fix lane 1 X8)
PROTOCOL_VERSION_DEFAULT = "2025-06-18"
AOS_AGENT_PROTOCOL = "2.6"
WAIT_POLL = 3.0
WAIT_SLICE = 0.25
FAMILY_TIMEOUT = 5.0
INTERACTIVE_TOOLS = ("aos_inbox_wait", "aos_family", "aos_import", "aos_export_candidate")
WAIT_NOTE = "[aos] Returned by the wait; answer a request with aos_reply."
OPEN_HEADER = "Delivered to you and not yet answered:"
_PENDING: queue.Queue = queue.Queue()
_EOF = threading.Event()
_STDOUT_LOCK = threading.Lock()
ACK_NOTE = "[aos] Delivery is best-effort: a message may be delivered again. Answer a request with aos_reply."
SHA40 = re.compile(r"^[0-9a-f]{40}$")
TASK_ID_RE = re.compile(r"^AO-\d+$")
RUN_ID_RE = re.compile(r"^AO-\d+-r\d+$")
TEXT_FIELDS = ("body", "title", "brief", "constraints", "report")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "aos_ask",
        "description": ("Ask another task (AO-<n>) or the operator (to = \"operator\") a question. "
                        "Returns the request id; use aos_wait to wait for the answer."),
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {"to": {"type": "string", "description": "AO-<n> or \"operator\""},
                                       "text": {"type": "string", "description": "the question"}},
                        "required": ["to", "text"]},
    },
    {
        "name": "aos_reply",
        "description": "Answer a request you received.",
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {"id": {"type": "string", "description": "the request id"},
                                       "text": {"type": "string", "description": "the answer"}},
                        "required": ["id", "text"]},
    },
    {
        "name": "aos_inbox",
        "description": ("Read messages addressed to your task. Reading does not acknowledge them; "
                        "answer a request with aos_reply."),
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
    },
    {
        "name": "aos_wait",
        "description": ("Wait for an answer or unrelated incoming questions/notices (at most 15 minutes). "
                        "An incoming return leaves the original request open; wait again with the same id."),
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {"id": {"type": "string", "description": "the request id aos_ask returned"},
                                       "timeout_s": {"type": "integer", "description": "seconds to wait, 1 to 900 (default 900)"}},
                        "required": ["id"]},
    },
    {
        "name": "aos_declare",
        "description": "Declare what your task changes (scope kinds and operations as in aos scope).",
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {"kind": {"type": "string"}, "key": {"type": "string"},
                                       "operation": {"type": "string"},
                                       "note": {"type": "string", "description": "optional note"}},
                        "required": ["kind", "key", "operation"]},
    },
    {
        "name": "aos_propose",
        "description": ("Propose a task for another run in your own repository (agents that talk, U7). "
                        "It waits in the backlog for the operator unless the repository starts proposals "
                        "ready; when its run finishes, a notice lands in your inbox. Returns the new task id."),
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {"title": {"type": "string", "description": "short title"},
                                       "brief": {"type": "string", "description": "what the task is to do"},
                                       "acceptance": {"type": "array", "minItems": 1,
                                                      "items": {"type": "string"},
                                                      "description": "commands that must pass, run from the repository root"},
                                       "constraints": {"type": "string", "description": "optional constraints"},
                                       "reviews": {"type": "string",
                                                   "description": ("a task id in this repository whose branch the "
                                                                   "proposed task reviews; its run gets that task's "
                                                                   "ao/ branch to read")},
                                       "request_id": {"type": "string", "description": "a stable identity for this proposal, 1-64 characters of letters, digits, `_ . : -`; the same request_id from the same parent returns the task already created"},
                                       "base": {"type": "string", "description": "a 40-character commit you exported with aos_export_candidate; omitted: the run's base commit"}},
                        "required": ["title", "brief", "acceptance"]},
    },
    {
        "name": "aos_submit",
        "description": ("Submit your work for verification (interactive runs): the final report, the "
                        "40-character sha of the commit to check, and the child results you coordinated "
                        "(empty unless you did). AOS then closes this session, checks exactly that commit "
                        "and runs acceptance on it; an identical repeat is accepted again, a different "
                        "second submission is refused."),
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {"report": {"type": "string", "description": "your final report"},
                                       "commit": {"type": "string", "description": "the 40-character commit sha"},
                                       "children": {"type": "array", "default": [],
                                                    "items": {"type": "object", "additionalProperties": False,
                                                              "properties": {"task_id": {"type": "string"},
                                                                             "run_id": {"type": "string"},
                                                                             "commit": {"type": "string"}},
                                                              "required": ["task_id", "run_id", "commit"]},
                                                    "description": "child results: task id, run id and commit each; empty unless you coordinated children"}},
                        "required": ["report", "commit"]},
    },
    {
        "name": "aos_inbox_wait",
        "description": "Wait for a message or a child notice; interactive-only.",
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {"timeout_s": {"type": "integer", "description": "seconds to wait for a message or a child notice, 1 to 900 (default 900); interactive runs only"}},
                        "required": []},
    },
    {
        "name": "aos_family",
        "description": "Read your task family, capacity and child outcomes; interactive-only.",
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {"task_id": {"type": "string", "description": "a child's task id for its full report and error; omitted lists the family"}},
                        "required": []},
    },
    {
        "name": "aos_import",
        "description": "Request import of a child's exact result into your sandbox; interactive-only.",
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {"task_id": {"type": "string"}, "run_id": {"type": "string"},
                                       "commit": {"type": "string"}},
                        "required": ["task_id", "run_id", "commit"]},
    },
    {
        "name": "aos_export_candidate",
        "description": "Request export of a committed candidate for dependent children; interactive-only.",
        "inputSchema": {"type": "object", "additionalProperties": False,
                        "properties": {"commit": {"type": "string"}}, "required": ["commit"]},
    },
]


# Fix lane 1 X3 (review F2): every string this file emits as a tool result, a stderr line or a
# BusError text passes through _scrub, which replaces the run's claim token with ***. A bus 422
# can echo a request body, token included, and the model's context must never carry it.
SECRETS: list[str] = []


def _scrub(text: str) -> str:
    for secret in SECRETS:
        if secret:
            text = text.replace(secret, "***")
    return text


def _alarm_noop(_signum: int, _frame: Any) -> None:
    return None


@contextmanager
def invocation_deadline(deadline: float):
    """Bound the whole operation, including DNS/connect, headers and trickling reads.

    Every bus operation runs on the main thread. Socket inactivity timeouts alone
    cannot bound urllib's header/body readers. A process timer also interrupts those
    readers; nested HTTP timers must never extend an enclosing invocation's timer.
    Once a deadline has run, the process never again holds SIGALRM's default disposition,
    whose late alarm killed the process under parallel load (the H1 run's finding 153,
    and round 1's stress measurement). A finished context's late alarm is a no-op.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("invocation deadline reached")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    live = [True]

    def expired(_signum: int, _frame: Any) -> None:
        if live[0]:
            raise TimeoutError("invocation deadline reached")

    signal.signal(signal.SIGALRM, expired)
    limit = min(remaining, previous_timer[0]) if previous_timer[0] else remaining
    signal.setitimer(signal.ITIMER_REAL, limit)
    try:
        yield
    finally:
        live[0] = False
        signal.setitimer(signal.ITIMER_REAL, 0)
        if callable(previous_handler):
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0]:
                signal.setitimer(signal.ITIMER_REAL,
                                 max(0.000001, previous_timer[0] - (time.monotonic() - started)),
                                 previous_timer[1])
        else:
            signal.signal(signal.SIGALRM, _alarm_noop)


def log(message: str) -> None:
    sys.stderr.write(_scrub(f"aos-agent: {message}\n"))
    sys.stderr.flush()


# -- credential and ledger -----------------------------------------------------------

def load_run() -> dict[str, str] | None:
    """The run credential, or None when the file is missing or malformed."""
    path = os.environ.get("AOS_AGENT_RUN_FILE", RUN_FILE_DEFAULT)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    run = {}
    for key in ("run_id", "task_id", "claim_token", "bus_url"):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            return None
        run[key] = value
    run["execution"] = "interactive" if data.get("execution") == "interactive" else "headless"
    run["bus_url"] = run["bus_url"].rstrip("/")
    if run["claim_token"] not in SECRETS:
        SECRETS.append(run["claim_token"])
    # An interactive run must name its secrets. A headless run's are optional: only a Devin run has one, its worker key,
    # which outlives the run as the claim token does not.
    key = "secrets" if data.get("execution") == "interactive" else "headless_secrets"
    if key == "secrets" or key in data:
        secrets = data.get(key)
        if (not isinstance(secrets, list) or not secrets
                or any(not isinstance(value, str) or not value for value in secrets)):
            return None
        for value in secrets:
            if value not in SECRETS:
                SECRETS.append(value)
    return run


class Ledger:
    """Append-only JSON lines per run; one line per event, each under 512 bytes."""

    def __init__(self, run_id: str) -> None:
        directory = os.environ.get("AOS_AGENT_STATE_DIR", STATE_DIR_DEFAULT)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.path = os.path.join(directory, f"{run_id}.jsonl")

    def append(self, event: dict[str, Any]) -> None:
        # Scrub before encoding, and before any truncation can split a token.
        line = (_scrub(json.dumps(event, separators=(",", ":"), ensure_ascii=False)) + "\n").encode("utf-8")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            original_end = os.lseek(fd, 0, os.SEEK_END)
            if original_end and os.pread(fd, 1, original_end - 1) != b"\n":
                raise OSError("ledger has an incomplete trailing record")
            try:
                if os.write(fd, line) != len(line):
                    raise OSError("incomplete ledger append")
            except BaseException:
                os.ftruncate(fd, original_end)
                raise
        finally:
            os.close(fd)

    def events(self):
        """Stream complete events; no size cap and no retained message bodies."""
        try:
            handle = open(self.path, encoding="utf-8")
        except FileNotFoundError:
            return
        with handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event

    def _bounded_row(self, event: dict[str, Any]) -> None:
        # Bound the ASCII-escaped encoding too, as the landed prepared evidence does.
        event["body"] = _scrub(str(event.get("body") or ""))[:LEDGER_BODY_CHARS]
        while event["body"] and len(json.dumps(event, separators=(",", ":")).encode("utf-8")) + 1 >= LEDGER_LINE_BYTES:
            event["body"] = event["body"][:-1]
        self.append(event)

    def prepared(self, row: dict[str, Any], source: str) -> None:
        """Local evidence that this process wrote the frame into the model's context by
        `source`. It is not an acknowledgement and not proof the model read it."""
        event = {"event": "prepared", "id": row.get("id"), "kind": row.get("kind"), "from": row.get("task_id"),
                 "reply_to": row.get("reply_to"), "source": source, "body": row.get("body")}
        self._bounded_row(event)

    def wait_candidates(self) -> list[str]:
        """Oldest prepared IDs not yet replied to, returned by a wait, or retired.

        The caller's invocation deadline bounds the complete history scan. Bodies in
        this ledger are only previews: recover complete content from the Store.
        """
        prepared: dict[str, None] = {}
        disposed: set[str] = set()
        for event in self.events():
            intent_id = event.get("id")
            if not isinstance(intent_id, str):
                continue
            if event.get("event") == "prepared":
                prepared.setdefault(intent_id, None)
            elif event.get("event") in ("replied", "wait_returned", "wait_retired"):
                disposed.add(intent_id)
        return [value for value in prepared if value not in disposed][:20]



# -- the bus ---------------------------------------------------------------------------

class BusError(Exception):
    pass


class WaitError(Exception):
    """An already framed specific-wait failure, preserving observed payloads."""


def _refuse_protected(field: str, value: Any) -> None:
    if isinstance(value, str):
        if any(secret and secret in value for secret in SECRETS):
            raise ValueError(f"a protected value occurs in the field {field}; refusing")
    elif isinstance(value, list):
        for item in value:
            _refuse_protected(field, item)
    elif isinstance(value, dict):
        for item in value.values():
            _refuse_protected(field, item)


class Bus:
    def __init__(self, run: dict[str, str], timeout: float) -> None:
        self.run, self.timeout = run, timeout
        self.deadline: float | None = None
        self.deliveries: list[dict[str, Any]] | None = None
        self.wait_returned_ids: list[str] = []

    def post(self, path: str, body: dict[str, Any], timeout: float | None = None) -> Any:
        scrubbed = {}
        for key, value in body.items():
            if key == "claim_token":
                scrubbed[key] = value
            elif key in TEXT_FIELDS and isinstance(value, str):
                scrubbed[key] = _scrub(value)
            else:
                if key not in TEXT_FIELDS:
                    _refuse_protected(key, value)
                scrubbed[key] = value
        body = scrubbed
        request = urllib.request.Request(self.run["bus_url"] + path, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        try:
            with invocation_deadline(deadline):
                try:
                    with urllib.request.urlopen(request, timeout=max(0.001, deadline - time.monotonic())) as response:
                        raw = response.read()
                except urllib.error.HTTPError as exc:
                    try:
                        payload = json.loads(exc.read() or b"")
                    except ValueError:
                        payload = None
                    finally:
                        exc.close()
                    detail = payload.get("detail") if isinstance(payload, dict) else None
                    if not isinstance(detail, str):
                        detail = json.dumps(detail) if detail is not None else exc.reason
                    raise BusError(_scrub(f"bus {exc.code}: {detail}")) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            timed_out = isinstance(exc, (socket.timeout, TimeoutError)) or (
                isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (socket.timeout, TimeoutError)))
            if timed_out and time.monotonic() >= deadline:
                # The socket bound and the alarm are armed at the same deadline and either may fire
                # first: both name the deadline, so the outcome does not depend on the race.
                raise BusError("bus unreachable: invocation deadline reached") from None
            reason = getattr(exc, "reason", None) or exc
            raise BusError(_scrub(f"bus unreachable: {reason}")) from None
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise BusError("bus unreachable: response is not JSON") from None

    def credential(self) -> dict[str, str]:
        return {"run_id": self.run["run_id"], "claim_token": self.run["claim_token"]}

    @staticmethod
    def _seg(value: str) -> str:
        """An id as one URL path segment (fix lane 1 X7, review N6): a space, `?` or `#` in an
        id must reach the bus quoted, never split or truncate the path."""
        return urllib.parse.quote(value, safe="")

    def ask(self, to: str, text: str) -> dict[str, Any]:
        return self.post(f"/tasks/{self._seg(self.run['task_id'])}/intents",
                         {**self.credential(), "kind": "request", "to_task_id": to, "body": text})

    def reply(self, intent_id: str, text: str) -> dict[str, Any]:
        return self.post(f"/tasks/{self._seg(self.run['task_id'])}/intents",
                         {**self.credential(), "reply_to": intent_id, "body": text})

    def inbox(self, known_ids: list[str] | None = None, timeout: float | None = None) -> dict[str, Any]:
        data = self.post(f"/runs/{self._seg(self.run['run_id'])}/requests",
                         {"claim_token": self.run["claim_token"], "known_ids": known_ids or []},
                         timeout=timeout)
        if not isinstance(data, dict) or not isinstance(data.get("retained_requests"), list):
            raise BusError("bus unreachable: invalid retained_requests response")
        return data

    def receipt(self, intent_id: str, stage: str, source: str) -> dict[str, Any]:
        _refuse_protected("id", intent_id)
        return self.post(f"/intents/{self._seg(intent_id)}/receipt",
                         {**self.credential(), "stage": stage, "source": source})

    def wait(self, intent_id: str, timeout: int) -> dict[str, Any]:
        _refuse_protected("id", intent_id)
        return self.post(f"/intents/{self._seg(intent_id)}/wait", {**self.credential(), "timeout": timeout},
                         timeout=timeout + WAIT_MARGIN + 1.0)

    def declare(self, kind: str, key: str, operation: str, note: str) -> dict[str, Any]:
        return self.post(f"/tasks/{self._seg(self.run['task_id'])}/scopes",
                         {**self.credential(), "scope_kind": kind, "key": key, "operation": operation, "body": note})

    def propose(self, title: str, brief: str, acceptance: list[str], constraints: str, reviews: str = "",
                request_id: str = "", base: str = "") -> dict[str, Any]:
        body = {**self.credential(), "title": title, "brief": brief, "acceptance": acceptance, "constraints": constraints}
        if reviews:  # lane V: sent only when given
            body["reviews"] = reviews
        if request_id:
            body["request_id"] = request_id
        if base:
            body["base"] = base
        return self.post(f"/tasks/{self._seg(self.run['task_id'])}/proposals", body)

    def family(self, task_id: str | None = None) -> dict[str, Any]:
        body = {"claim_token": self.run["claim_token"]}
        if task_id is not None:
            body["task_id"] = task_id
        return self.post(f"/runs/{self._seg(self.run['run_id'])}/family", body, timeout=FAMILY_TIMEOUT)

    def candidate(self, commit: str) -> dict[str, Any]:
        return self.post(f"/runs/{self._seg(self.run['run_id'])}/candidates",
                         {"claim_token": self.run["claim_token"], "commit": commit})

    def import_result(self, task_id: str, run_id: str, commit: str) -> dict[str, Any]:
        return self.post(f"/runs/{self._seg(self.run['run_id'])}/imports",
                         {"claim_token": self.run["claim_token"], "task_id": task_id,
                          "run_id": run_id, "commit": commit})

    def submit(self, report: str, commit: str, children: list[dict[str, str]]) -> dict[str, Any]:
        return self.post(f"/runs/{self._seg(self.run['run_id'])}/submission",
                         {"claim_token": self.run["claim_token"], "report": report,
                          "commit": commit, "children": children})

    def receiver(self, action: str, owner: str, reason: str) -> dict[str, Any]:
        return self.post(f"/runs/{self._seg(self.run['run_id'])}/receiver",
                         {"claim_token": self.run["claim_token"], "action": action,
                          "owner": owner, "reason": reason})


# -- tools ------------------------------------------------------------------------------

def _text(arguments: dict[str, Any], name: str, *, required: bool = True) -> str:
    value = arguments.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{name}' is required and must be a non-empty string")
    return value


def admission_text(row: dict[str, Any]) -> str:
    if row.get("admission") == "accepted" and not row.get("terminal_reason"):
        return f"{row['id']} accepted for {row.get('to_run_id') or row.get('to_task_id')}"
    return (f"{row['id']} retained {row.get('admission') or 'legacy'}; "
            f"attempted run={row.get('to_run_id')}; reason={row.get('terminal_reason')}; "
            f"terminal_at={row.get('terminal_at')}")


def tool_ask(bus: Bus, ledger: Ledger, arguments: dict[str, Any]) -> str:
    to, text = _text(arguments, "to"), _text(arguments, "text")
    row = bus.ask(to, text)
    return admission_text(row)


def tool_reply(bus: Bus, ledger: Ledger, arguments: dict[str, Any]) -> str:
    intent_id, text = _text(arguments, "id"), _text(arguments, "text")
    _refuse_protected("reply_to", intent_id)
    row = bus.reply(intent_id, text)  # the answer is the explicit act; the bus records it as such
    ledger.append({"event": "replied", "id": intent_id})
    return f"answer recorded for {intent_id}; reply {admission_text(row)}"


def _prepare_rows(bus: Bus, ledger: Ledger, rows: list[dict[str, Any]], source: str) -> tuple[list[str], list[str]]:
    """Keep local evidence before any possibly committed receipt report."""
    texts, reported = [], []
    for row in rows:
        framed = str(row.get("framed") or row.get("body") or "")
        if bus.deliveries is not None:
            bus.deliveries.append(row)
            texts.append(framed)
            continue
        ledger.prepared(row, source)
        if bus.deadline is not None and time.monotonic() >= bus.deadline:
            texts.append(f"{framed}\n(receipt skipped: invocation deadline reached; may be delivered again)")
            continue
        try:
            bus.receipt(row["id"], "prepared", source)
        except (BusError, KeyError) as exc:
            texts.append(f"{framed}\n(receipt failed for {row.get('id')}: {exc}; may be delivered again)")
            continue
        reported.append(row["id"])
        texts.append(framed)
    return texts, reported


def finish_prepared(bus: Bus, ledger: Ledger, rows: list[dict[str, Any]], source: str) -> None:
    """Called only after stdout flush: output, ledger, then best-effort `prepared` receipt.

    A receipt response may be lost after commit. The ledger records that this process
    wrote the frames out, not that the model read them and not the bus's state.
    """
    for row in rows:
        ledger.prepared(row, source)
    for row in rows:
        if bus.deadline is not None and time.monotonic() >= bus.deadline:
            log("receipt skipped: invocation deadline reached; messages may be delivered again")
            break
        try:
            bus.receipt(row["id"], "prepared", source)
        except (BusError, KeyError) as exc:
            log(f"receipt failed: {exc}; messages may be delivered again")


def tool_inbox(bus: Bus, ledger: Ledger, arguments: dict[str, Any]) -> str:
    data = bus.inbox()
    rows = data.get("pending_requests") or [] if isinstance(data, dict) else []
    overlaps = [str(o) for o in (data.get("overlaps") or [])] if isinstance(data, dict) else []
    texts, _ = _prepare_rows(bus, ledger, rows, "inbox_tool")
    parts = texts + overlaps
    text = "\n\n".join(parts) if parts else "inbox empty"
    if bus.deliveries:
        text += "\n\n" + ACK_NOTE
    return text + open_block(ledger)


def tool_wait(bus: Bus, ledger: Ledger, arguments: dict[str, Any]) -> str:
    intent_id = _text(arguments, "id")
    _refuse_protected("id", intent_id)
    raw = arguments.get("timeout_s", WAIT_DEFAULT)
    try:
        budget = int(raw)
    except (TypeError, ValueError):
        raise ValueError("'timeout_s' must be an integer number of seconds")
    budget = max(1, min(budget, WAIT_MAX))
    deadline = time.monotonic() + budget
    source = "caller" if bus.deadline is not None and bus.deadline < deadline else "wait"
    deadline = min(deadline, bus.deadline) if bus.deadline is not None else deadline
    bus.deadline = deadline  # Also bounds post-output best-effort bus work.
    state = "unknown"
    intent: dict[str, Any] = {}
    replies: dict[str, dict[str, Any]] = {}
    pending: dict[str, dict[str, Any]] = {}
    recovered: dict[str, dict[str, Any]] = {}
    in_flight = False

    def control() -> str | None:
        return "eof" if _EOF.is_set() else "superseded" if not _PENDING.empty() else None

    def eligible(row: dict[str, Any]) -> bool:
        return (row.get("admission") == "accepted"
                and row.get("to_run_id") == bus.run["run_id"]
                and row.get("kind") in ("request", "notice")
                and not row.get("terminal_reason")
                and row.get("state") in ("stored", "prepared", "acknowledged"))

    def observed() -> str | None:
        if state == "answered":
            return "answered"
        if intent.get("terminal_reason") or state in ("expired", "undeliverable", "unavailable"):
            return "terminal"
        return "incoming" if pending or recovered else None

    def finish(outcome: str, explanation: str, *, error: bool = False) -> str:
        rows = {key: row for key, row in replies.items() if eligible(row)}
        rows.update({key: row for key, row in pending.items() if key not in rows})
        rows.update({key: row for key, row in recovered.items() if key not in rows})
        header = {"request_id": intent_id, "outcome": outcome, "target_state": state,
                  "deadline_source": source, "remaining_s": max(0.0, deadline - time.monotonic()),
                  "control": control()}
        if error:
            texts = [str(row.get("framed") or row.get("body") or "") for row in rows.values()]
        else:
            texts, _ = _prepare_rows(bus, ledger, list(rows.values()), "wait_tool")
            bus.wait_returned_ids = list(rows)
        texts.extend(admission_text(row) for key, row in replies.items() if key not in rows)
        if observed() == "terminal":
            texts.insert(0, admission_text(intent))
        text = "aos_wait " + json.dumps(header, separators=(",", ":")) + "\n" + explanation
        if texts:
            text += "\n\n" + "\n\n".join(texts)
        if error:
            raise WaitError(text)
        return text

    def read_target() -> None:
        nonlocal state, intent, in_flight
        in_flight = True
        data = bus.wait(intent_id, 0)
        in_flight = False
        intent = data.get("intent") or {}
        state = str(intent.get("state") or "unknown")
        if state == "answered":
            for row in data.get("replies") or []:
                if isinstance(row, dict) and isinstance(row.get("id"), str):
                    replies[row["id"]] = row

    try:
        with invocation_deadline(deadline):
            ledger.append({"event": "tool", "name": "aos_wait"})
            while True:
                current_control = control()
                if current_control:
                    return finish(current_control, "Wait ended before further network work; re-wait with the original id.")
                started = time.monotonic()
                candidates = ledger.wait_candidates()
                read_target()
                # Always read the companion inbox, then stabilize discovery after
                # every complete response. Hook ACKs can land during either read.
                while True:
                    in_flight = True
                    data = bus.inbox(candidates, timeout=WAIT_POLL)
                    in_flight = False
                    retained = {row["id"]: row for row in data["retained_requests"]
                                if isinstance(row, dict) and isinstance(row.get("id"), str)}
                    for row in data.get("pending_requests") or []:
                        if (isinstance(row, dict) and isinstance(row.get("id"), str)
                                and eligible(row) and len(pending) < 20):
                            pending.setdefault(row["id"], row)
                    for value in candidates:
                        row = retained.get(value)
                        if (row is not None and eligible(row) and value not in pending
                                and value not in replies and len(recovered) < 20):
                            recovered.setdefault(value, row)
                    # Capture first: even a retirement write failure must retain
                    # every complete observation in the local-error result.
                    for value in candidates:
                        row = retained.get(value)
                        if row is None or not eligible(row):
                            reason = ("terminal" if row and row.get("terminal_reason")
                                      else "answered" if row and row.get("state") == "answered"
                                      else "unavailable")
                            ledger.append({"event": "wait_retired", "id": value, "reason": reason})
                    fresh = ledger.wait_candidates()
                    captured = replies.keys() | pending.keys() | recovered.keys()
                    candidates = [value for value in fresh if value not in captured][:20 - len(recovered)]
                    if not candidates:
                        break
                if observed() == "incoming":
                    read_target()  # One bounded zero-hold tie recheck.
                outcome = observed()
                if outcome:
                    return finish(outcome, "Observed target and incoming work; the original request id is unchanged.")
                current_control = control()
                if current_control:
                    return finish(current_control, "Wait ended; re-wait with the original id.")
                sleep_until = min(deadline, started + WAIT_POLL)
                while time.monotonic() < sleep_until:
                    current_control = control()
                    if current_control:
                        return finish(current_control, "Wait ended; re-wait with the original id.")
                    time.sleep(min(WAIT_SLICE, max(0.0, sleep_until - time.monotonic())))
                if time.monotonic() >= deadline:
                    return finish("timeout", "No answer or incoming work before the effective deadline.")
    except BusError as exc:
        return finish("transport_error", f"{exc}; operation incomplete, server outcome unknown.", error=True)
    except TimeoutError:
        if in_flight:
            return finish("transport_error", "bus unreachable: invocation deadline reached; "
                          "operation incomplete, server outcome unknown.", error=True)
        return finish(observed() or "timeout", "Effective deadline reached; companion observation may be incomplete.")
    except OSError as exc:
        return finish("local_error", f"Local ledger I/O failed: {exc}", error=True)


def tool_declare(bus: Bus, ledger: Ledger, arguments: dict[str, Any]) -> str:
    kind, key, operation = _text(arguments, "kind"), _text(arguments, "key"), _text(arguments, "operation")
    note = _text(arguments, "note", required=False)
    row = bus.declare(kind, key, operation, note)
    lines = [f"scope {row.get('id')}: {kind}:{key}={operation}"]
    for finding in row.get("findings") or []:
        lines.append(f"finding {finding.get('id')}: {finding.get('severity')} {finding.get('scope')} "
                     f"with {finding.get('other_task_id')}")
    return "\n".join(lines)


def tool_propose(bus: Bus, ledger: Ledger, arguments: dict[str, Any]) -> str:
    title, brief = _text(arguments, "title"), _text(arguments, "brief")
    acceptance = arguments.get("acceptance")
    if (not isinstance(acceptance, list) or not acceptance
            or any(not isinstance(a, str) or not a.strip() for a in acceptance)):
        raise ValueError("'acceptance' is required and must be a non-empty list of non-empty strings")
    constraints = _text(arguments, "constraints", required=False)
    reviews = _text(arguments, "reviews", required=False)
    request_id = _text(arguments, "request_id", required=False)
    base = _text(arguments, "base", required=False)
    row = bus.propose(title, brief, acceptance, constraints, reviews, request_id, base)
    ledger.append({"event": "proposed", "id": row.get("id")})
    return f"proposed {row.get('id')} ({row.get('status')}) in {row.get('repo')}: {title}" + (" (repeated)" if row.get("repeated") else "")


def tool_submit(bus: Bus, ledger: Ledger, arguments: dict[str, Any]) -> str:
    report = _text(arguments, "report")
    commit = _text(arguments, "commit").strip()
    if not SHA40.fullmatch(commit):
        raise ValueError("'commit' must be a 40-character lowercase hex sha")
    children = arguments.get("children") or []
    patterns = {"task_id": TASK_ID_RE, "run_id": RUN_ID_RE, "commit": SHA40}
    if (not isinstance(children, list) or any(
            not isinstance(child, dict) or set(child) != set(patterns)
            or any(not isinstance(child[key], str) or not pattern.fullmatch(child[key])
                   for key, pattern in patterns.items()) for child in children)):
        raise ValueError("'children' must be a list of {task_id, run_id, commit} entries")
    row = bus.submit(report, commit, children)
    ledger.append({"event": "submitted", "commit": commit, "repeated": bool(row.get("repeated"))})
    return (f"submitted {commit} for verification (repeated: {bool(row.get('repeated'))}); "
            "AOS now closes this session, checks that commit and runs acceptance on it")


def superseded() -> bool:
    return _EOF.is_set() or not _PENDING.empty()


def family_summary(bus: Bus) -> str:
    bus.deadline = None
    try:
        data = bus.family()
    except BusError as exc:
        return f"family: unavailable ({exc})"
    children = data.get("children") or []
    counts = Counter(child.get("status") for child in children)
    text = f"family: {len(children)} children"
    if counts:
        text += " (" + ", ".join(f"{status} {count}" for status, count in sorted(counts.items())) + ")"
    capacity = data.get("capacity")
    if capacity is None:
        return text + "; capacity: no run"
    text += f"; parallel_children {capacity['parallel_children']}"
    if capacity.get("blocker"):
        text += f" — {capacity['blocker']}"
    return text


def tool_inbox_wait(bus: Bus, ledger: Ledger, arguments: dict[str, Any]) -> str:
    raw = arguments.get("timeout_s", WAIT_DEFAULT)
    try:
        budget = int(raw)
    except (TypeError, ValueError):
        raise ValueError("'timeout_s' must be an integer number of seconds")
    budget = max(1, min(budget, WAIT_MAX))
    deadline = time.monotonic() + budget
    bus.deadline = deadline
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if superseded():
            return "wait superseded by a newer call"
        # D6: every poll has its own bound as well as the overall wait budget.
        bus.deadline = min(deadline, time.monotonic() + WAIT_POLL)
        data = bus.inbox()
        rows = data.get("pending_requests") or []
        if rows:
            texts, _ = _prepare_rows(bus, ledger, rows, "inbox_wait_tool")
            return "\n\n".join(texts) + "\n\n" + WAIT_NOTE + "\n" + family_summary(bus) + open_block(ledger)
        sleep_until = min(deadline, time.monotonic() + WAIT_POLL)
        while True:
            if superseded():
                return "wait superseded by a newer call"
            remaining = sleep_until - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(WAIT_SLICE, remaining))
    bus.deadline = None
    return (f"no message after {budget} s; call aos_inbox_wait again if you are still waiting"
            + "\n" + family_summary(bus) + open_block(ledger))


def _child_line(child: dict[str, Any]) -> str:
    run = child.get("run")
    text = f"child {child['id']} \"{child['title']}\": {child['status']} attempt {child['attempt']}"
    text += f" run={run['id']} {run['status']}/{run['stage']}" if run else " run=none"
    text += f" waiting={child['waiting']}"
    if run:
        if run.get("branch"):
            text += f" branch={run['branch']}"
        if run.get("result_commit"):
            text += f" commit={run['result_commit'][:12]}"
    entry = child.get("import")
    state = ("stale" if entry.get("stale") else entry['state']) if entry else "none"
    text += f" questions={len(child.get('questions') or [])} notices={child['notices']} import={state}"
    if run:
        if run.get("remote"):
            text += f" remote={run['remote']}"
        if run.get("pr_url"):
            text += f" pr={run['pr_url']}"
    return text


def tool_family(bus: Bus, ledger: Ledger, arguments: dict[str, Any]) -> str:
    task_id = _text(arguments, "task_id", required=False)
    data = bus.family(task_id or None)
    children = data.get("children") or []
    if task_id:
        child = children[0]
        run = child.get("run") or {}
        return _child_line(child) + "\nreport:\n" + str(run.get("report") or "") + f"\nerror: {run.get('error') or 'none'}"
    capacity = data.get("capacity")
    if capacity is None:
        lines = ["capacity: no run"]
    elif capacity.get("blocker"):
        lines = [f"capacity: parallel_children {capacity['parallel_children']} — {capacity['blocker']}"]
    else:
        lines = [f"capacity: parallel_children {capacity['parallel_children']} "
                 f"(host {capacity['worker']} cap {capacity['host_cap']} live {capacity['host_live']}; "
                 f"repository {capacity['repo']} cap {capacity['repo_cap']} live {capacity['repo_live']})"]
    lines.extend(_child_line(child) for child in children)
    for child in children:
        for question in child.get("questions") or []:
            lines.append(f"question {question['id']} from {child['id']} ({question['state']}): {question['body']}")
    parent = data.get("parent") or {}
    run = parent.get("run") or {}
    for entry in run.get("candidates") or []:
        lines.append(f"candidate {entry['commit'][:12]}: {entry['state']}")
    for entry in run.get("imports") or []:
        lines.append(f"import {entry['task_id']} {entry['run_id']} {entry['commit'][:12]}: {entry['state']}")
    if parent.get("foreign_children"):
        lines.append("foreign children (another repository, not listed): " + ", ".join(parent["foreign_children"]))
    return "\n".join(lines)


def tool_export_candidate(bus: Bus, ledger: Ledger, arguments: dict[str, Any]) -> str:
    commit = _text(arguments, "commit").strip()
    if not SHA40.fullmatch(commit):
        raise ValueError("'commit' must be a 40-character lowercase hex sha")
    row = bus.candidate(commit)
    ledger.append({"event": "candidate", "commit": commit})
    return (f"candidate {commit[:12]} requested (repeated: {bool(row.get('repeated'))}); "
            "the runner exports it and aos_family shows the outcome")


def tool_import(bus: Bus, ledger: Ledger, arguments: dict[str, Any]) -> str:
    values = {key: _text(arguments, key).strip() for key in ("task_id", "run_id", "commit")}
    for key, pattern in (("task_id", TASK_ID_RE), ("run_id", RUN_ID_RE), ("commit", SHA40)):
        if not pattern.fullmatch(values[key]):
            if key == "commit":
                raise ValueError("'commit' must be a 40-character lowercase hex sha")
            form = "AO-<n>" if key == "task_id" else "AO-<n>-r<n>"
            raise ValueError(f"'{key}' must be {form}")
    row = bus.import_result(**values)
    ledger.append({"event": "import", **values})
    return (f"import of {values['task_id']} {values['run_id']} {values['commit'][:12]} requested "
            f"(repeated: {bool(row.get('repeated'))}); when aos_family shows it imported, "
            f"merge refs/aos/imports/{values['task_id']} in /sandbox/repo")


# The receipt source each delivering tool reports (SU3 D7a); the hook reports "hook".
PREPARED_SOURCES = {"aos_inbox": "inbox_tool", "aos_wait": "wait_tool", "aos_inbox_wait": "inbox_wait_tool"}
TOOL_FUNCTIONS = {"aos_ask": tool_ask, "aos_reply": tool_reply, "aos_inbox": tool_inbox,
                  "aos_wait": tool_wait, "aos_declare": tool_declare, "aos_propose": tool_propose,
                  "aos_submit": tool_submit, "aos_inbox_wait": tool_inbox_wait,
                  "aos_family": tool_family, "aos_import": tool_import, "aos_export_candidate": tool_export_candidate}


def run_tool(name: str, arguments: Any, emit: Any = None) -> dict[str, Any]:
    """Compute a result; the MCP writer flushes it before inbox acknowledgements."""
    def result(text: str, error: bool = False) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": _scrub(text)}], "isError": error}

    run = load_run()
    bus = Bus(run, TOOL_TIMEOUT) if run is not None else None
    ledger = None
    answer = None
    try:
        if run is not None:
            ledger = Ledger(run["run_id"])
            if name != "aos_wait":
                ledger.append({"event": "tool", "name": name})
        function = TOOL_FUNCTIONS.get(name)
        if function is None:
            answer = result(f"unknown tool {name}", True)
        elif arguments is not None and not isinstance(arguments, dict):
            answer = result(f"{name}: arguments must be an object", True)
        elif bus is None:
            answer = result(NO_CREDENTIAL, True)
        elif name in INTERACTIVE_TOOLS and run["execution"] != "interactive":
            answer = result(f"{name}: interactive-only tool", True)
        else:
            bus.deliveries = []
            text = function(bus, ledger, arguments or {})
            if bus.deliveries and name != "aos_inbox":
                text += "\n\n" + ACK_NOTE
            answer = result(text)
    except (BusError, WaitError) as exc:
        answer = result(str(exc), True)
    except (ValueError, KeyError, TypeError) as exc:
        answer = result(f"{name}: {exc}", True)
    except OSError as exc:
        if name == "aos_wait" and bus is not None:
            try:
                intent_id = _text(arguments or {}, "id")
                _refuse_protected("id", intent_id)
                budget = max(1, min(int((arguments or {}).get("timeout_s", WAIT_DEFAULT)), WAIT_MAX))
                remaining = budget if bus.deadline is None else max(0.0, bus.deadline - time.monotonic())
                header = {"request_id": intent_id, "outcome": "local_error", "target_state": "unknown",
                          "deadline_source": "wait" if bus.deadline is None else "caller",
                          "remaining_s": min(budget, remaining),
                          "control": "eof" if _EOF.is_set() else "superseded" if not _PENDING.empty() else None}
                answer = result("aos_wait " + json.dumps(header, separators=(",", ":"))
                                + f"\nLocal ledger I/O failed: {exc}", True)
            except (ValueError, TypeError, AttributeError) as invalid:
                answer = result(f"{name}: {invalid}", True)
        else:
            answer = result(f"{name}: {type(exc).__name__}", True)
    except Exception as exc:
        log(f"{name} failed: {type(exc).__name__}")
        answer = result(f"{name}: {type(exc).__name__}", True)
    # Keep writes outside the error conversion: a failed output must not trigger a
    # second protocol response, nor acknowledge content the consumer could not read.
    if emit is not None:
        emit(answer)
    if bus is not None and ledger is not None and bus.deliveries and not answer["isError"]:
        try:
            if name == "aos_wait" and emit is not None:
                for intent_id in bus.wait_returned_ids:
                    ledger.append({"event": "wait_returned", "id": intent_id})
            finish_prepared(bus, ledger, bus.deliveries, PREPARED_SOURCES.get(name, "inbox_tool"))
        except Exception as exc:
            try:
                log(f"post-output delivery bookkeeping failed: {type(exc).__name__}: {exc}; "
                    "messages may be delivered again")
            except OSError:
                pass
    return answer


# -- the MCP stdio server ------------------------------------------------------------

def advertised_tools() -> list:
    return TOOLS if (load_run() or {}).get("execution") == "interactive" else TOOLS[:6]


def send(message: dict[str, Any]) -> None:
    with _STDOUT_LOCK:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def serve() -> int:
    global _PENDING, _EOF, _STDOUT_LOCK
    _PENDING = queue.Queue()
    _STDOUT_LOCK = threading.Lock()
    _EOF.clear()
    pending, eof = _PENDING, _EOF

    def reader() -> None:
        try:
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    message = None
                if not isinstance(message, dict):
                    send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                    continue
                method = message.get("method")
                message_id = message.get("id")
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                if message_id is None or not isinstance(method, str):
                    continue
                if method == "initialize":
                    send({"jsonrpc": "2.0", "id": message_id, "result": {
                        "protocolVersion": str(params.get("protocolVersion") or PROTOCOL_VERSION_DEFAULT),
                        "capabilities": {"tools": {}}, "serverInfo": {"name": "aos", "version": "1"}}})
                elif method == "ping":
                    send({"jsonrpc": "2.0", "id": message_id, "result": {}})
                elif method == "tools/list":
                    send({"jsonrpc": "2.0", "id": message_id, "result": {"tools": advertised_tools()}})
                elif method == "tools/call":
                    pending.put((message_id, params))
                else:
                    send({"jsonrpc": "2.0", "id": message_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})
        finally:
            eof.set()
            pending.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        while True:
            item = pending.get()
            if item is None:
                break
            message_id, params = item
            run_tool(str(params.get("name")), params.get("arguments"),
                     emit=lambda result: send({"jsonrpc": "2.0", "id": message_id, "result": result}))
        return 0
    finally:
        # Keep hooks and in-process callers free of stale input, including on a
        # failed stdout write. The reader retains only this invocation's objects.
        _PENDING = queue.Queue()
        _EOF = threading.Event()


# -- hooks ---------------------------------------------------------------------------

def _env_seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _hook_payload() -> dict[str, Any]:
    payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, dict):
        raise ValueError("hook payload must be an object")
    return payload


def _hook_receiver(action: str) -> int:
    payload = _hook_payload()
    if payload.get("agent_id") is not None:
        return 0
    owner = payload.get("session_id")
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("root lifecycle requires session_id")
    run = load_run()
    if run is None:
        raise ValueError(NO_CREDENTIAL)
    reason = payload.get("reason") if action == "closed" else "native_start"
    if action == "closed" and reason != "native_owner_replaced":
        reason = "native_end"
    result = Bus(run, 2.0).receiver(action, owner, reason)
    if not isinstance(result, dict) or result.get("receiver_state") != action:
        raise ValueError("receiver transition unconfirmed")
    return 0


def hook_receiver(action: str) -> int:
    try:
        with invocation_deadline(time.monotonic() + 2.0):
            return _hook_receiver(action)
    except Exception as exc:
        log("receiver lifecycle failed: " + " ".join(_scrub(str(exc)).split())[:400])
        return 1


def _hook_inbox() -> int:
    """Print and flush every frame before recording evidence or attempting an ack."""
    started = time.monotonic()
    if _hook_payload().get("agent_id") is not None:
        return 0
    run = load_run()
    if run is None:
        return 0
    interval = _env_seconds("AOS_AGENT_HOOK_INTERVAL", 5.0)
    deadline = started + min(HOOK_DEADLINE, _env_seconds("AOS_AGENT_HOOK_DEADLINE", HOOK_DEADLINE))
    ledger = Ledger(run["run_id"])
    last = None
    for event in ledger.events():
        if event.get("event") == "hook" and isinstance(event.get("at"), (int, float)):
            last = event["at"]
    now = time.time()
    if last is not None and now - last < interval:
        return 0
    ledger.append({"event": "hook", "at": int(now)})
    bus = Bus(run, HOOK_TIMEOUT)
    bus.deadline = deadline
    data = bus.inbox()
    if not isinstance(data, dict) or not isinstance(data.get("pending_requests"), list):
        raise ValueError("invalid inbox response")
    rows = data["pending_requests"]
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in rows):
        raise ValueError("invalid inbox row")
    if rows:
        texts = [str(row.get("framed") or row.get("body") or "") for row in rows]
        send({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                     "additionalContext": _scrub("\n\n".join(texts) + "\n\n" + ACK_NOTE)}})
        finish_prepared(bus, ledger, rows, "hook")
    return 0


def open_requests(ledger: Ledger) -> list[dict[str, Any]]:
    events = list(ledger.events())
    replied = {e.get("id") for e in events if e.get("event") == "replied"}
    open_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        if event.get("event") != "prepared" or event.get("reply_to") is not None or event.get("kind") == "notice":
            continue
        intent_id = event.get("id")
        if not isinstance(intent_id, str) or intent_id in replied or intent_id in seen:
            continue
        seen.add(intent_id)
        open_rows.append(event)
    return open_rows


def open_block(ledger: Ledger) -> str:
    rows = open_requests(ledger)
    if not rows:
        return ""
    lines = "\n".join(f"- {_scrub(e['id'])} from {_scrub(str(e.get('from')))}: {' '.join(_scrub(str(e.get('body') or '')).split())[:LEDGER_BODY_CHARS]}"
                      for e in rows[:10])
    return "\n\n" + OPEN_HEADER + "\n" + lines + (f"\n(+{len(rows) - 10} more)" if len(rows) > 10 else "")


def _hook_stop() -> int:
    """Stop: block once while a delivered question stays unanswered; no bus call, always exit 0.
    A finish notice needs no answer and never counts; an event without a kind (an older ledger) does."""
    payload = _hook_payload()
    if payload.get("agent_id") is not None or payload.get("stop_hook_active") is True:
        return 0
    run = load_run()
    if run is None:
        return 0
    ledger = Ledger(run["run_id"])
    events = list(ledger.events())
    if any(e.get("event") == "stop_blocked" for e in events):
        return 0
    open_rows = open_requests(ledger)
    if not open_rows:
        return 0
    # One line per id (fix lane 1 X6, review N4): the body collapsed to one line, cut to 200 characters.
    lines = "\n".join(f"- {_scrub(e['id'])} from {_scrub(str(e.get('from')))}: {' '.join(_scrub(str(e.get('body') or '')).split())[:LEDGER_BODY_CHARS]}"
                      for e in open_rows)
    # Lane B B3: after a Stop block, Claude's `result` carries only the continuation's text and the
    # runner records that as the run's report, so the agent is asked to restate the whole report.
    send({"decision": "block", "reason": _scrub("Requests delivered to you that you have not answered:\n" + lines +
                                           "\nAnswer each with aos_reply, or say in your report why you did not. "
                                           "Then end with your full final report again: your last message is the "
                                           "report this run records.")})
    ledger.append({"event": "stop_blocked"})
    return 0


def _hook_failure(kind: str, exc: Exception) -> int:
    """Signal failure without another stdout document or spending the stop bound."""
    try:
        sys.stderr.write("AOS_AGENT_HOOK_FAILURE\n")
        sys.stderr.flush()
    except Exception:
        pass
    try:
        log(f"{kind} hook: {type(exc).__name__}: {exc}")
    except Exception:
        pass
    return 0


def hook_inbox() -> int:
    try:
        with invocation_deadline(time.monotonic() + HOOK_DEADLINE):
            return _hook_inbox()
    except Exception as exc:
        return _hook_failure("inbox", exc)


def hook_stop() -> int:
    try:
        with invocation_deadline(time.monotonic() + HOOK_DEADLINE):
            return _hook_stop()
    except Exception as exc:
        return _hook_failure("stop", exc)


def summary() -> int:
    """One bounded bookkeeping line, streaming the complete ledger without bodies."""
    run = load_run()
    if run is None:
        print("messaging: no aos-agent ledger (the hooks or the extension never ran)")
        return 0
    directory = os.environ.get("AOS_AGENT_STATE_DIR", STATE_DIR_DEFAULT)
    path = os.path.join(directory, f"{run['run_id']}.jsonl")
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        print("messaging: no aos-agent ledger (the hooks or the extension never ran)")
        return 0
    hooks = calls = size = lines = 0
    tools = {name: 0 for name in ("aos_inbox", "aos_wait", "aos_ask", "aos_reply")}
    prepared: set[str] = set()
    replied: set[str] = set()
    blocked = False
    with handle:
        for raw in handle:
            size += len(raw)
            lines += 1
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("event")
            if kind == "hook":
                hooks += 1
            elif kind == "tool":
                calls += 1
                name = event.get("name")
                if isinstance(name, str) and name in tools:
                    tools[name] += 1
            elif kind in ("prepared", "replied") and isinstance(event.get("id"), str):
                (prepared if kind == "prepared" else replied).add(event["id"])
            elif kind == "stop_blocked":
                blocked = True
    # IDs are diagnostic only: never print bodies, arbitrary control characters,
    # unbounded IDs, or the claim token (scrub before bounding).
    ids = []
    for intent_id in sorted(prepared)[:20]:
        safe = "".join(c if c.isascii() and (c.isalnum() or c in "-_.:") else "_" for c in _scrub(intent_id))
        ids.append(safe[:80])
    if len(prepared) > 20:
        ids.append(f"+{len(prepared) - 20} more")
    print(f"messaging: hook pulls {hooks}, mcp tool calls {calls} "
          f"(inbox {tools['aos_inbox']}, wait {tools['aos_wait']}, ask {tools['aos_ask']}, reply {tools['aos_reply']}), "
          f"prepared {len(prepared)} ({', '.join(ids)}), replied {len(replied)}, stop blocked {int(blocked)}, "
          f"ledger {size} bytes {lines} lines")
    return 0


def peek() -> int:
    run = load_run()
    if run is None:
        sys.stderr.write(NO_CREDENTIAL + "\n")
        return 3
    try:
        data = Bus(run, HOOK_TIMEOUT).inbox()
    except BusError as exc:
        sys.stderr.write(_scrub(f"aos-agent: peek: {exc}\n"))
        return 3
    rows = data.get("pending_requests") or []
    send({"pending": len(rows)})
    return 0


def _owner_queue() -> int:
    """SU4 D2a: fetch pending rows for the OMP owner queue, recording `queued` once per row
    without marking anything `prepared`. Main-thread deadline bounds every bus and ledger call."""
    run = load_run()
    if run is None:
        sys.stderr.write(NO_CREDENTIAL + "\n")
        return 3
    ledger = Ledger(run["run_id"])
    already_queued: set[str] = set()
    confirmed_prepared: set[str] = set()
    for event in ledger.events():
        intent_id = event.get("id")
        if not isinstance(intent_id, str):
            continue
        kind = event.get("event")
        if kind == "queued":
            already_queued.add(intent_id)
        elif kind == "prepared_bus" or (kind == "prepared" and event.get("source") != "owner_queue"):
            confirmed_prepared.add(intent_id)
    bus = Bus(run, HOOK_TIMEOUT)
    data = bus.inbox()
    if not isinstance(data, dict) or not isinstance(data.get("pending_requests"), list):
        raise ValueError("invalid inbox response")
    rows = data["pending_requests"]
    messages: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError("invalid inbox row")
        intent_id = row["id"]
        if intent_id in confirmed_prepared:
            continue
        if intent_id not in already_queued:
            bus.receipt(intent_id, "queued", "owner_queue")
            ledger.append({"event": "queued", "id": intent_id, "source": "owner_queue"})
            already_queued.add(intent_id)
        messages.append({
            "id": intent_id,
            "kind": row.get("kind"),
            "task_id": row.get("task_id"),
            "reply_to": row.get("reply_to"),
            "body": row.get("body"),
            "framed": _scrub(str(row.get("framed") or row.get("body") or "")),
        })
    send({"messages": messages, "ack_note": _scrub(ACK_NOTE)})
    return 0


def owner_queue() -> int:
    started = time.monotonic()
    deadline = started + min(HOOK_DEADLINE, _env_seconds("AOS_AGENT_HOOK_DEADLINE", HOOK_DEADLINE))
    try:
        with invocation_deadline(deadline):
            return _owner_queue()
    except BusError as exc:
        sys.stderr.write(_scrub(f"aos-agent: queue: {exc}\n"))
        return 3
    except Exception as exc:
        sys.stderr.write(_scrub(f"aos-agent: queue: {type(exc).__name__}: {exc}\n"))
        return 3


def _hook_prepare() -> int:
    """SU4 D3a: record `prepared` only when the native `context` event is observed."""
    started = time.monotonic()
    payload = _hook_payload()
    if payload.get("agent_id") is not None:
        send({"prepared_ids": []})
        return 0
    run = load_run()
    if run is None:
        raise ValueError(NO_CREDENTIAL)
    rows = payload.get("messages")
    if not isinstance(rows, list):
        raise ValueError("prepare payload requires a messages list")
    deadline = started + min(HOOK_DEADLINE, _env_seconds("AOS_AGENT_HOOK_DEADLINE", HOOK_DEADLINE))
    ledger = Ledger(run["run_id"])
    already_prepared: set[str] = set()
    confirmed_bus: set[str] = set()
    for event in ledger.events():
        intent_id = event.get("id")
        if not isinstance(intent_id, str):
            continue
        kind = event.get("event")
        if kind == "prepared":
            already_prepared.add(intent_id)
            if event.get("source") != "owner_queue":
                confirmed_bus.add(intent_id)
        elif kind == "prepared_bus":
            confirmed_bus.add(intent_id)
    bus = Bus(run, HOOK_TIMEOUT)
    bus.deadline = deadline
    reported: list[str] = []
    failed = False
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            continue
        intent_id = row["id"]
        if intent_id not in already_prepared:
            ledger.prepared(row, "owner_queue")
            already_prepared.add(intent_id)
        if intent_id in confirmed_bus:
            reported.append(intent_id)
            continue
        if bus.deadline is not None and time.monotonic() >= bus.deadline:
            failed = True
            log("receipt skipped: invocation deadline reached; messages may be delivered again")
            break
        try:
            bus.receipt(intent_id, "prepared", "owner_queue")
            ledger.append({"event": "prepared_bus", "id": intent_id})
            confirmed_bus.add(intent_id)
            reported.append(intent_id)
        except (BusError, KeyError) as exc:
            failed = True
            log(f"receipt failed: {exc}; messages may be delivered again")
    send({"prepared_ids": reported})
    if failed:
        return _hook_failure("prepare", RuntimeError("one or more prepared receipts were not confirmed"))
    return 0


def hook_prepare() -> int:
    started = time.monotonic()
    deadline = started + min(HOOK_DEADLINE, _env_seconds("AOS_AGENT_HOOK_DEADLINE", HOOK_DEADLINE))
    try:
        with invocation_deadline(deadline):
            return _hook_prepare()
    except Exception as exc:
        return _hook_failure("prepare", exc)


def main(argv: list[str]) -> int:
    if argv == ["mcp"]:
        return serve()
    if argv == ["inbox", "--hook"]:
        return hook_inbox()
    if argv == ["stop", "--hook"]:
        return hook_stop()
    if argv == ["prepare", "--hook"]:
        return hook_prepare()
    if argv == ["receiver-start", "--hook"]:
        return hook_receiver("receiving")
    if argv == ["receiver-end", "--hook"]:
        return hook_receiver("closed")
    if argv == ["version"]:
        print(f"aos-agent protocol {AOS_AGENT_PROTOCOL}")
        return 0
    if argv == ["summary"]:
        return summary()
    if argv == ["peek"]:
        return peek()
    if argv == ["queue"]:
        return owner_queue()
    sys.stderr.write("usage: aos-agent mcp | aos-agent inbox --hook | aos-agent stop --hook | aos-agent prepare --hook | aos-agent receiver-start --hook | aos-agent receiver-end --hook | aos-agent version | aos-agent summary | aos-agent peek | aos-agent queue\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
