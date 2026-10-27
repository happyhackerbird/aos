"""CodeLane: opens PRs for review runs and keeps one Telegram card per run.

U1 scope: on_result, _open_pr (unbounded retry, one needs-you ping at
FAIL_THRESHOLD), _sync_card, retire_card, pr_state, notify_result and the
poll() sweep's no-pr_number arm. The merge-observation arm of poll() (GET the
PR, merged -> complete the task locally) and CodeLane.merge/_complete/requeue
are U2's; they land in this same file after U1 merges.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

import httpx

from agent_os.config import Settings
from agent_os.github import GitHub, GitHubRefused, parse_remote
from agent_os.store import Store, TransitionError, utcnow
from agent_os.telegram import Telegram, code_buttons, hash8, render_code
from agent_os.workflow import WorkflowError, run_acceptance

logger = logging.getLogger("agent_os.lanes.code")


class CodeLane:
    def __init__(self, store: Store, telegram: Telegram, settings: Settings,
                 github: GitHub | None = None, background: bool = False) -> None:
        self.store, self.telegram, self.settings = store, telegram, settings
        self.lock = threading.RLock()
        self.github = github or GitHub(settings)
        self.background = background
        # One token is shared by every run: a rate-limit refusal from any run
        # pauses every GitHub call the lane makes.
        self.github_paused_until: float = 0.0

    # -- meta helpers -----------------------------------------------------

    def attempts(self, run_id: str) -> int:
        return int(self.store.meta_get(f"code.pr_attempts.{run_id}") or 0)

    def pr_step(self, run_id: str) -> str | None:
        return self.store.meta_get(f"code.pr_step.{run_id}")

    def card_pending(self, run_id: str) -> bool:
        return bool(self.store.meta_get(f"code.card_pending.{run_id}"))

    def merged_unclosed(self, run_id: str) -> str | None:
        return self.store.meta_get(f"code.merged_unclosed.{run_id}")

    def pr_error(self, run_id: str) -> str:
        return self.store.meta_get(f"code.pr_error.{run_id}") or ""

    def pr_wanted(self, task: dict[str, Any]) -> bool:
        """Task-flow Part C: a pull request is opened only for a repository registered with
        `deliver=pr` (`aos repo set <name> --deliver pr`); the default, and an unregistered
        repository, is push only: the branch is on the remote and `aos accept` closes the task."""
        repo = self.store.get_repo(task["repo"])
        return repo is not None and repo.get("deliver") == "pr"

    def result_marks(self, run_id: str, status: str, remote: str | None) -> dict[str, str]:
        """The meta rows finish_run writes inside the result's own transaction (reads only: the
        run's task and its repo registration decide the PR step)."""
        if status != "review":
            return {}
        task = self.store.get_task(self.store.get_run(run_id)["task_id"])
        pr_on = bool(remote and parse_remote(remote) and self.github.enabled and self.pr_wanted(task))
        marks = {f"code.pr_step.{run_id}": "on" if pr_on else "off"}
        if self.telegram.enabled:
            marks[f"code.card_pending.{run_id}"] = "1"
        return marks

    def pr_state(self, run: dict[str, Any]) -> str:
        if run.get("pr_number") is not None:
            return "opened"
        if self.pr_step(run["id"]) != "on":
            return "off"
        return "pending"

    @staticmethod
    def _newest_review_run(task: dict[str, Any]) -> dict[str, Any] | None:
        for run in task["runs"]:
            if run["status"] == "review":
                return run
        return None

    # -- result -------------------------------------------------------------

    def notify_result(self, run: dict[str, Any]) -> None:
        if self.background:
            threading.Thread(target=self._safe_on_result, args=(run,), daemon=True).start()
        else:
            self._safe_on_result(run)

    def _safe_on_result(self, run: dict[str, Any]) -> None:
        try:
            self.on_result(run)
        except Exception:
            logger.exception("code lane on_result failed")

    def on_result(self, run: dict[str, Any]) -> None:
        with self.lock:
            run = self.store.get_run(run["id"])
            task = self.store.get_task(run["task_id"])
            if run["status"] in ("failed", "blocked"):
                self.telegram.send(
                    self.settings.TELEGRAM_CHAT_NEEDS_YOU,
                    f"❌ {task['id']} · {task['title']}\n"
                    f"run {run['id']} {run['status']}: {run.get('error') or ''}\n"
                    f"branch {run.get('branch') or ''}",
                )
                return
            if run["status"] != "review" or task["status"] != "review":
                return
            newest = self._newest_review_run(task)
            if newest is None or newest["id"] != run["id"]:
                return
            if self.pr_wanted(task):
                run = self._open_pr(task, run)
            self._sync_card(task, run)

    # -- rate-limit pause -------------------------------------------------

    def _note_refusal(self, exc: GitHubRefused) -> None:
        """A rate-limit refusal (403/429 carrying Retry-After) from ANY GitHub
        call pauses every call the lane makes; a plain 4xx refusal with no
        Retry-After leaves the pause untouched. Called at every
        `except GitHubRefused` in the lane (_open_pr, poll's get_pr arm,
        merge's merge_pr branch)."""
        if exc.retry_after is not None:
            self.github_paused_until = time.monotonic() + max(exc.retry_after, 60.0)

    # -- PR open, unbounded retry ---------------------------------------------

    def _open_pr(self, task: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        if (time.monotonic() < self.github_paused_until or self.pr_step(run["id"]) != "on"
                or not self.github.enabled or run.get("pr_number") is not None):
            return run
        parsed = parse_remote(run.get("remote"))
        if parsed is None:
            return run
        owner, repo = parsed
        try:
            commands = run_acceptance(run)
        except WorkflowError as exc:
            message = f"{exc}; requeue and run the task again before opening a PR"
            if message != self.pr_error(run["id"]):
                self.store.meta_set(f"code.pr_error.{run['id']}", message)
                self.store.meta_set(f"code.card_pending.{run['id']}", "1")
                self.store.task_event("pr:refused", task_id=task["id"], run_id=run["id"], message=message)
                logger.warning("GitHub PR refused for %s: %s", run["id"], message)
            return run
        try:
            base = self.github.default_branch(owner, repo)
            title = f"{task['id']}: {task['title']}"
            acceptance_lines = "acceptance:\n" + "".join(f"  {c}\n" for c in commands)
            body = (f"run {run['id']}, verdict {run.get('verdict')}, "
                    f"result_commit {run.get('result_commit')}\n\n"
                    f"{acceptance_lines}\n{(run.get('report') or '')[:4000]}")
            pr = self.github.open_pr(owner, repo, head=f"ao/{task['id']}", base=base, title=title, body=body)
            self.store.annotate_run(run["id"], pr_number=pr["number"], pr_url=pr["html_url"],
                                    pr_head_sha=pr["head"]["sha"], pr_base=base)
            self.store.task_event("pr:opened", task_id=task["id"], run_id=run["id"],
                                  pr_number=str(pr["number"]), pr_url=pr["html_url"],
                                  pr_head_sha=pr["head"]["sha"])
            self.store.meta_set(f"code.pr_attempts.{run['id']}", "")
            self.store.meta_set(f"code.pr_error.{run['id']}", "")
            self.store.meta_set(f"code.card_pending.{run['id']}", "1")
            run = self.store.get_run(run["id"])
        except (httpx.HTTPError, GitHubRefused) as exc:
            attempts = self.attempts(run["id"]) + 1
            self.store.meta_set(f"code.pr_attempts.{run['id']}", str(attempts))
            message = f"{type(exc).__name__}: {str(exc)[:200]}"
            if message != self.pr_error(run["id"]):
                self.store.meta_set(f"code.pr_error.{run['id']}", message)
                self.store.meta_set(f"code.card_pending.{run['id']}", "1")
            logger.warning("GitHub PR open failed for %s: %s: %s", run["id"], type(exc).__name__, exc)
            if isinstance(exc, GitHubRefused):
                self._note_refusal(exc)
            if attempts == self.settings.FAIL_THRESHOLD:
                self.telegram.send(
                    self.settings.TELEGRAM_CHAT_NEEDS_YOU,
                    f"⚠️ {run['id']}: PR not opened after {attempts} attempts: {str(exc)[:300]}")
                self.store.meta_set(f"code.card_pending.{run['id']}", "1")
        return run

    # -- card -----------------------------------------------------------------

    def _sync_card(self, task: dict[str, Any], run: dict[str, Any]) -> None:
        if not self.telegram.enabled or not self.card_pending(run["id"]):
            return
        task = self.store.get_task(task["id"])
        if task["status"] != "review" or self.merged_unclosed(run["id"]):
            self.store.meta_set(f"code.card_pending.{run['id']}", "")
            return
        text = render_code(task, run, pr_state=self.pr_state(run), pr_error=self.pr_error(run["id"]))
        buttons = code_buttons(task, run)
        if run.get("tg_message_id") is None:
            message_id = self.telegram.send(self.settings.code_chat, text, buttons)
            if message_id is not None:
                self.store.annotate_run(run["id"], tg_chat_id=self.settings.code_chat,
                                        tg_message_id=message_id)
                self.store.meta_set(f"code.card_pending.{run['id']}", "")
        else:
            if self.telegram.edit_card(run, text, buttons):
                self.store.meta_set(f"code.card_pending.{run['id']}", "")

    def retire_card(self, run_id: str, prefix: str) -> None:
        with self.lock:
            run = self.store.get_run(run_id)
            if run.get("tg_message_id") is None:
                return
            task = self.store.get_task(run["task_id"])
            text = render_code(task, run, prefix, pr_state=self.pr_state(run))
            self.telegram.edit_card(run, text, [])
            self.store.meta_set(f"code.card_pending.{run_id}", "")

    # -- merge -----------------------------------------------------------------

    def _refuse_merge(self, task: dict[str, Any], run: dict[str, Any], message: str) -> None:
        """The card the way the existing merge refusals render one: the
        refusal as its prefix line, Merge still offered (buttons unchanged)."""
        self.telegram.edit_card(run, render_code(task, run, message, pr_state="opened"),
                                code_buttons(task, run))
        raise TransitionError(message)

    def _require_integration_check(self, task: dict[str, Any], run: dict[str, Any],
                                   owner: str, repo: str) -> None:
        check = run["integration"]
        if check is None:
            self._refuse_merge(task, run, f"no integration check; run aos integrate {task['id']}")
        if check["head_sha"] != run["pr_head_sha"]:
            self._refuse_merge(task, run, "integration check is for another commit; run aos integrate again")
        # C1: the PR's base is read from GitHub on every call -- a PR can be
        # retargeted after the check ran, and a stale `pr_base` (including an
        # empty-string sentinel from before this repair) must never lock the
        # gate: it stays whatever GitHub answers right now.
        try:
            pr = self.github.get_pr(owner, repo, run["pr_number"])
        except GitHubRefused as exc:
            self._note_refusal(exc)
            self._refuse_merge(task, run, str(exc))
            return  # unreachable: _refuse_merge always raises
        pr_base = pr["base"]["ref"]
        if pr_base != (run.get("pr_base") or ""):
            run = self.store.annotate_run(run["id"], pr_base=pr_base)
        if check["base_branch"] != pr_base:
            self._refuse_merge(task, run, (
                f"integration check ran onto {check['base_branch']}, the PR merges into {pr_base}; "
                f"run aos integrate again"))
        if check["status"] != "passed":
            self._refuse_merge(task, run, f"integration check {check['status']}; requeue or fix")
        try:
            current_base_sha = self.github.branch_head(owner, repo, check["base_branch"])
        except GitHubRefused as exc:
            self._note_refusal(exc)
            self._refuse_merge(task, run, str(exc))
            return  # unreachable: _refuse_merge always raises
        if current_base_sha != check["base_sha"]:
            self._refuse_merge(task, run, (
                f"{check['base_branch']} moved since the integration check "
                f"(tested {hash8(check['base_sha'])}, now {hash8(current_base_sha)}); "
                f"run aos integrate again"))

    def merge(self, task_id: str, sha: str, *, run_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            task = self.store.get_task(task_id)
            if task["status"] == "done":
                raise TransitionError("already done")
            if task["status"] != "review":
                raise TransitionError(f"task is not review ({task['status']})")
            run = self._newest_review_run(task)
            unclosed = self.merged_unclosed(run["id"])
            if unclosed:
                # The PR is merged on GitHub, the task not closed: retry the
                # completion locally, no PUT and no merge:requested receipt.
                return self._complete(task, run, unclosed, recorded=True)
            if run_id is not None and run_id != run["id"]:
                raise TransitionError(f"stale card: {run_id} is not the task's newest run")
            if run.get("pr_number") is None:
                if self.pr_step(run["id"]) != "on":
                    raise TransitionError("no PR on the review run; close with aos accept")
                raise TransitionError("PR not opened yet; wait for the sweep or requeue")
            if run.get("pr_head_sha") is None:
                raise TransitionError("PR head unknown, wait for the sweep")
            if len(sha) < 7:
                raise TransitionError("sha too short: give at least 7 characters")
            if run["pr_head_sha"] != run.get("result_commit"):
                self.telegram.edit_card(run, render_code(task, run, pr_state="opened"),
                                        code_buttons(task, run))
                raise TransitionError("PR head is not the acceptance commit, re-review")
            if not run["pr_head_sha"].startswith(sha):
                self.telegram.edit_card(run, render_code(task, run, pr_state="opened"),
                                        code_buttons(task, run))
                raise TransitionError("PR head changed, re-review")
            if task["spec_hash"] != run["spec_hash"]:
                raise TransitionError("spec has changed since the run was claimed")
            if time.monotonic() < self.github_paused_until:
                wait = math.ceil(self.github_paused_until - time.monotonic())
                raise TransitionError(f"GitHub rate limited; retry in {wait} s")
            owner, repo = parse_remote(run["remote"])
            if self.settings.CODE_INTEGRATION_CHECK == "require":
                self._require_integration_check(task, run, owner, repo)
            self.store.task_event("merge:requested", task_id=task["id"], run_id=run["id"],
                                  pr_head_sha=run["pr_head_sha"])
            try:
                response = self.github.merge_pr(owner, repo, run["pr_number"], sha=run["pr_head_sha"],
                                                merge_method=self.settings.GITHUB_MERGE_METHOD)
            except GitHubRefused as exc:
                self._note_refusal(exc)
                self.store.task_event("merge:refused", task_id=task["id"], run_id=run["id"],
                                      message=str(exc))
                self.telegram.edit_card(run, render_code(
                    task, run, f"⛔ merge refused: {str(exc)[:200]}", pr_state="opened"),
                    code_buttons(task, run))
                raise TransitionError(str(exc)) from exc
            if not response.get("merged"):
                message = response.get("message", "")
                self.store.task_event("merge:refused", task_id=task["id"], run_id=run["id"],
                                      message=message)
                self.telegram.edit_card(run, render_code(
                    task, run, f"⛔ merge refused: {message[:200]}", pr_state="opened"),
                    code_buttons(task, run))
                raise TransitionError(message)
            return self._complete(task, run, response["sha"])

    def _complete(self, task: dict[str, Any], run: dict[str, Any], sha: str, *,
                 recorded: bool = False) -> dict[str, Any]:
        if not recorded:
            self.store.task_event("merged", task_id=task["id"], run_id=run["id"], merge_commit=sha)
        try:
            self.store.complete_task(task["id"], merge_commit=sha, run_id=run["id"])
        except TransitionError as exc:
            self.store.meta_set(f"code.merged_unclosed.{run['id']}", sha)
            self.store.meta_set(f"code.card_pending.{run['id']}", "")
            self.telegram.edit_card(run, render_code(
                task, run, f"✅ merged {hash8(sha)} · task NOT closed: {str(exc)[:120]}",
                pr_state="opened"), [])
            if not recorded:
                self.telegram.send(
                    self.settings.TELEGRAM_CHAT_NEEDS_YOU,
                    f"⚠️ {task['id']}: PR #{run.get('pr_number')} merged at {hash8(sha)} but the "
                    f"task could not be closed: {str(exc)[:200]}")
            raise TransitionError(
                f"merged {hash8(sha)} on GitHub but the task could not be closed: "
                f"{str(exc)[:200]}") from exc
        ok = self.telegram.edit_card(run, render_code(task, run, f"✅ merged {hash8(sha)}",
                                                       pr_state="opened"), [])
        if not ok and run.get("tg_message_id") is not None:
            self.telegram.send(
                self.settings.TELEGRAM_CHAT_NEEDS_YOU,
                f"⚠️ {task['id']}: merged {hash8(sha)} but the card could not be updated")
        return self.store.get_task(task["id"])

    def merge_callback(self, run_id: str, sha8: str) -> dict[str, Any]:
        return self.merge(self.store.get_run(run_id)["task_id"], sha8, run_id=run_id)

    def requeue(self, task_id: str, *, run_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            task = self.store.get_task(task_id)
            run = self._newest_review_run(task) if task["status"] == "review" else None
            if run_id is not None and (run is None or run_id != run["id"]):
                raise TransitionError(f"stale card: {run_id} is not the task's newest run")
            task = self.store.requeue_task(task_id, "backlog")
            if run is not None:
                self.store.meta_set(f"code.card_pending.{run['id']}", "")
                self.telegram.edit_card(run, render_code(
                    task, run, "↩ requeued", pr_state=self.pr_state(run)), [])
            return task

    # -- sweep -----------------------------------------------------------------

    def poll(self) -> None:
        started = utcnow()
        errors = 0
        try:
            for row in self.store.list_tasks(status="review"):
                with self.lock:
                    try:
                        task = self.store.get_task(row["id"])
                        if task["status"] != "review":
                            continue
                        run = self._newest_review_run(task)
                        if run is None:
                            continue
                        if run.get("pr_number") is not None:
                            unclosed = self.merged_unclosed(run["id"])
                            if unclosed:
                                try:
                                    self._complete(task, run, unclosed, recorded=True)
                                except TransitionError:
                                    logger.debug(
                                        "code lane poll: completion retry still refused for %s",
                                        run["id"])
                                continue
                            if (self.github.enabled and parse_remote(run.get("remote")) is not None
                                    and time.monotonic() >= self.github_paused_until):
                                owner, repo = parse_remote(run["remote"])
                                pr = self.github.get_pr(owner, repo, run["pr_number"])
                                if pr.get("merged"):
                                    self._complete(task, run, pr["merge_commit_sha"])
                                    continue
                            self._sync_card(task, run)
                        else:
                            if (self.pr_step(run["id"]) == "on" and self.github.enabled
                                    and parse_remote(run.get("remote")) is not None
                                    and self.pr_wanted(task)):
                                run = self._open_pr(task, run)
                            self._sync_card(task, run)
                    except Exception as exc:
                        if isinstance(exc, GitHubRefused):
                            self._note_refusal(exc)
                        errors += 1
                        logger.warning("code lane poll failed for %s: %s: %s",
                                       row["id"], type(exc).__name__, exc)
        finally:
            self.store.record_run("code.poll", started, utcnow(),
                                  "ok" if errors == 0 else f"error:{errors} run(s) failed")
