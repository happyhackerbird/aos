from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_os.api import create_app
from agent_os.config import Settings
from agent_os.github import GitHub
from agent_os.lanes.code import CodeLane
from agent_os.store import Store
from agent_os.telegram import Telegram
from conftest import FakeHTTP


@pytest.fixture(autouse=True)
def _no_github_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


class FakeGitHub:
    """Records every request; keyed on path, GITHUB_API_BASE = https://github.test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.default_branch = "main"
        self.open_prs: list[dict[str, Any]] = []
        self.pr_number = 42
        self.pr_html_url = "https://github.test/o/r/pull/42"
        self.pr_head_sha = "a" * 40
        self.post_already_exists = False
        self.reused_pr: dict[str, Any] | None = None
        self.post_error: tuple[int, str] | None = None
        self.merged = False
        self.merge_commit_sha = "m" * 40
        self.merge_error: tuple[int, str] | None = None
        self.unreachable_open = False
        self.unreachable_get_pr = False
        self.unreachable_merge = False
        # U3: the base branch's current head, as branch_head answers it.
        self.base_branch = "main"
        self.base_sha = "b" * 40
        self.branch_head_error: tuple[int, str] | None = None
        # A1: the PR's own base, as get_pr answers it (the pr_base fallback read).
        self.pr_base = "main"
        # Called (no args) right before a successful PUT .../merge answers 200,
        # so a test can mutate the task (edit the spec, requeue it) mid-request
        # to simulate the race the store's own transition guards are for.
        self.on_put_merge: Callable[[], None] | None = None
        self.repo_error: tuple[int, str] | None = None
        self._posted_once = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, body))
        if self.unreachable_open and path == "/repos/o/r/pulls" and method in ("GET", "POST"):
            raise httpx.ConnectError("github unreachable", request=request)
        if (self.unreachable_merge and method == "PUT"
                and path == f"/repos/o/r/pulls/{self.pr_number}/merge"):
            raise httpx.ConnectError("github unreachable", request=request)
        if method == "GET" and path == "/repos/o/r":
            if self.repo_error:
                status, message = self.repo_error
                return httpx.Response(status, json={"message": message})
            return httpx.Response(200, json={"default_branch": self.default_branch})
        if method == "GET" and path == "/repos/o/r/pulls":
            if self.repo_error:
                status, message = self.repo_error
                return httpx.Response(status, json={"message": message})
            if self._posted_once and self.reused_pr is not None:
                return httpx.Response(200, json=[self.reused_pr])
            return httpx.Response(200, json=self.open_prs)
        if method == "POST" and path == "/repos/o/r/pulls":
            if self.post_already_exists:
                self._posted_once = True
                return httpx.Response(422, json={"message": "Validation Failed",
                                                 "errors": [{"message": "A pull request already exists"}]})
            if self.post_error:
                status, message = self.post_error
                return httpx.Response(status, json={"message": message})
            return httpx.Response(201, json={"number": self.pr_number, "html_url": self.pr_html_url,
                                             "head": {"sha": self.pr_head_sha}})
        if method == "GET" and path == f"/repos/o/r/pulls/{self.pr_number}":
            if self.unreachable_get_pr:
                raise httpx.ConnectError("github unreachable", request=request)
            return httpx.Response(200, json={"head": {"sha": self.pr_head_sha}, "merged": self.merged,
                                             "merge_commit_sha": self.merge_commit_sha,
                                             "base": {"ref": self.pr_base}})
        if method == "PUT" and path == f"/repos/o/r/pulls/{self.pr_number}/merge":
            if self.merge_error:
                status, message = self.merge_error
                return httpx.Response(status, json={"message": message})
            if self.on_put_merge is not None:
                self.on_put_merge()
            return httpx.Response(200, json={"merged": True, "sha": self.merge_commit_sha})
        if method == "GET" and path.startswith("/repos/o/r/branches/"):
            if self.branch_head_error:
                status, message = self.branch_head_error
                return httpx.Response(status, json={"message": message})
            return httpx.Response(200, json={"commit": {"sha": self.base_sha}})
        raise AssertionError(f"unexpected request {method} {request.url}")


def build_code_bus(tmp_path: Path, fake: FakeHTTP, github_fake: FakeGitHub,
                   **overrides: Any) -> tuple[TestClient, Store, Settings, CodeLane]:
    kwargs: dict[str, Any] = dict(_env_file=None, BUS_TOKEN="test-bus-token", BUS_DATA_DIR=tmp_path,
                                  TELEGRAM_BOT_TOKEN="fake-token",
                                  TELEGRAM_CHAT_NEEDS_YOU="-200", TELEGRAM_API_BASE="https://telegram.test",
                                  GITHUB_API_BASE="https://github.test", GITHUB_TOKEN="ghp-test",
                                  TELEGRAM_CHAT_CODE="-300")
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    store = Store(settings.BUS_DATA_DIR)
    telegram = Telegram(settings, httpx.Client(transport=httpx.MockTransport(fake.handle)))
    github = GitHub(settings, httpx.Client(transport=httpx.MockTransport(github_fake.handle)))
    code_lane = CodeLane(store, telegram, settings, github=github, background=False)
    app = create_app(settings, store=store, telegram=telegram, code_lane=code_lane,
                     background=False)
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer test-bus-token"
    return client, store, settings, code_lane


def _setup_task(client: TestClient, *, acceptance: list[str] | None = None,
                deliver: str | None = "pr",
                inherited: list[str] | None = None) -> tuple[dict, dict]:
    # Part C (task-flow chain): a pull request is opened only for a repo registered with
    # deliver=pr; the default is push only. The lane's PR tests register repo-a that way.
    if deliver is not None:
        registered = client.put("/repos/repo-a", json={"default_host": "any", "deliver": deliver})
        assert registered.status_code == 200, registered.text
    body = {"repo": "repo-a", "title": "Fix the bug", "brief": "do it",
           "acceptance": acceptance if acceptance is not None else ["true"]}
    task = client.post("/tasks", json=body).json()
    assert client.post(f"/tasks/{task['id']}/ready").status_code == 200
    snapshot = ({"effective_acceptance": inherited, "acceptance_source": "workflow.acceptance",
                 "workflow_commit": "default-commit", "workflow_ref": "main", "workflow_sha": "workflow-sha"}
                if inherited is not None else
                {"effective_acceptance": task["acceptance"], "acceptance_source": "task"})
    run = client.post(f"/tasks/{task['id']}/claim", json={
        "worker": "w1", "worker_host": "mac", "expected_spec_hash": task["spec_hash"], **snapshot,
    }).json()
    started = client.post(f"/runs/{run['id']}/start",
                          json={"claim_token": run["claim_token"], "base_commit": "b1",
                                "branch": f"ao/{task['id']}", "worktree": "/x"})
    assert started.status_code == 200, started.text
    return task, run


def _post_result(client: TestClient, run: dict, *, status: str = "review",
                 remote: str | None = "git@github.com:o/r.git", report: str = "did it",
                 result_commit: str | None = None) -> dict:
    body: dict[str, Any] = {"claim_token": run["claim_token"], "status": status, "report": report,
                            "result_commit": result_commit or "c" * 40, "remote": remote}
    if status == "review":
        body["verdict"] = "pass"
    else:
        body["error"] = "boom"
    response = client.post(f"/runs/{run['id']}/result", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _record_passing_check(store: Store, github_fake: FakeGitHub, task: dict, run: dict) -> dict:
    """U3: a passing integration check for the run's current pr_head_sha, onto
    the base sha the fake GitHub will answer for branch_head -- so CodeLane.merge's
    CODE_INTEGRATION_CHECK='require' gate (the setting's default) passes."""
    return store.record_integration(
        task["id"], run["id"], head_sha=run["pr_head_sha"], base_branch=github_fake.base_branch,
        base_sha=github_fake.base_sha, status="passed", output="ok", worker="test")


def _setup_merge_ready_no_check(client: TestClient, store: Store, github_fake: FakeGitHub, *,
                                result_commit: str = "c" * 40) -> tuple[dict, dict]:
    """Like _setup_merge_ready but WITHOUT recording an integration check --
    for the U3 refusal tests, which need CodeLane.merge to see run["integration"]
    as None or as a specific non-passing shape."""
    github_fake.pr_head_sha = result_commit
    task, run = _setup_task(client)
    _post_result(client, run, result_commit=result_commit)
    return task, store.get_run(run["id"])


def _setup_merge_ready(client: TestClient, store: Store, github_fake: FakeGitHub, *,
                       result_commit: str = "c" * 40) -> tuple[dict, dict]:
    """A task in review with a PR already open whose head matches result_commit,
    i.e. ready for CodeLane.merge with no re-render refusal. Also carries a
    passing U3 integration check, since CODE_INTEGRATION_CHECK defaults to
    'require': every existing merge test keeps that default and gets its
    passing check from here; only the test for 'off' turns the setting off."""
    task, run = _setup_merge_ready_no_check(client, store, github_fake, result_commit=result_commit)
    _record_passing_check(store, github_fake, task, run)
    return task, store.get_run(run["id"])


def _last_card(fake: FakeHTTP, chat: str = "-300") -> dict[str, Any]:
    """The latest card body for `chat`, whether it was a fresh send or an edit
    of an already-posted card (fake.messages() only sees sendMessage calls)."""
    matches = [call for call in fake.calls
              if call[1].endswith(("sendMessage", "editMessageText")) and call[2].get("chat_id") == chat]
    return matches[-1][2]


def _finish_result_directly(store: Store, code_lane: CodeLane, run: dict, *, status: str = "review",
                            remote: str | None = "git@github.com:o/r.git", report: str = "did it",
                            result_commit: str | None = None) -> dict:
    """Bypasses the HTTP route (and its notify_result call) to simulate the
    result having landed while the on_result thread has not run yet."""
    marks = code_lane.result_marks(run["id"], status, remote)
    kwargs: dict[str, Any] = dict(status=status, report=report, result_commit=result_commit or "c" * 40,
                                  remote=remote, marks=marks)
    if status == "review":
        kwargs["verdict"] = "pass"
    else:
        kwargs["error"] = "boom"
    return store.finish_run(run["id"], run["claim_token"], **kwargs)


# -- result / marks -----------------------------------------------------------

def test_result_stores_remote_and_marks_exist_in_same_response(tmp_path: Path, fake: FakeHTTP) -> None:
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, FakeGitHub())
    task, run = _setup_task(client)
    body = _post_result(client, run)
    assert body["remote"] == "git@github.com:o/r.git"
    assert store.meta_get(f"code.pr_step.{run['id']}") == "on"


# -- per-repo delivery switch (task-flow Part C) ---------------------------------

def test_repo_set_to_push_gets_no_pr_shows_the_branch_and_accepts(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client, deliver="push")
    _post_result(client, run)  # a GitHub remote and a token on the bus: still no PR for a push-only repo
    assert not github_fake.calls
    assert store.meta_get(f"code.pr_step.{run['id']}") == "off"
    assert store.get_run(run["id"])["pr_number"] is None
    card = fake.messages("-300")[-1][2]["text"]
    assert "no PR: close with aos accept" in card
    assert f"branch https://github.com/o/r/tree/ao/{task['id']}" in card
    code_lane.poll()
    assert not github_fake.calls
    accepted = client.post(f"/tasks/{task['id']}/accept")
    assert accepted.status_code == 200, accepted.text


def test_unregistered_repo_defaults_to_push_only(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client, deliver=None)
    _post_result(client, run)
    assert not github_fake.calls and store.meta_get(f"code.pr_step.{run['id']}") == "off"


def test_repo_switched_to_push_after_the_result_opens_no_pr_on_poll_or_on_result(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)  # deliver=pr at result time
    finished = _finish_result_directly(store, code_lane, run)
    assert store.meta_get(f"code.pr_step.{run['id']}") == "on"
    assert client.put("/repos/repo-a", json={"default_host": "any", "deliver": "push"}).status_code == 200
    code_lane.poll()
    code_lane.on_result(finished)
    assert not github_fake.calls and store.get_run(run["id"])["pr_number"] is None


def test_repo_set_to_pr_opens_one_and_the_card_shows_the_branch(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client, deliver="pr")
    _post_result(client, run)
    assert [c for c in github_fake.calls if c[0] == "POST"]
    card = fake.messages("-300")[-1][2]["text"]
    assert f"branch https://github.com/o/r/tree/ao/{task['id']}" in card and "PR #42" in card


def test_card_without_a_github_remote_has_no_branch_line(tmp_path: Path, fake: FakeHTTP) -> None:
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, FakeGitHub())
    task, run = _setup_task(client, deliver="push")
    _post_result(client, run, remote="/srv/git/r.git")
    assert "branch " not in fake.messages("-300")[-1][2]["text"]


# -- PR open on the result path -----------------------------------------------

def test_pr_opened_on_result_path_and_card_shows_pr_and_merge_button(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    github_fake.pr_number, github_fake.pr_head_sha = 55, "c" * 40
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run, result_commit="c" * 40)

    posts = [c for c in github_fake.calls if c[0] == "POST"]
    assert len(posts) == 1
    _, _, sent = posts[0]
    assert sent["head"] == f"ao/{task['id']}" and sent["base"] == "main"
    assert sent["title"] == f"{task['id']}: Fix the bug"
    assert "acceptance:\n  true" in sent["body"]
    assert "['" not in sent["body"]

    saved = store.get_run(run["id"])
    assert (saved["pr_number"], saved["pr_url"], saved["pr_head_sha"]) == (
        55, github_fake.pr_html_url, "c" * 40)

    events = [json.loads(line) for line in store.runs_path.read_text().splitlines()]
    assert any(isinstance(e.get("receipt"), dict) and e["receipt"].get("event") == "pr:opened"
              for e in events)

    # A1: _open_pr stores the base it opened the PR against, in the same
    # annotate_run call that stores the PR number.
    assert saved["pr_base"] == "main"

    card = fake.messages("-300")[-1][2]
    assert "PR #55" in card["text"]
    row = card["reply_markup"]["inline_keyboard"][0]
    assert any(b["text"] == "Merge" for b in row)


def test_head_get_finds_open_pr_reuses_no_post(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    github_fake.open_prs = [{"number": 9, "html_url": "u9", "head": {"sha": "f" * 40}}]
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run)
    assert not [c for c in github_fake.calls if c[0] == "POST"]
    assert store.get_run(run["id"])["pr_number"] == 9


def test_422_already_exists_reuses(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    github_fake.post_already_exists = True
    github_fake.reused_pr = {"number": 21, "html_url": "u21", "head": {"sha": "a" * 40}}
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run)
    assert store.get_run(run["id"])["pr_number"] == 21
    assert len([c for c in github_fake.calls if c[0] == "POST"]) == 1


def test_head_sha_mismatch_card_has_no_merge_and_warning_line(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    github_fake.pr_head_sha = "b" * 40
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run, result_commit="c" * 40)
    card = fake.messages("-300")[-1][2]
    assert "⚠️ PR head" in card["text"]
    row = card["reply_markup"]["inline_keyboard"][0]
    assert not any(b["text"] == "Merge" for b in row)

    # U2: aos merge (POST /tasks/{id}/merge) with no --sha refused 409 with no PUT
    puts_before = len([c for c in github_fake.calls if c[0] == "PUT"])
    merge_response = client.post(f"/tasks/{task['id']}/merge",
                                 json={"pr_head_sha": github_fake.pr_head_sha[:8]})
    assert merge_response.status_code == 409
    assert "PR head is not the acceptance commit" in merge_response.json()["detail"]
    assert len([c for c in github_fake.calls if c[0] == "PUT"]) == puts_before


# -- retry: network failure, 404/422, rate limit -----------------------------

def test_network_failure_unbounded_retry_one_needs_you_at_threshold(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    github_fake.unreachable_open = True
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake, FAIL_THRESHOLD=3)
    task, run = _setup_task(client)
    _post_result(client, run)  # attempt 1
    for _ in range(4):
        code_lane.poll()  # attempts 2..5

    pings = [m for m in fake.messages("-200") if "PR not opened after" in m[2]["text"]]
    assert len(pings) == 1
    assert code_lane.attempts(run["id"]) == 5
    card = fake.messages("-300")[-1][2]
    lines = card["text"].split("\n")
    # F132: the pending line names the error, never an attempt count -- exact
    # line equality, so a suffix or prefix (an attempt count anywhere) fails.
    assert "PR pending (retrying) · ConnectError: github unreachable" in lines
    assert "attempts" not in card["text"]

    github_fake.unreachable_open = False
    code_lane.poll()
    assert store.get_run(run["id"])["pr_number"] is not None
    # no second needs-you line after the PR opens
    pings_after = [m for m in fake.messages("-200") if "PR not opened after" in m[2]["text"]]
    assert len(pings_after) == 1


def test_404_retried_then_succeeds(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    github_fake.repo_error = (404, "Not Found")
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run)
    card = fake.messages("-300")[-1][2]
    assert "GitHubRefused: Not Found" in card["text"]

    github_fake.repo_error = None
    code_lane.poll()
    assert store.get_run(run["id"])["pr_number"] is not None
    card2 = _last_card(fake)
    assert "PR #" in card2["text"]


def test_403_rate_limit_pauses_the_lane(tmp_path: Path, fake: FakeHTTP, monkeypatch: pytest.MonkeyPatch) -> None:
    github_fake = FakeGitHub()
    github_fake.repo_error = (403, "rate limited")
    real_handle = github_fake.handle

    def handle_with_retry_after(request: httpx.Request) -> httpx.Response:
        response = real_handle(request)
        if response.status_code == 403:
            return httpx.Response(403, json={"message": "rate limited"}, headers={"Retry-After": "120"})
        return response

    github_fake.handle = handle_with_retry_after  # type: ignore[method-assign]
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run)
    assert code_lane.github_paused_until > time.monotonic()

    calls_before = len(github_fake.calls)
    code_lane.poll()
    assert len(github_fake.calls) == calls_before  # paused: no GitHub request at all

    # U2: aos merge (POST /tasks/{id}/merge) on another task answers 409 while paused
    task2, run2 = _setup_task(client)
    _post_result(client, run2, remote="git@github.com:o/r.git", result_commit="d" * 40)
    store.annotate_run(run2["id"], pr_number=99, pr_url="u99", pr_head_sha="d" * 40)
    calls_before2 = len(github_fake.calls)
    merge_response = client.post(f"/tasks/{task2['id']}/merge", json={"pr_head_sha": "d" * 8})
    assert merge_response.status_code == 409
    assert "GitHub rate limited; retry in" in merge_response.json()["detail"]
    assert len(github_fake.calls) == calls_before2  # no GitHub request while paused

    monkeypatch.setattr(time, "monotonic", lambda: code_lane.github_paused_until + 1)
    github_fake.repo_error = None
    code_lane.poll()
    assert store.get_run(run["id"])["pr_number"] is not None


# -- accept refusals while the PR step is on ---------------------------------

def test_accept_refused_while_pr_step_on_no_pr_open(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    github_fake.unreachable_open = True
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run)
    calls_before = len(github_fake.calls)
    response = client.post(f"/tasks/{task['id']}/accept")
    assert response.status_code == 409
    assert "task has a PR step on" in response.json()["detail"]
    assert len(github_fake.calls) == calls_before


# -- marked off / marked on token cases ---------------------------------------

def test_marked_off_gets_no_pr_even_after_token_is_set_later(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake, GITHUB_TOKEN="")
    code_lane.github.enabled = False
    task, run = _setup_task(client)
    _post_result(client, run)
    assert store.meta_get(f"code.pr_step.{run['id']}") == "off"
    code_lane.github.enabled = True  # the token appears later
    code_lane.poll()
    assert not github_fake.calls


def test_marked_on_gets_no_request_after_token_removed(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _finish_result_directly(store, code_lane, run)  # marks set, PR not yet opened
    assert store.meta_get(f"code.pr_step.{run['id']}") == "on"
    code_lane.github.enabled = False
    code_lane.poll()
    assert not github_fake.calls


# -- marks alone / poll before the on_result thread ---------------------------

def test_marks_alone_then_poll_opens_pr_and_posts_card(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    finished = _finish_result_directly(store, code_lane, run)
    assert finished["pr_number"] is None
    code_lane.poll()
    assert store.get_run(run["id"])["pr_number"] is not None
    assert fake.messages("-300")


def test_poll_opens_before_on_result_thread_then_on_result_is_a_noop(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    finished = _finish_result_directly(store, code_lane, run)

    code_lane.poll()
    posts_after_poll = len([c for c in github_fake.calls if c[0] == "POST"])
    assert posts_after_poll == 1

    code_lane.on_result(finished)  # the delayed thread fires with its stale snapshot
    posts_after_on_result = len([c for c in github_fake.calls if c[0] == "POST"])
    assert posts_after_on_result == 1


def test_task_requeued_before_on_result_ran_opens_no_pr(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    finished = _finish_result_directly(store, code_lane, run)
    assert client.post(f"/tasks/{task['id']}/requeue", json={"to": "backlog"}).status_code == 200
    code_lane.on_result(finished)
    assert not github_fake.calls


# -- card send/edit retries ----------------------------------------------------

def test_first_card_send_failed_then_poll_posts_card(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    fake.telegram_error = True
    _post_result(client, run)
    assert store.get_run(run["id"])["tg_message_id"] is None
    fake.telegram_error = False
    code_lane.poll()
    assert store.get_run(run["id"])["tg_message_id"] is not None


def test_card_edit_failed_then_next_poll_edits(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run)
    assert store.get_run(run["id"])["tg_message_id"] is not None

    store.meta_set(f"code.card_pending.{run['id']}", "1")
    fake.telegram_error = True
    code_lane.poll()
    assert code_lane.card_pending(run["id"])
    fake.telegram_error = False
    code_lane.poll()
    assert not code_lane.card_pending(run["id"])


def test_card_pending_cleared_when_task_no_longer_review(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    finished = _finish_result_directly(store, code_lane, run)
    assert code_lane.card_pending(run["id"])
    assert client.post(f"/tasks/{task['id']}/requeue", json={"to": "backlog"}).status_code == 200
    task_now = store.get_task(task["id"])
    code_lane._sync_card(task_now, finished)
    assert not fake.messages("-300")
    assert not code_lane.card_pending(run["id"])


# -- card content / buttons ---------------------------------------------------

def test_card_posted_with_buttons_and_ids_stored(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    github_fake.pr_head_sha = "c" * 40
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run, result_commit="c" * 40)
    saved = store.get_run(run["id"])
    assert saved["tg_chat_id"] == "-300" and saved["tg_message_id"] is not None
    card = fake.messages("-300")[-1][2]
    row = card["reply_markup"]["inline_keyboard"][0]
    assert row[0]["callback_data"].startswith(f"mg:{run['id']}:")
    assert row[1]["callback_data"] == f"rq:{task['id']}:{run['id']}"


def test_no_remote_card_has_accept_line_no_merge(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run, remote=None)
    card = fake.messages("-300")[-1][2]
    assert "no PR: close with aos accept" in card["text"]
    row = card["reply_markup"]["inline_keyboard"][0]
    assert not any(b["text"] == "Merge" for b in row)
    assert not github_fake.calls


def test_non_github_remote_no_pr_request(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run, remote="https://gitlab.com/o/r.git")
    assert not github_fake.calls
    assert store.meta_get(f"code.pr_step.{run['id']}") == "off"
    card = fake.messages("-300")[-1][2]
    assert "no PR: close with aos accept" in card["text"]
    row = card["reply_markup"]["inline_keyboard"][0]
    assert not any(b["text"] == "Merge" for b in row)
    assert any(b["text"] == "Requeue" for b in row)


def test_github_token_empty_no_request_and_bus_starts(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake, GITHUB_TOKEN="")
    assert not code_lane.github.enabled
    task, run = _setup_task(client)
    _post_result(client, run)
    assert not github_fake.calls
    assert store.meta_get(f"code.pr_step.{run['id']}") == "off"


# -- accept / requeue retire the card -----------------------------------------

def test_requeue_retires_card_no_buttons(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run, remote=None)
    assert client.post(f"/tasks/{task['id']}/requeue", json={"to": "backlog"}).status_code == 200
    card = fake.method("editMessageText")[-1][2]
    assert card["text"].startswith("↩ requeued") and card["reply_markup"]["inline_keyboard"] == []


def test_accept_retires_card_no_buttons(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run, remote=None)
    assert client.post(f"/tasks/{task['id']}/accept").status_code == 200
    card = fake.method("editMessageText")[-1][2]
    assert card["text"].startswith("✅ accepted") and card["reply_markup"]["inline_keyboard"] == []


def test_retire_card_edits_only_the_named_run(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task_a, run_a = _setup_task(client)
    _post_result(client, run_a, remote=None)
    task_b, run_b = _setup_task(client)
    _post_result(client, run_b, remote=None)

    edits_before = len(fake.method("editMessageText"))
    code_lane.retire_card(run_a["id"], "✅ accepted")
    edits = fake.method("editMessageText")[edits_before:]
    assert len(edits) == 1
    assert edits[0][2]["message_id"] == store.get_run(run_a["id"])["tg_message_id"]
    assert store.get_run(run_b["id"])["tg_message_id"] is not None


def test_retire_card_same_task_newer_run_untouched(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run1 = _setup_task(client)
    _post_result(client, run1, remote=None)
    run1_saved = store.get_run(run1["id"])
    assert run1_saved["tg_message_id"] is not None

    # Send the task back to "ready" through the store directly (not through
    # code_lane.requeue, which would retire run1's card) so a second run can
    # be claimed on the same task while run1's own review card survives.
    store.requeue_task(task["id"], "ready")

    run2 = client.post(f"/tasks/{task['id']}/claim", json={
        "worker": "w1", "worker_host": "mac", "expected_spec_hash": task["spec_hash"],
        "effective_acceptance": task["acceptance"], "acceptance_source": "task",
    }).json()
    started2 = client.post(f"/runs/{run2['id']}/start",
                           json={"claim_token": run2["claim_token"], "base_commit": "b2",
                                 "branch": f"ao/{task['id']}", "worktree": "/x"})
    assert started2.status_code == 200, started2.text
    _post_result(client, run2, remote=None)
    run2_saved = store.get_run(run2["id"])
    assert run2_saved["tg_message_id"] is not None
    assert run2_saved["id"] != run1_saved["id"]

    task_now = store.get_task(task["id"])
    reviews = [r for r in task_now["runs"] if r["status"] == "review"]
    assert {r["id"] for r in reviews} == {run1_saved["id"], run2_saved["id"]}

    edits_before = len(fake.method("editMessageText"))
    code_lane.retire_card(run1_saved["id"], "↩ requeued")
    edits = fake.method("editMessageText")[edits_before:]
    assert len(edits) == 1
    assert edits[0][2]["message_id"] == run1_saved["tg_message_id"]
    # run2's card is untouched: its stored ids are unchanged, and the only
    # edit above targeted run1's message id, not run2's.
    assert store.get_run(run2_saved["id"])["tg_message_id"] == run2_saved["tg_message_id"]
    assert edits[0][2]["message_id"] != run2_saved["tg_message_id"]


# -- failed result -------------------------------------------------------------

def test_failed_result_one_needs_you_message_no_buttons(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client, acceptance=["false"])
    _post_result(client, run, status="failed", remote="git@github.com:o/r.git")
    needs_you = fake.messages("-200")
    assert len(needs_you) == 1
    assert "reply_markup" not in needs_you[0][2]
    assert not github_fake.calls  # a failed result never opens a PR


# -- poll outcome / health --------------------------------------------------

def test_poll_outcome_error_on_non_httpx_exception_hides_detail_from_health(
        tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _finish_result_directly(store, code_lane, run)

    def boom(owner: str, repo: str) -> str:
        raise RuntimeError("boom from the fake")

    code_lane.github.default_branch = boom  # type: ignore[method-assign]
    code_lane.poll()
    lanes = {row["lane"]: row for row in store.lanes()}
    assert lanes["code.poll"]["last_outcome"] == "error:1 run(s) failed"
    health = client.get("/health").json()
    assert "boom" not in json.dumps(health)


def test_store_lanes_include_code_poll(tmp_path: Path, fake: FakeHTTP) -> None:
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, FakeGitHub())
    names = {row["lane"] for row in store.lanes()}
    assert "code.poll" in names


# ===========================================================================
# U2: merge, observe, requeue, callbacks
# ===========================================================================

# -- merge ----------------------------------------------------------------

def test_merge_matching_sha_completes_task_and_edits_card(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 200, response.text

    puts = [c for c in github_fake.calls if c[0] == "PUT"]
    assert len(puts) == 1
    assert puts[0][2]["sha"] == run["pr_head_sha"]

    assert response.json()["status"] == "done"
    saved_run = store.get_run(run["id"])
    assert saved_run["status"] == "passed"
    assert saved_run["merge_commit"] == github_fake.merge_commit_sha

    card = fake.method("editMessageText")[-1][2]
    assert card["text"].startswith("✅ merged")

    events = [json.loads(line) for line in store.runs_path.read_text().splitlines()]
    assert any(e.get("receipt", {}).get("event") == "merged" for e in events)


def test_merge_stale_sha_409_no_put_card_rerendered(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)

    edits_before = len(fake.method("editMessageText"))
    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": "f" * 8})
    assert response.status_code == 409
    assert "PR head changed, re-review" in response.json()["detail"]
    assert not [c for c in github_fake.calls if c[0] == "PUT"]
    assert len(fake.method("editMessageText")) == edits_before + 1


def test_merge_sha_too_short_422_from_body(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)

    calls_before = len(github_fake.calls)
    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": "abcdef"})
    assert response.status_code == 422
    assert len(github_fake.calls) == calls_before  # rejected by MergeBody, before the lane runs


def test_merge_github_409_through_run_stays_review(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    github_fake.merge_error = (409, "not mergeable")

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 409
    assert response.json()["detail"] == "not mergeable"
    assert store.get_run(run["id"])["status"] == "review"

    events = [json.loads(line) for line in store.runs_path.read_text().splitlines()]
    assert any(e.get("receipt", {}).get("event") == "merge:refused" for e in events)


def test_merge_spec_edited_after_pr_409_no_put(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)

    edit_response = client.patch(f"/tasks/{task['id']}", json={"constraints": "changed"})
    assert edit_response.status_code == 200, edit_response.text

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 409
    assert "spec has changed" in response.json()["detail"]
    assert not [c for c in github_fake.calls if c[0] == "PUT"]


def test_merge_spec_edited_during_merge_distinct_409_and_local_retry_refused(
        tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    sha8 = run["pr_head_sha"][:8]
    merge_sha8 = github_fake.merge_commit_sha[:8]  # _complete uses the PUT response's sha, not pr_head_sha

    def edit_spec() -> None:
        client.patch(f"/tasks/{task['id']}", json={"constraints": "changed mid-merge"})

    github_fake.on_put_merge = edit_spec
    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": sha8})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail == (
        f"merged {merge_sha8} on GitHub but the task could not be closed: "
        f"spec has changed since the run was claimed")

    assert store.get_run(run["id"])["status"] == "review"
    needs_you = fake.messages("-200")
    assert len(needs_you) == 1
    assert "could not be closed" in needs_you[0][2]["text"]
    card = fake.method("editMessageText")[-1][2]
    assert "task NOT closed" in card["text"]

    events = [json.loads(line) for line in store.runs_path.read_text().splitlines()]
    assert any(e.get("receipt", {}).get("event") == "merged" for e in events)

    # F133: the next poll retries completion locally and is refused again,
    # with no GitHub request in the fake log.
    github_fake.on_put_merge = None
    calls_before = len(github_fake.calls)
    code_lane.poll()
    assert len(github_fake.calls) == calls_before
    assert store.get_task(task["id"])["status"] == "review"


def test_merge_second_attempt_spec_still_changed_same_distinct_409_no_second_needs_you(
        tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    sha8 = run["pr_head_sha"][:8]
    merge_sha8 = github_fake.merge_commit_sha[:8]

    def edit_spec() -> None:
        client.patch(f"/tasks/{task['id']}", json={"constraints": "changed"})

    github_fake.on_put_merge = edit_spec
    client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": sha8})
    github_fake.on_put_merge = None

    puts_before = len([c for c in github_fake.calls if c[0] == "PUT"])
    response2 = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": sha8})
    assert response2.status_code == 409
    assert response2.json()["detail"] == (
        f"merged {merge_sha8} on GitHub but the task could not be closed: "
        f"spec has changed since the run was claimed")
    assert len([c for c in github_fake.calls if c[0] == "PUT"]) == puts_before
    assert len(fake.messages("-200")) == 1


def test_merge_second_attempt_after_spec_restored_completes_no_put(
        tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    sha8 = run["pr_head_sha"][:8]
    original_constraints = store.get_task(task["id"])["constraints"]

    def edit_spec() -> None:
        client.patch(f"/tasks/{task['id']}", json={"constraints": "changed"})

    github_fake.on_put_merge = edit_spec
    client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": sha8})
    github_fake.on_put_merge = None

    restore = client.patch(f"/tasks/{task['id']}", json={"constraints": original_constraints})
    assert restore.status_code == 200, restore.text

    puts_before = len([c for c in github_fake.calls if c[0] == "PUT"])
    response2 = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": sha8})
    assert response2.status_code == 200, response2.text
    assert len([c for c in github_fake.calls if c[0] == "PUT"]) == puts_before
    assert response2.json()["status"] == "done"
    saved_run = store.get_run(run["id"])
    assert saved_run["status"] == "passed" and saved_run["merge_commit"]
    card = fake.method("editMessageText")[-1][2]
    assert card["text"].startswith("✅ merged")

    events = [json.loads(line) for line in store.runs_path.read_text().splitlines()]
    merged_events = [e for e in events if e.get("receipt", {}).get("event") == "merged"]
    assert len(merged_events) == 1


def test_spec_restored_next_poll_completes_locally_no_github_request(
        tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    sha8 = run["pr_head_sha"][:8]
    original_constraints = store.get_task(task["id"])["constraints"]

    def edit_spec() -> None:
        client.patch(f"/tasks/{task['id']}", json={"constraints": "changed"})

    github_fake.on_put_merge = edit_spec
    client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": sha8})
    github_fake.on_put_merge = None

    restore = client.patch(f"/tasks/{task['id']}", json={"constraints": original_constraints})
    assert restore.status_code == 200, restore.text

    calls_before = len(github_fake.calls)
    code_lane.poll()
    assert len(github_fake.calls) == calls_before  # F117: no GitHub request
    assert store.get_task(task["id"])["status"] == "done"
    assert store.get_run(run["id"])["status"] == "passed"


def test_merge_task_requeued_during_merge_needs_you_names_transition_refusal(
        tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    sha8 = run["pr_head_sha"][:8]
    merge_sha8 = github_fake.merge_commit_sha[:8]

    def requeue_mid_merge() -> None:
        # Direct store call, not the HTTP route: the route's requeue_task also
        # calls code_lane.retire_card under the same lock merge() is holding
        # here, and a nested client.post from inside this hook (same call
        # stack, different threadpool worker) would deadlock on it.
        store.requeue_task(task["id"], "backlog")

    github_fake.on_put_merge = requeue_mid_merge
    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": sha8})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail.startswith(f"merged {merge_sha8} on GitHub but the task could not be closed:")
    assert "Forbidden task transition" in detail
    assert "spec has changed" not in detail

    assert store.get_task(task["id"])["status"] == "backlog"
    assert store.get_run(run["id"])["status"] == "review"
    needs_you = fake.messages("-200")
    assert len(needs_you) == 1
    assert "spec has changed" not in needs_you[0][2]["text"]


def test_merge_success_completion_edit_fails_one_needs_you(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    sha8 = run["pr_head_sha"][:8]

    fake.telegram_error = True
    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": sha8})
    assert response.status_code == 200, response.text
    needs_you = fake.messages("-200")
    assert len(needs_you) == 1
    assert "could not be updated" in needs_you[0][2]["text"]


def test_merge_double_tap_second_call_already_done_one_put(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    sha8 = run["pr_head_sha"][:8]

    r1 = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": sha8})
    assert r1.status_code == 200, r1.text
    r2 = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": sha8})
    assert r2.status_code == 409
    assert r2.json()["detail"] == "already done"
    assert len([c for c in github_fake.calls if c[0] == "PUT"]) == 1


def test_merge_network_failure_502_run_stays_review_no_card_edit(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    github_fake.unreachable_merge = True
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)

    edits_before = len(fake.method("editMessageText"))
    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 502
    assert store.get_run(run["id"])["status"] == "review"
    assert len(fake.method("editMessageText")) == edits_before


def test_merge_pr_403_rate_limit_pauses_lane(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    real_handle = github_fake.handle
    armed = {"on": False}

    # Same construction-order constraint as test_get_pr_429_pauses_lane: the
    # override must be installed before build_code_bus, and armed only after
    # the merge-ready setup's own PR-open call has already succeeded.
    def handle_merge_403_when_armed(request: httpx.Request) -> httpx.Response:
        if (armed["on"] and request.method == "PUT"
                and request.url.path == f"/repos/o/r/pulls/{github_fake.pr_number}/merge"):
            return httpx.Response(403, json={"message": "rate limited on merge"},
                                  headers={"Retry-After": "90"})
        return real_handle(request)

    github_fake.handle = handle_merge_403_when_armed  # type: ignore[method-assign]
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    armed["on"] = True

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 409
    assert response.json()["detail"] == "rate limited on merge"
    assert code_lane.github_paused_until > time.monotonic()

    calls_before = len(github_fake.calls)
    code_lane.poll()  # still within the pause
    assert len(github_fake.calls) == calls_before  # no GitHub request at all

    response2 = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response2.status_code == 409
    detail2 = response2.json()["detail"]
    assert detail2.startswith("GitHub rate limited; retry in") and detail2.endswith(" s")


def test_merge_without_token_409_no_request(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake, GITHUB_TOKEN="")
    task, run = _setup_task(client)
    _post_result(client, run, remote="git@github.com:o/r.git", result_commit="c" * 40)
    store.annotate_run(run["id"], pr_number=1, pr_url="u1", pr_head_sha="c" * 40)
    # U3: a passing check so the merge reaches GitHub's own call (branch_head)
    # instead of being refused earlier for having no check at all -- the point
    # of this test is the disabled-client refusal, not the integration gate.
    _record_passing_check(store, github_fake, task, store.get_run(run["id"]))

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": "c" * 8})
    assert response.status_code == 409
    assert response.json()["detail"] == "GITHUB_TOKEN is not set"
    assert not github_fake.calls


# -- merge observed by poll -------------------------------------------------

def test_poll_completes_run_when_pr_shows_merged_no_put(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    github_fake.merged = True

    code_lane.poll()
    assert not [c for c in github_fake.calls if c[0] == "PUT"]
    assert store.get_task(task["id"])["status"] == "done"
    saved_run = store.get_run(run["id"])
    assert saved_run["status"] == "passed"
    assert saved_run["merge_commit"] == github_fake.merge_commit_sha


def test_merged_observed_while_card_pending_no_extra_card(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    store.meta_set(f"code.card_pending.{run['id']}", "1")
    github_fake.merged = True

    edits_before = len(fake.method("editMessageText"))
    code_lane.poll()
    assert store.get_task(task["id"])["status"] == "done"
    edits = fake.method("editMessageText")[edits_before:]
    assert len(edits) == 1  # only _complete's own edit; no separate pending-card sync
    assert edits[0][2]["text"].startswith("✅ merged")


def test_get_pr_429_pauses_lane(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    real_handle = github_fake.handle
    armed = {"on": False}

    # MockTransport binds to github_fake.handle at build_code_bus time, so
    # the override must be installed before that call; the fault is armed
    # only after the merge-ready setup (whose own PR-open call must succeed).
    def handle_get_pr_429_when_armed(request: httpx.Request) -> httpx.Response:
        if (armed["on"] and request.method == "GET"
                and request.url.path == f"/repos/o/r/pulls/{github_fake.pr_number}"):
            return httpx.Response(429, json={"message": "rate limited"}, headers={"Retry-After": "120"})
        return real_handle(request)

    github_fake.handle = handle_get_pr_429_when_armed  # type: ignore[method-assign]
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    armed["on"] = True

    before = time.monotonic()
    code_lane.poll()
    assert code_lane.github_paused_until > before

    # Another pending task, no PR yet: it would draw an open_pr POST if not
    # for the pause.
    task2, run2 = _setup_task(client)
    finished2 = _finish_result_directly(store, code_lane, run2)
    assert finished2["pr_number"] is None

    calls_before = len(github_fake.calls)
    code_lane.poll()  # still within the pause; monotonic has not advanced
    assert len(github_fake.calls) == calls_before  # no GitHub request at all


def test_poll_outcome_error_when_get_pr_raises(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)

    def boom(owner: str, repo: str, number: int) -> dict[str, Any]:
        raise RuntimeError("boom from get_pr")

    code_lane.github.get_pr = boom  # type: ignore[method-assign]
    code_lane.poll()
    lanes = {row["lane"]: row for row in store.lanes()}
    assert lanes["code.poll"]["last_outcome"] == "error:1 run(s) failed"
    health = client.get("/health").json()
    assert "boom" not in json.dumps(health)


# -- requeue and callbacks ---------------------------------------------------

def test_rq_callback_requeues_and_edits_card(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run, remote=None)

    query = {"id": "q1", "data": f"rq:{task['id']}:{run['id']}", "message": {"chat": {"id": -300}}}
    code_lane.telegram.dispatch(query, store, code_lane)

    assert store.get_task(task["id"])["status"] == "backlog"
    card = fake.method("editMessageText")[-1][2]
    assert card["text"].startswith("↩ requeued") and card["reply_markup"]["inline_keyboard"] == []
    assert fake.method("answerCallbackQuery")[-1][2]["text"] == "requeued"


def test_rq_callback_stale_card_no_change(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run, remote=None)
    query = {"id": "q1", "data": f"rq:{task['id']}:not-the-run", "message": {"chat": {"id": -300}}}
    code_lane.telegram.dispatch(query, store, code_lane)
    answer = fake.method("answerCallbackQuery")[-1][2]["text"]
    assert "stale card" in answer
    assert store.get_task(task["id"])["status"] == "review"


def test_mg_callback_merges_and_answers(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    sha8 = run["pr_head_sha"][:8]

    query = {"id": "q1", "data": f"mg:{run['id']}:{sha8}", "message": {"chat": {"id": -300}}}
    code_lane.telegram.dispatch(query, store, code_lane)

    assert store.get_task(task["id"])["status"] == "done"
    assert fake.method("answerCallbackQuery")[-1][2]["text"].startswith("merged")


def test_mg_callback_double_tap_answers_already_done(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    sha8 = run["pr_head_sha"][:8]
    client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": sha8})

    query = {"id": "q2", "data": f"mg:{run['id']}:{sha8}", "message": {"chat": {"id": -300}}}
    code_lane.telegram.dispatch(query, store, code_lane)
    assert fake.method("answerCallbackQuery")[-1][2]["text"] == "already done"
    assert len([c for c in github_fake.calls if c[0] == "PUT"]) == 1


def test_mg_callback_network_failure_answers_github_unreachable(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    github_fake.unreachable_merge = True
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    sha8 = run["pr_head_sha"][:8]

    query = {"id": "q1", "data": f"mg:{run['id']}:{sha8}", "message": {"chat": {"id": -300}}}
    code_lane.telegram.dispatch(query, store, code_lane)
    assert fake.method("answerCallbackQuery")[-1][2]["text"] == "GitHub unreachable, try again"


def test_mg_and_rq_callbacks_answer_no_code_lane_when_absent(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    for data in (f"mg:{run['id']}:abcdefab", f"rq:{task['id']}:{run['id']}"):
        query = {"id": "q", "data": data, "message": {"chat": {"id": -300}}}
        code_lane.telegram.dispatch(query, store, None)
        assert fake.method("answerCallbackQuery")[-1][2]["text"] == "no code lane configured"


def test_callback_from_unconfigured_chat_ignored_for_code_prefixes(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    query = {"id": "q1", "data": f"rq:{task['id']}:{run['id']}", "message": {"chat": {"id": -999}}}
    code_lane.telegram.dispatch(query, store, code_lane)
    assert not fake.method("answerCallbackQuery")
    assert store.get_task(task["id"])["status"] != "backlog"


# ===========================================================================
# U3: Merge requires the integration check (CODE_INTEGRATION_CHECK)
# ===========================================================================

def test_merge_refused_no_integration_check(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready_no_check(client, store, github_fake)

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 409
    assert response.json()["detail"] == f"no integration check; run aos integrate {task['id']}"
    assert not [c for c in github_fake.calls if c[0] == "PUT"]
    card = fake.method("editMessageText")[-1][2]
    assert card["text"].startswith(f"no integration check; run aos integrate {task['id']}")
    row = card["reply_markup"]["inline_keyboard"][0]
    assert any(b["text"] == "Merge" for b in row)


def test_merge_refused_integration_check_for_another_commit(
        tmp_path: Path, fake: FakeHTTP, monkeypatch: pytest.MonkeyPatch) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)  # a passing check for run["pr_head_sha"]

    # Simulate the check being on record for a commit that is not this run's
    # current PR head (record_integration itself refuses to store this state
    # for the run's newest run, so it is faked at the read side instead).
    real_latest = store.latest_integration

    def stale_head(run_id: str) -> dict[str, Any] | None:
        result = real_latest(run_id)
        if run_id == run["id"] and result is not None:
            result = dict(result)
            result["head_sha"] = "d" * 40
        return result

    monkeypatch.setattr(store, "latest_integration", stale_head)

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 409
    assert response.json()["detail"] == "integration check is for another commit; run aos integrate again"
    assert not [c for c in github_fake.calls if c[0] == "PUT"]
    card = fake.method("editMessageText")[-1][2]
    assert card["text"].startswith("integration check is for another commit; run aos integrate again")


@pytest.mark.parametrize("status", ["failed", "conflict", "error"])
def test_merge_refused_integration_check_not_passed(tmp_path: Path, fake: FakeHTTP, status: str) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready_no_check(client, store, github_fake)
    store.record_integration(task["id"], run["id"], head_sha=run["pr_head_sha"],
                             base_branch=github_fake.base_branch, base_sha=github_fake.base_sha,
                             status=status, output="bad", worker="test")

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 409
    assert response.json()["detail"] == f"integration check {status}; requeue or fix"
    assert not [c for c in github_fake.calls if c[0] == "PUT"]
    card = fake.method("editMessageText")[-1][2]
    assert card["text"].startswith(f"integration check {status}; requeue or fix")


def test_merge_refused_base_branch_moved_since_check(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    checked_base_sha = github_fake.base_sha
    github_fake.base_sha = "e" * 40  # main moved after the check ran

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail == (f"main moved since the integration check (tested {checked_base_sha[:8]}, "
                      f"now {github_fake.base_sha[:8]}); run aos integrate again")
    assert not [c for c in github_fake.calls if c[0] == "PUT"]
    card = fake.method("editMessageText")[-1][2]
    assert card["text"].startswith(detail)


def test_merge_refused_branch_head_github_refusal_becomes_transition_error(
        tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    github_fake.branch_head_error = (404, "Branch not found")

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 409
    assert response.json()["detail"] == "Branch not found"
    assert not [c for c in github_fake.calls if c[0] == "PUT"]
    card = fake.method("editMessageText")[-1][2]
    assert card["text"].startswith("Branch not found")


# ===========================================================================
# A1: the check is bound to the branch the PR merges into
# ===========================================================================

def test_merge_refused_check_recorded_onto_another_branch(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready_no_check(client, store, github_fake)
    assert run["pr_base"] == "main"
    store.record_integration(task["id"], run["id"], head_sha=run["pr_head_sha"], base_branch="dev",
                             base_sha="f" * 40, status="passed", output="ok", worker="test")

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "integration check ran onto dev, the PR merges into main; run aos integrate again")
    assert not [c for c in github_fake.calls if c[0] == "PUT"]
    card = fake.method("editMessageText")[-1][2]
    assert card["text"].startswith("integration check ran onto dev, the PR merges into main")


def test_merge_base_binding_refusal_takes_precedence_over_not_passed(
        tmp_path: Path, fake: FakeHTTP) -> None:
    """The base-binding refusal is third in the order, before 'not passed':
    a check onto the wrong branch that ALSO failed still reports the base
    mismatch, not the status."""
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready_no_check(client, store, github_fake)
    store.record_integration(task["id"], run["id"], head_sha=run["pr_head_sha"], base_branch="dev",
                             base_sha="f" * 40, status="failed", output="bad", worker="test")

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "integration check ran onto dev, the PR merges into main; run aos integrate again")


def test_pr_base_missing_falls_back_to_get_pr_and_refusal_still_holds(
        tmp_path: Path, fake: FakeHTTP) -> None:
    """A PR opened before this change carries no pr_base: the gate reads it
    once from GitHub, stores it, and the base-binding refusal still holds."""
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _finish_result_directly(store, code_lane, run, result_commit="c" * 40)
    store.annotate_run(run["id"], pr_number=github_fake.pr_number, pr_url=github_fake.pr_html_url,
                       pr_head_sha="c" * 40)
    run = store.get_run(run["id"])
    assert run["pr_base"] is None

    github_fake.pr_head_sha = "c" * 40
    github_fake.pr_base = "dev"
    store.record_integration(task["id"], run["id"], head_sha="c" * 40, base_branch="main",
                             base_sha=github_fake.base_sha, status="passed", output="ok", worker="test")

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": "c" * 8})
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "integration check ran onto main, the PR merges into dev; run aos integrate again")
    assert ("GET", f"/repos/o/r/pulls/{github_fake.pr_number}") in [
        (c[0], c[1]) for c in github_fake.calls]
    assert store.get_run(run["id"])["pr_base"] == "dev"


def test_merge_refused_pr_retargeted_after_check_no_merge_call(tmp_path: Path, fake: FakeHTTP) -> None:
    """C1: the PR's base is read from GitHub on every call, not stored once.
    A PR retargeted on GitHub after a passing check ran onto the old base is
    refused, and the stale stored pr_base is overwritten with the live one."""
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    assert run["pr_base"] == "main"
    github_fake.pr_base = "dev"  # retargeted on GitHub after the check ran

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "integration check ran onto main, the PR merges into dev; run aos integrate again")
    assert not [c for c in github_fake.calls if c[0] == "PUT"]
    assert store.get_run(run["id"])["pr_base"] == "dev"


def test_merge_succeeds_with_pr_base_empty_string_once_check_matches_real_base(
        tmp_path: Path, fake: FakeHTTP) -> None:
    """C1: a pr_base stored as '' (the bug the review found: under the old
    'already set' guard this locked Merge out forever) is treated like an
    absent one, read fresh from GitHub, and overwritten."""
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready_no_check(client, store, github_fake)
    store.annotate_run(run["id"], pr_base="")
    assert store.get_run(run["id"])["pr_base"] == ""
    _record_passing_check(store, github_fake, task, run)  # base_branch = github_fake.base_branch = "main"

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 200, response.text
    assert store.get_run(run["id"])["pr_base"] == github_fake.pr_base


def test_merge_with_passing_check_calls_branch_head_then_merges(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 200, response.text
    assert ("GET", "/repos/o/r/branches/main") in [(c[0], c[1]) for c in github_fake.calls]
    assert response.json()["status"] == "done"


def test_merge_setting_off_skips_the_check_entirely(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake,
                                                        CODE_INTEGRATION_CHECK="off")
    task, run = _setup_merge_ready_no_check(client, store, github_fake)  # no check recorded at all

    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 200, response.text
    assert not [c for c in github_fake.calls if c[0] == "GET" and c[1].startswith("/repos/o/r/branches/")]
    assert response.json()["status"] == "done"


def test_merge_of_a_clean_run_is_unchanged_by_evidence_flags(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_merge_ready(client, store, github_fake)
    with store.connection() as db:
        db.execute("UPDATE task_runs SET evidence=? WHERE id=?",
                   (json.dumps({"run_id": run["id"], "file_count": 3, "flags": []}), run["id"]))
    response = client.post(f"/tasks/{task['id']}/merge", json={"pr_head_sha": run["pr_head_sha"][:8]})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "done"
    card = fake.method("editMessageText")[-1][2]
    assert card["text"].startswith("✅ merged") and "evidence flagged" not in card["text"]


def test_reject_retires_the_card_with_the_rejected_label(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    _post_result(client, run, remote=None)
    response = client.post(f"/tasks/{task['id']}/reject", json={"note": "no"})
    assert response.status_code == 200, response.text
    card = fake.method("editMessageText")[-1][2]
    assert card["text"].startswith("❌ rejected") and card["reply_markup"]["inline_keyboard"] == []


def test_pr_description_preserves_inherited_run_checks(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client, acceptance=[], inherited=["python -m pytest inherited"])
    _post_result(client, run)
    posts = [call for call in github_fake.calls if call[0] == "POST"]
    assert len(posts) == 1
    assert "acceptance:\n  python -m pytest inherited\n" in posts[0][2]["body"]


def test_pr_refuses_missing_run_snapshot(tmp_path: Path, fake: FakeHTTP) -> None:
    github_fake = FakeGitHub()
    client, store, settings, code_lane = build_code_bus(tmp_path, fake, github_fake)
    task, run = _setup_task(client)
    with store.connection() as db:
        db.execute("UPDATE task_runs SET effective_acceptance=NULL WHERE id=?", (run["id"],))
    _post_result(client, run)
    assert not github_fake.calls
    assert store.get_run(run["id"])["pr_number"] is None
    saved = store.get_run(run["id"])
    assert saved["tg_message_id"] is not None
    assert code_lane.attempts(run["id"]) == 0
    code_lane.poll()
    assert next(row for row in store.lanes() if row["lane"] == "code.poll")["last_outcome"] == "ok"
