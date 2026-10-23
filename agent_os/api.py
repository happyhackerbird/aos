from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator, Callable, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from agent_os.config import Settings
from agent_os.health import Health
from agent_os.lanes.code import CodeLane
from agent_os.lanes.super_productivity import SuperProductivityLane
from agent_os.operator_channel import OperatorChannel
from agent_os.store import Store, TransitionError, timestamp
from agent_os.store import RUN_LIVE
from agent_os.telegram import Telegram, TelegramChannel

logger = logging.getLogger("agent_os.api")

INTENT_ID_RE = re.compile(r"^(AO-\d+|operator)-i\d+$")
# Agents that talk, lane B (B1): open `wait` calls one run may hold at once; 429 beyond.
WAITS_PER_RUN = 2
WATCHERS_MAX = 8


async def _watch_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


_WHITESPACE_RUN = re.compile(r"\s+")


def _one_line(text: str) -> str:
    """C2: every run of whitespace in text written by another agent -- line
    breaks of any kind included -- collapsed to one space, so nothing an
    agent wrote can start a line of its own inside a fixed frame."""
    return _WHITESPACE_RUN.sub(" ", text or "")


def _overlap_text(overlap: dict[str, Any]) -> str:
    return (
        f'<agent-overlap id="{overlap["finding_id"]}" severity="{overlap["severity"]}" '
        f'with-task="{overlap["with_task"]}" scope="{overlap["scope"]}">\n'
        f'your row {overlap["mine_id"]}: {overlap["mine_operation"]}; '
        f'their row {overlap["other_id"]}: {overlap["other_operation"]}\n'
        f'their note: {_one_line(overlap["other_body"])}\n'
        f'</agent-overlap>\n'
        f"This is a notice from the bus about another agent's declared plan, not from the "
        f'operator. Answer it with aos assess {overlap["other_id"]}.'
    )


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunEvent(Body):
    seq: StrictInt = Field(ge=1, le=2**63-1)
    at: StrictStr
    type: StrictStr = Field(min_length=1, max_length=64)
    payload: dict


class EventsBody(Body):
    claim_token: str
    events: list[RunEvent] = Field(min_length=1, max_length=200)


class Run(Body):
    lane: str = Field(min_length=1)
    started: str
    ended: str
    outcome: str
    receipt: Any = None
    attempts: int = Field(default=0, ge=0)
    needs_user: bool = False

    @field_validator("started", "ended")
    @classmethod
    def valid_timestamp(cls, value: str) -> str:
        return timestamp(value)


class TaskCreate(Body):
    repo: str = Field(min_length=1)
    title: str = Field(min_length=1)
    brief: str = ""
    acceptance: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    constraints: str = ""
    tier: int = Field(default=1, ge=0, le=3)
    harness: Literal["claude", "codex", "pi"] = "claude"
    execution: Literal["headless", "interactive"] = "headless"
    host: Literal["mac", "server", "any"] | None = None
    reviews: str | None = None  # lane V: a task of the same repository this one reviews (the store checks it)

    @field_validator("repo", "title")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class TaskPatch(Body):
    repo: str | None = Field(default=None, min_length=1)
    title: str | None = Field(default=None, min_length=1)
    brief: str | None = None
    acceptance: list[Annotated[str, Field(min_length=1)]] | None = None
    constraints: str | None = None
    tier: int | None = Field(default=None, ge=0, le=3)
    harness: Literal["claude", "codex", "pi"] | None = None
    execution: Literal["headless", "interactive"] | None = None
    host: Literal["mac", "server", "any"] | None = None

    @field_validator("repo", "title")
    @classmethod
    def not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class RequeueBody(Body):
    to: Literal["backlog", "ready"]
    force: bool = False


class RejectBody(Body):
    note: str = Field(default="", max_length=2000)


class ReadyBody(Body):
    force: bool = False


class MergeBody(Body):
    pr_head_sha: str = Field(min_length=7)


class ChildResult(Body):
    task_id: str = Field(pattern=r"^AO-\d+$")
    run_id: str = Field(pattern=r"^AO-\d+-r\d+$")
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class SubmissionBody(Body):
    claim_token: str
    report: str = Field(min_length=1, max_length=20000)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    children: list[ChildResult] = Field(default_factory=list, max_length=50)


class ClaimBody(Body):
    worker: str = Field(min_length=1)
    worker_host: Literal["mac", "server"]
    expected_spec_hash: str = Field(min_length=1)
    effective_acceptance: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    acceptance_source: Literal["task", "workflow.acceptance", "workflow.test_command", "task+workflow.test_command"]
    workflow_commit: str | None = None
    workflow_ref: str | None = None
    workflow_sha: str | None = None
    harness: Literal["claude", "codex", "pi"] | None = None
    confirm_tier3: bool = False
    dispatch_key: str | None = Field(default=None, min_length=1)


class PreflightRefusalBody(Body):
    expected_spec_hash: str = Field(min_length=1)
    error: str = Field(min_length=1)


class RepoBody(Body):
    default_host: Literal["mac", "server", "any"]
    note: str = ""
    deliver: Literal["push", "pr"] | None = None  # absent keeps the stored value; a new repo defaults to push
    # Agents that talk, U7: proposals by a run in this repo start ready; absent keeps the stored value.
    auto_ready_proposals: bool | None = None


class IntegrationBody(Body):
    run_id: str
    head_sha: str
    base_branch: str
    base_sha: str
    status: str
    output: str
    worker: str


class ScenarioItem(Body):
    title: str
    why: str
    test_command: str


class InteractionBody(Body):
    task_a: str
    task_b: str
    head_a: str
    head_b: str
    status: str
    raw: str
    worker: str
    scenarios: list[ScenarioItem]


class HeartbeatBody(Body):
    claim_token: str
    session_owner: Literal["runner", "hand", "happier"] | None = None
    session_pid: StrictInt | None = None
    session_addr: str | None = None
    session_agent: Literal["claude", "codex", "pi"] | None = None


class StartBody(Body):
    claim_token: str
    base_commit: str
    branch: str
    worktree: str
    session_id: str | None = None
    session_owner: Literal["runner", "hand", "happier"] | None = None
    session_pid: StrictInt | None = None
    session_addr: str | None = None
    session_agent: Literal["claude", "codex", "pi"] | None = None


class ResultBody(Body):
    claim_token: str
    status: Literal["review", "failed", "blocked"]
    report: str = ""
    result_commit: str | None = None
    verdict: str | None = None
    acceptance_output: str | None = None
    error: str | None = None
    session_id: str | None = None
    remote: str | None = None
    evidence: dict[str, Any] | None = None


class ProgressBody(Body):
    claim_token: str
    stage: Literal["claimed", "harness", "acceptance", "push", "cleanup"] | None = None
    report: str | None = None
    acceptance_output: str | None = None
    verdict: str | None = None
    harness_tail: str | None = None
    cleanup_status: Literal["complete", "failed"] | None = None
    cleanup_report: str | None = None


class IntentBody(Body):
    # The agent form carries run_id and claim_token; the operator's form carries neither and
    # authorises with the bearer (agents that talk, D1).
    run_id: str | None = Field(default=None, min_length=1)
    claim_token: str | None = None
    kind: Literal["request"] = "request"
    to_task_id: str | None = None
    body: str = Field(min_length=1, max_length=8000)
    reply_to: str | None = None


class ProposalBody(Body):
    # Agents that talk, U7: a live run proposes a task in its own repository; the run token
    # authorises, never the bearer. The store checks the texts for frame tags.
    run_id: str = Field(min_length=1)
    claim_token: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, max_length=8000)
    acceptance: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    constraints: str = Field(default="", max_length=8000)
    reviews: str | None = Field(default=None, min_length=1, max_length=64)  # lane V: a task of the parent's repository
    request_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    base: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


class CandidateBody(Body):
    claim_token: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class CandidateStateBody(Body):
    state: Literal["exported", "failed"]
    ref: str | None = None
    reason: str | None = None


class ImportBody(Body):
    claim_token: str
    task_id: str = Field(pattern=r"^AO-\d+$")
    run_id: str = Field(pattern=r"^AO-\d+-r\d+$")
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class ImportStateBody(Body):
    run_id: str
    commit: str
    state: Literal["imported", "failed"]
    ref: str | None = None
    reason: str | None = None


class FamilyIntegrationBody(Body):
    order: list[str]
    integrated_at: dict[str, str]


class CapacityBody(Body):
    max_runs: int = Field(ge=1)
    live: int = Field(ge=0)


class FamilyBody(Body):
    claim_token: str
    task_id: str | None = None


class WaitBody(Body):
    run_id: str = Field(min_length=1)
    claim_token: str
    timeout: int = Field(default=60, ge=0, le=60)


class DeliveredBody(Body):
    run_id: str = Field(min_length=1)
    claim_token: str
    via: Literal["socket", "daemon-steer", "daemon-start", "queue", "happier", "inbox"]
    note: str | None = Field(default=None, max_length=500)


class AckBody(Body):
    run_id: str = Field(min_length=1)
    claim_token: str


class RequestsBody(Body):
    claim_token: str


class WriteForm(Body):
    run_id: str | None = None
    claim_token: str | None = None


class ScopeBody(WriteForm):
    scope_kind: str
    key: str
    operation: str
    body: str = ""


class DependencyBody(WriteForm):
    to_task_id: str
    body: str = ""


class BlockerBody(WriteForm):
    body: str


class AssessmentBody(WriteForm):
    about_intent: str
    assessment: str
    merge_rule: str
    body: str = ""


class RetractBody(WriteForm):
    pass


def periodic(stop: threading.Event, interval: int, action: Callable[[], Any]) -> None:
    while not stop.is_set():
        try:
            action()
        except Exception:
            logger.exception("Background action failed")
        stop.wait(interval)


def create_app(settings: Settings | None = None, *, store: Store | None = None,
               telegram: Telegram | None = None, code_lane: CodeLane | None = None,
               sp_lane: SuperProductivityLane | None = None,
               operator_channel: OperatorChannel | None = None,
               background: bool = True) -> FastAPI:
    settings = settings or Settings()
    store = store or Store(settings.BUS_DATA_DIR)
    telegram = telegram or Telegram(settings)
    # Where the request sweep's notices go (the swappable operator channel): Telegram's
    # needs-you chat unless another implementation is passed.
    operator_channel = operator_channel or TelegramChannel(telegram, store)
    if sp_lane is None and settings.sp_enabled:
        sp_lane = SuperProductivityLane(store, telegram, settings)
    code_lane = code_lane or CodeLane(store, telegram, settings, background=background)
    monitored = {"code.poll": settings.CODE_POLL_INTERVAL_SECONDS}
    if sp_lane is not None:
        monitored["sp.poll"] = settings.AOS_SP_POLL_SECONDS
    for name, cadence in monitored.items():
        store.register_lane(name, cadence)
    # Keep historical outcomes, but stop monitoring every lane this start does not run: an
    # integration disabled on restart, or a lane an older bus registered and that no longer exists.
    for registered in store.lanes():
        if registered["lane"] not in monitored and registered["cadence_seconds"]:
            store.register_lane(registered["lane"], 0)
    health = Health(store, telegram, settings)
    # Open `wait` calls per run (lane B B1): run_id -> count, under one lock.
    open_waits: dict[str, int] = {}
    watchers = {"open": 0}
    open_waits_lock = threading.Lock()

    def sweep_requests() -> None:
        for row in store.sweep_intents(ack_seconds=settings.AOS_REQUEST_ACK_SECONDS):
            posted = operator_channel.notify(row)
            if posted is None:
                continue
            if row["notice"] == "operator":
                # A row addressed to the operator (a question, or an agent's reply to her message):
                # where its notice was posted stays on the row, so a reply to it answers it (U5).
                channel, message_id = posted
                store.mark_intent_notified(row["id"], "operator", chat_id=channel, message_id=message_id)
            else:
                store.mark_intent_notified(row["id"], row["notice"])

    def sweep_findings() -> None:
        for finding in store.pending_finding_notices():
            text = (f"⚠️ overlap {finding['severity']}: {finding['task_id']} and "
                   f"{finding['other_task_id']} both declared {finding['scope']} "
                   f"({finding['operation']} / {finding['other_operation']})")
            message_id = telegram.send(settings.TELEGRAM_CHAT_NEEDS_YOU, text)
            if message_id is not None:
                store.mark_finding_notified(finding["id"])

    def sweep_interactions() -> None:
        for review in store.pending_interaction_notices():
            scenarios = review.get("scenarios") or []
            lines = [f"model-proposed, unverified · {review['task_a']} + {review['task_b']}: "
                    f"{review['status']}, {len(scenarios)} scenarios"]
            lines.extend(scenario.get("title", "") for scenario in scenarios[:3])
            message_id = telegram.send(settings.TELEGRAM_CHAT_NEEDS_YOU, "\n".join(lines))
            if message_id is not None:
                store.mark_interaction_notified(review["id"])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = threading.Event()
        threads = []
        if background:
            workers = [(health.check, settings.HEALTH_INTERVAL_SECONDS)]
            workers.append((store.expire_task_leases, settings.TASK_SWEEP_INTERVAL_SECONDS))
            workers.append((sweep_requests, settings.TASK_SWEEP_INTERVAL_SECONDS))
            workers.append((sweep_findings, settings.TASK_SWEEP_INTERVAL_SECONDS))
            workers.append((sweep_interactions, settings.TASK_SWEEP_INTERVAL_SECONDS))
            workers.append((code_lane.poll, settings.CODE_POLL_INTERVAL_SECONDS))
            if sp_lane is not None:
                workers.append((sp_lane.poll, settings.AOS_SP_POLL_SECONDS))
            for action, interval in workers:
                thread = threading.Thread(target=periodic, args=(stop, interval, action), daemon=True)
                thread.start()
                threads.append(thread)
            thread = telegram.start(stop, store, code_lane)
            if thread:
                threads.append(thread)
        try:
            yield
        finally:
            stop.set()
            # Long-poll requests have finite HTTP timeouts and daemon threads.
            for thread in threads:
                thread.join(timeout=1)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.health = store, health
    app.state.sp_lane = sp_lane
    app.state.code_lane = code_lane
    app.state.operator_channel = operator_channel
    app.state.sweep_requests = sweep_requests
    app.state.sweep_findings = sweep_findings
    app.state.sweep_interactions = sweep_interactions

    def auth(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {settings.BUS_TOKEN}".encode()
        if authorization is None or not hmac.compare_digest(authorization.encode(), expected):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    def _write_credential(run_id: str | None, claim_token: str | None,
                         authorization: str | None) -> tuple[str | None, str | None, bool]:
        """The two forms of a write route: (run_id, claim_token) both present is the agent's
        form; both absent with a valid bearer is the operator's form. One of the two fields is
        422; neither a credential nor a valid bearer is 401."""
        has_run, has_tok = run_id is not None, claim_token is not None
        if has_run != has_tok:
            raise HTTPException(422, "run_id and claim_token must both be present or both absent")
        if has_run and has_tok:
            return run_id, claim_token, False
        expected = f"Bearer {settings.BUS_TOKEN}".encode()
        if authorization is None or not hmac.compare_digest(authorization.encode(), expected):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})
        return None, None, True

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Agents that talk, fix lane 1 (review F2): FastAPI's default 422 echoes the offending
        value as `input`, which for a missing field is the whole body, claim token included; the
        sandboxed agent prints bus details into the model's context, so `input` is dropped."""
        errors = [{key: value for key, value in error.items() if key != "input"} for error in exc.errors()]
        return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing(request: Request, exc: KeyError) -> JSONResponse:
        key = exc.args[0] if exc.args else None
        if isinstance(key, str) and INTENT_ID_RE.match(key):
            detail = "Intent not found"
        else:
            path = request.url.path
            if path.startswith("/tasks"):
                detail = "Task not found"
            elif path.startswith("/runs/"):
                detail = "Run not found"
            elif path.startswith("/repos"):
                detail = "Repo not found"
            else:
                detail = "Not found"
        return JSONResponse({"detail": detail}, status_code=404)

    @app.exception_handler(TransitionError)
    async def conflict(request: Request, exc: TransitionError) -> JSONResponse:
        body: dict[str, Any] = {"detail": str(exc)}
        run_id = getattr(exc, "run_id", None)
        if run_id is not None:
            body["run_id"] = run_id
        return JSONResponse(body, status_code=409)

    @app.exception_handler(ValueError)
    async def bad_input(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(httpx.HTTPError)
    async def upstream(request: Request, exc: httpx.HTTPError) -> JSONResponse:
        return JSONResponse({"detail": "Upstream request failed"}, status_code=502)

    @app.get("/health")
    def get_health() -> dict[str, Any]:
        return health.snapshot()

    @app.get("/capabilities", dependencies=[Depends(auth)])
    def get_capabilities() -> dict[str, Any]:
        return {"evidence": True, "bus_id": store.meta_setdefault("bus_id", secrets.token_hex(16))}

    @app.post("/runs", dependencies=[Depends(auth)])
    def run(body: Run) -> dict[str, bool]:
        store.record_run(**body.model_dump())
        return {"ok": True}

    # -- task backlog ---------------------------------------------------

    @app.post("/tasks", dependencies=[Depends(auth)])
    def create_task(body: TaskCreate) -> dict[str, Any]:
        return store.create_task(**body.model_dump())

    @app.get("/tasks", dependencies=[Depends(auth)])
    def list_tasks(
        status: Literal["backlog", "ready", "claimed", "running", "review", "done",
                        "blocked", "failed", "cancelled"] | None = Query(default=None),
        repo: str | None = Query(default=None),
        host: Literal["mac", "server", "any"] | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        return store.list_tasks(status=status, repo=repo, host=host)

    @app.get("/tasks/{task_id}", dependencies=[Depends(auth)])
    def get_task(task_id: str) -> dict[str, Any]:
        task = store.get_task(task_id)
        task["interactions"] = store.interactions_for_task(task_id)
        return task

    @app.patch("/tasks/{task_id}", dependencies=[Depends(auth)])
    def patch_task(task_id: str, body: TaskPatch) -> dict[str, Any]:
        return store.edit_task(task_id, **body.model_dump(exclude_unset=True))

    @app.post("/tasks/{task_id}/ready", dependencies=[Depends(auth)])
    def ready_task(task_id: str, body: ReadyBody = ReadyBody()) -> dict[str, Any]:
        return store.ready_task(task_id, force=body.force)

    @app.post("/tasks/{task_id}/unready", dependencies=[Depends(auth)])
    def unready_task(task_id: str) -> dict[str, Any]:
        return store.unready_task(task_id)

    @app.post("/tasks/{task_id}/preflight-refusal", dependencies=[Depends(auth)])
    def preflight_refusal(task_id: str, body: PreflightRefusalBody) -> dict[str, bool]:
        return store.record_preflight_refusal(
            task_id, expected_spec_hash=body.expected_spec_hash, error=body.error)

    @app.post("/tasks/{task_id}/cancel", dependencies=[Depends(auth)])
    def cancel_task(task_id: str) -> dict[str, Any]:
        return store.cancel_task(task_id)

    def _newest_review_run_id(task_id: str) -> str | None:
        return next((r["id"] for r in store.get_task(task_id)["runs"] if r["status"] == "review"), None)

    @app.post("/tasks/{task_id}/accept", dependencies=[Depends(auth)])
    def accept_task(task_id: str) -> dict[str, Any]:
        run_id = _newest_review_run_id(task_id)
        result = store.complete_task(task_id)
        if run_id is not None:
            code_lane.retire_card(run_id, "✅ accepted")
        return result

    @app.post("/tasks/{task_id}/requeue", dependencies=[Depends(auth)])
    def requeue_task(task_id: str, body: RequeueBody) -> dict[str, Any]:
        run_id = _newest_review_run_id(task_id)
        result = store.requeue_task(task_id, body.to, force=body.force)
        if run_id is not None:
            code_lane.retire_card(run_id, "↩ requeued")
        return result

    @app.post("/tasks/{task_id}/reject", dependencies=[Depends(auth)])
    def reject_task(task_id: str, body: RejectBody = RejectBody()) -> dict[str, Any]:
        run_id = _newest_review_run_id(task_id)
        result = store.reject_task(task_id, body.note)
        if run_id is not None:
            code_lane.retire_card(run_id, "❌ rejected")
        return result

    @app.post("/tasks/{task_id}/merge", dependencies=[Depends(auth)])
    def merge_task(task_id: str, body: MergeBody) -> dict[str, Any]:
        try:
            return code_lane.merge(task_id, body.pr_head_sha)
        except httpx.HTTPError as exc:
            raise HTTPException(502, "GitHub request failed; nothing changed") from exc

    @app.post("/tasks/{task_id}/claim", dependencies=[Depends(auth)])
    def claim_task(task_id: str, body: ClaimBody) -> dict[str, Any]:
        run = store.claim_task(task_id, worker=body.worker, worker_host=body.worker_host,
                               expected_spec_hash=body.expected_spec_hash,
                               effective_acceptance=body.effective_acceptance,
                               acceptance_source=body.acceptance_source,
                               workflow_commit=body.workflow_commit, workflow_ref=body.workflow_ref,
                               workflow_sha=body.workflow_sha,
                               harness=body.harness, confirm_tier3=body.confirm_tier3,
                               dispatch_key=body.dispatch_key, lease_seconds=settings.TASK_LEASE_SECONDS,
                               max_live_per_repo=settings.TASK_MAX_LIVE_RUNS_PER_REPO)
        conflicts = [f for f in store.findings_for_task(task_id, open_only=True)
                    if f["severity"] in ("high", "medium")]
        return {**run, "conflicts": conflicts}

    @app.post("/tasks/{task_id}/integration", dependencies=[Depends(auth)])
    def post_integration(task_id: str, body: IntegrationBody) -> dict[str, Any]:
        return store.record_integration(task_id, body.run_id, head_sha=body.head_sha,
                                        base_branch=body.base_branch, base_sha=body.base_sha,
                                        status=body.status, output=body.output, worker=body.worker)

    @app.post("/interactions", dependencies=[Depends(auth)])
    def post_interaction(body: InteractionBody) -> dict[str, Any]:
        return store.record_interaction(
            body.task_a, body.task_b, head_a=body.head_a, head_b=body.head_b, status=body.status,
            scenarios=[scenario.model_dump() for scenario in body.scenarios], raw=body.raw,
            worker=body.worker)

    @app.get("/repos", dependencies=[Depends(auth)])
    def get_repos() -> list[dict[str, Any]]:
        return store.repos()

    @app.put("/repos/{name}", dependencies=[Depends(auth)])
    def put_repo(name: str, body: RepoBody) -> dict[str, Any]:
        return store.set_repo(name, default_host=body.default_host, note=body.note,
                              deliver=body.deliver,
                              auto_ready_proposals=body.auto_ready_proposals)

    @app.delete("/repos/{name}", dependencies=[Depends(auth)])
    def delete_repo(name: str) -> dict[str, bool]:
        store.delete_repo(name)
        return {"ok": True}

    # -- task sources ---------------------------------------------------

    @app.post("/sources/sp/poll", dependencies=[Depends(auth)])
    def sp_poll() -> dict[str, Any]:
        """One Super Productivity read applied to the bus, at once; the outcome lists what changed."""
        if sp_lane is None:
            raise HTTPException(404, "Super Productivity source is not configured (the AOS_SP_* settings)")
        return sp_lane.poll()

    @app.post("/runs/{run_id}/heartbeat", dependencies=[Depends(auth)])
    def heartbeat(run_id: str, body: HeartbeatBody) -> dict[str, Any]:
        return store.heartbeat_run(run_id, body.claim_token, lease_seconds=settings.TASK_LEASE_SECONDS,
                                   ack_seconds=settings.AOS_REQUEST_ACK_SECONDS,
                                   session_owner=body.session_owner, session_pid=body.session_pid,
                                   session_addr=body.session_addr, session_agent=body.session_agent)

    # -- request ledger (task_intents) -----------------------------------

    @app.post("/tasks/{task_id}/intents")
    def create_intent(task_id: str, body: IntentBody,
                      authorization: str | None = Header(default=None)) -> dict[str, Any]:
        run_id, claim_token, operator = _write_credential(body.run_id, body.claim_token, authorization)
        if operator:
            # The operator's form (agents that talk, U1 S7): the bearer, no run fields, and the path
            # names the sender `operator`; the operator never speaks as a task.
            if task_id != "operator":
                raise HTTPException(422, "the operator form posts to /tasks/operator/intents")
            return store.create_operator_intent(to_task_id=body.to_task_id, body=body.body,
                                                reply_to=body.reply_to,
                                                ack_seconds=settings.AOS_REQUEST_ACK_SECONDS)
        return store.create_intent(task_id, run_id, claim_token, kind=body.kind,
                                   to_task_id=body.to_task_id, body=body.body, reply_to=body.reply_to,
                                   ack_seconds=settings.AOS_REQUEST_ACK_SECONDS)

    @app.post("/intents/{intent_id}/wait")
    def wait_intent(intent_id: str, body: WaitBody) -> dict[str, Any]:
        """Long poll on the run's own request (U1 S10): returns as soon as it is answered, else after
        `timeout` seconds (0..60), re-reading every second with no transaction held.

        Ceiling: each waiter holds one worker thread for up to 60 s, so the threadpool's size bounds
        the concurrent waiters; one run holds at most WAITS_PER_RUN of them (429 beyond, lane B
        B1), so a single run cannot take the pool; fine for a handful of runs, revisit before
        raising the per-repo run cap or the pool."""
        deadline = time.monotonic() + body.timeout
        # The first read authenticates (409/404 propagate) before any slot is taken.
        result = store.intent_for_waiter(intent_id, body.run_id, body.claim_token)
        with open_waits_lock:
            if open_waits.get(body.run_id, 0) >= WAITS_PER_RUN:
                raise HTTPException(429, f"this run already has {WAITS_PER_RUN} waits open")
            open_waits[body.run_id] = open_waits.get(body.run_id, 0) + 1
        try:
            while True:
                if result["intent"]["state"] == "answered" or time.monotonic() >= deadline:
                    return result
                time.sleep(1)
                result = store.intent_for_waiter(intent_id, body.run_id, body.claim_token)
        finally:
            with open_waits_lock:
                left = open_waits.get(body.run_id, 0) - 1
                if left > 0:
                    open_waits[body.run_id] = left
                else:
                    open_waits.pop(body.run_id, None)

    # -- proposals (agents that talk, U7) ---------------------------------

    @app.post("/tasks/{task_id}/proposals")
    def propose_task(task_id: str, body: ProposalBody) -> dict[str, Any]:
        """A live run of `task_id` proposes a task in its own repository. Run token in the body,
        no bearer; the answer is the new task row (backlog, or ready when the repository's
        `auto_ready_proposals` switch is on)."""
        return store.propose_task(task_id, body.run_id, body.claim_token, title=body.title, brief=body.brief,
                                  acceptance=body.acceptance, constraints=body.constraints, reviews=body.reviews,
                                  request_id=body.request_id, base=body.base)

    @app.post("/intents/{intent_id}/delivered")
    def deliver_intent(intent_id: str, body: DeliveredBody) -> dict[str, Any]:
        return store.report_delivery(intent_id, body.run_id, body.claim_token, body.via, note=body.note,
                                     ack_seconds=settings.AOS_REQUEST_ACK_SECONDS)

    @app.post("/intents/{intent_id}/ack")
    def ack_intent(intent_id: str, body: AckBody) -> dict[str, Any]:
        return store.ack_intent(intent_id, body.run_id, body.claim_token,
                                ack_seconds=settings.AOS_REQUEST_ACK_SECONDS)

    @app.post("/runs/{run_id}/candidates")
    def request_candidate(run_id: str, body: CandidateBody) -> dict[str, Any]:
        return store.request_candidate(run_id, body.claim_token, commit=body.commit)

    @app.post("/runs/{run_id}/candidates/{commit}/state", dependencies=[Depends(auth)])
    def candidate_state(run_id: str, commit: str, body: CandidateStateBody) -> dict[str, Any]:
        return store.set_candidate_state(run_id, commit, state=body.state, ref=body.ref, reason=body.reason)

    @app.post("/runs/{run_id}/imports")
    def request_import(run_id: str, body: ImportBody) -> dict[str, Any]:
        return store.request_import(run_id, body.claim_token, task_id=body.task_id,
                                    child_run_id=body.run_id, commit=body.commit)

    @app.post("/runs/{run_id}/imports/{task_id}/state", dependencies=[Depends(auth)])
    def import_state(run_id: str, task_id: str, body: ImportStateBody) -> dict[str, Any]:
        return store.set_import_state(run_id, task_id, body.run_id, body.commit,
                                      state=body.state, ref=body.ref, reason=body.reason)

    @app.post("/runs/{run_id}/integration", dependencies=[Depends(auth)])
    def family_integration(run_id: str, body: FamilyIntegrationBody) -> dict[str, Any]:
        return store.record_family_integration(run_id, order=body.order, integrated_at=body.integrated_at)

    @app.post("/hosts/{worker}/capacity", dependencies=[Depends(auth)])
    def publish_capacity(worker: str, body: CapacityBody) -> dict[str, Any]:
        return store.publish_capacity(worker, max_runs=body.max_runs, live=body.live)

    @app.post("/runs/{run_id}/family")
    def run_family(run_id: str, body: FamilyBody) -> dict[str, Any]:
        try:
            run = store.run_bound(run_id, body.claim_token, interactive=True)
        except TransitionError as exc:
            if str(exc) == "run is not interactive":
                raise HTTPException(403, "family read is interactive-only") from exc
            raise
        return store.family(run["task_id"], max_live_per_repo=settings.TASK_MAX_LIVE_RUNS_PER_REPO,
                            detail=body.task_id)

    @app.get("/tasks/{task_id}/family", dependencies=[Depends(auth)])
    def task_family(task_id: str, child: str | None = Query(default=None)) -> dict[str, Any]:
        return store.family(task_id, max_live_per_repo=settings.TASK_MAX_LIVE_RUNS_PER_REPO, detail=child)

    @app.post("/runs/{run_id}/submission")
    def submit_run(run_id: str, body: SubmissionBody) -> dict[str, Any]:
        return store.submit_run(run_id, body.claim_token, report=body.report, commit=body.commit,
                                children=[c.model_dump() for c in body.children])

    @app.post("/runs/{run_id}/requests")
    def run_requests(run_id: str, body: RequestsBody) -> dict[str, Any]:
        pending = store.requests_for_run(run_id, body.claim_token,
                                         ack_seconds=settings.AOS_REQUEST_ACK_SECONDS)
        task_id = store.get_run(run_id)["task_id"]
        overlaps = [_overlap_text(o) for o in store.overlaps_for_task(task_id)]
        return {"pending_requests": pending, "overlaps": overlaps}

    @app.get("/tasks/{task_id}/intents", dependencies=[Depends(auth)])
    def list_intents(task_id: str) -> list[dict[str, Any]]:
        return store.list_intents(task_id)

    # -- scope declarations, dependencies, blockers, assessments (parallel step 2,
    #    unit U2) -----------------------------------------------------------

    @app.post("/tasks/{task_id}/scopes")
    def declare_scope(task_id: str, body: ScopeBody,
                      authorization: str | None = Header(default=None)) -> dict[str, Any]:
        run_id, claim_token, operator = _write_credential(body.run_id, body.claim_token, authorization)
        return store.declare_scope(task_id, scope_kind=body.scope_kind, key=body.key,
                                   operation=body.operation, body=body.body,
                                   run_id=run_id, claim_token=claim_token, operator=operator)

    @app.post("/tasks/{task_id}/dependencies")
    def declare_dependency(task_id: str, body: DependencyBody,
                           authorization: str | None = Header(default=None)) -> dict[str, Any]:
        run_id, claim_token, operator = _write_credential(body.run_id, body.claim_token, authorization)
        return store.declare_dependency(task_id, to_task_id=body.to_task_id, body=body.body,
                                        run_id=run_id, claim_token=claim_token, operator=operator)

    @app.post("/tasks/{task_id}/blockers")
    def declare_blocker(task_id: str, body: BlockerBody,
                        authorization: str | None = Header(default=None)) -> dict[str, Any]:
        run_id, claim_token, operator = _write_credential(body.run_id, body.claim_token, authorization)
        return store.declare_blocker(task_id, body=body.body,
                                     run_id=run_id, claim_token=claim_token, operator=operator)

    @app.post("/tasks/{task_id}/assessments")
    def declare_assessment(task_id: str, body: AssessmentBody,
                           authorization: str | None = Header(default=None)) -> dict[str, Any]:
        run_id, claim_token, operator = _write_credential(body.run_id, body.claim_token, authorization)
        return store.assess(task_id, about_intent=body.about_intent, assessment=body.assessment,
                            merge_rule=body.merge_rule, body=body.body,
                            run_id=run_id, claim_token=claim_token, operator=operator)

    @app.post("/intents/{intent_id}/retract")
    def retract_intent(intent_id: str, body: RetractBody,
                       authorization: str | None = Header(default=None)) -> dict[str, Any]:
        run_id, claim_token, operator = _write_credential(body.run_id, body.claim_token, authorization)
        return store.retract_intent(intent_id, run_id=run_id, claim_token=claim_token, operator=operator)

    @app.get("/intents", dependencies=[Depends(auth)])
    def intents_for_repo(repo: str = Query(...), open: int = Query(default=0)) -> list[dict[str, Any]]:
        return store.intents_for_repo(repo, open_only=bool(open))

    @app.post("/runs/{run_id}/scopes")
    def run_scopes(run_id: str, body: RequestsBody) -> list[dict[str, Any]]:
        return store.open_scopes_for_run(run_id, body.claim_token)

    @app.post("/runs/{run_id}/start", dependencies=[Depends(auth)])
    def start(run_id: str, body: StartBody) -> dict[str, Any]:
        return store.start_run(run_id, body.claim_token, base_commit=body.base_commit,
                               branch=body.branch, worktree=body.worktree, session_id=body.session_id,
                               session_owner=body.session_owner, session_pid=body.session_pid,
                               session_addr=body.session_addr, session_agent=body.session_agent)

    @app.post("/runs/{run_id}/result", dependencies=[Depends(auth)])
    def result(run_id: str, body: ResultBody) -> dict[str, Any]:
        marks = code_lane.result_marks(run_id, body.status, body.remote)
        run = store.finish_run(run_id, body.claim_token, status=body.status, report=body.report,
                               result_commit=body.result_commit, verdict=body.verdict,
                               acceptance_output=body.acceptance_output, error=body.error,
                               session_id=body.session_id, remote=body.remote, marks=marks,
                               grace_seconds=settings.TASK_LATE_RESULT_GRACE_SECONDS,
                               evidence=(json.dumps(body.evidence, separators=(",", ":"))
                                         if body.evidence is not None else None))
        code_lane.notify_result(run)
        return run

    @app.post("/runs/{run_id}/progress", dependencies=[Depends(auth)])
    def progress(run_id: str, body: ProgressBody) -> dict[str, Any]:
        return store.progress_run(run_id, body.claim_token, stage=body.stage, report=body.report,
                                  acceptance_output=body.acceptance_output, verdict=body.verdict,
                                  harness_tail=body.harness_tail, cleanup_status=body.cleanup_status,
                                  cleanup_report=body.cleanup_report)

    @app.post("/runs/{run_id}/events", dependencies=[Depends(auth)])
    def publish_events(run_id: str, body: EventsBody) -> dict[str, int]:
        return store.append_run_events(run_id, body.claim_token, [event.model_dump() for event in body.events])

    @app.get("/runs/{run_id}/events", dependencies=[Depends(auth)])
    async def read_events(run_id: str, after: int = Query(0, ge=0, le=2**63-1),
                          wait: int = Query(0, ge=0, le=30),
                          limit: int = Query(500, ge=1, le=500)) -> dict[str, Any]:
        result = await run_in_threadpool(store.read_run_events, run_id, after=after, limit=limit)
        if result["events"] or wait == 0 or result["run"]["status"] not in RUN_LIVE:
            return result
        if watchers["open"] >= WATCHERS_MAX:
            raise HTTPException(429, f"the bus already has {WATCHERS_MAX} watchers open")
        watchers["open"] += 1
        deadline = time.monotonic() + wait
        try:
            while True:
                await _watch_sleep(1)
                result = await run_in_threadpool(store.read_run_events, run_id, after=after, limit=limit)
                if result["events"] or result["run"]["status"] not in RUN_LIVE or time.monotonic() >= deadline:
                    return result
        finally:
            watchers["open"] -= 1

    return app
