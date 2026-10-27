from __future__ import annotations

from typing import Any

import httpx
import pytest

from agent_os.config import Settings
from agent_os.github import GitHub, GitHubRefused, parse_remote


# -- parse_remote -------------------------------------------------------------

@pytest.mark.parametrize("remote,owner,repo", [
    ("git@github.com:acme/widgets.git", "acme", "widgets"),
    ("git@github.com:acme/widgets", "acme", "widgets"),
    ("ssh://git@github.com/acme/widgets.git", "acme", "widgets"),
    ("ssh://git@github.com/acme/widgets", "acme", "widgets"),
    ("https://github.com/acme/widgets.git", "acme", "widgets"),
    ("https://github.com/acme/widgets", "acme", "widgets"),
    ("https://github.com/acme/widgets/", "acme", "widgets"),
])
def test_parse_remote_matches_the_three_shapes(remote: str, owner: str, repo: str) -> None:
    assert parse_remote(remote) == (owner, repo)


@pytest.mark.parametrize("remote", [
    "https://gitlab.com/acme/widgets.git",
    "git@gitlab.com:acme/widgets.git",
    "/Users/operator/code/agent-os",
    "",
    None,
])
def test_parse_remote_non_github_and_bare_path_is_none(remote: str | None) -> None:
    assert parse_remote(remote) is None


# -- GitHub client -------------------------------------------------------------

def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, BUS_TOKEN="t", GITHUB_API_BASE="https://github.test", **overrides)


class FakeHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.default_branch = "main"
        # each GET pulls?head= call pops the first element while more than one
        # remains, then repeats the last forever (lets a test express "empty,
        # then reused" without a second fixture).
        self.open_prs_sequence: list[list[dict[str, Any]]] = [[]]
        self.post_status = 201
        self.post_body: dict[str, Any] = {
            "number": 7, "html_url": "https://github.test/acme/widgets/pull/7",
            "head": {"sha": "d" * 40},
        }
        self.branch_sha = "f" * 40
        self.branch_status: int | None = None
        self.branch_message = ""

    def _next_open_prs(self) -> list[dict[str, Any]]:
        if len(self.open_prs_sequence) > 1:
            return self.open_prs_sequence.pop(0)
        return self.open_prs_sequence[0]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "GET" and path == "/repos/acme/widgets":
            return httpx.Response(200, json={"default_branch": self.default_branch})
        if request.method == "GET" and path == "/repos/acme/widgets/pulls":
            return httpx.Response(200, json=self._next_open_prs())
        if request.method == "POST" and path == "/repos/acme/widgets/pulls":
            return httpx.Response(self.post_status, json=self.post_body)
        if request.method == "PUT" and path.endswith("/merge"):
            return httpx.Response(200, json={"merged": True, "sha": "abc123"})
        if request.method == "GET" and path.startswith("/repos/acme/widgets/branches/"):
            if self.branch_status is not None:
                return httpx.Response(self.branch_status, json={"message": self.branch_message})
            return httpx.Response(200, json={"commit": {"sha": self.branch_sha}})
        raise AssertionError(f"unexpected request {request.method} {request.url}")


def _github(handler: FakeHandler, **settings_overrides: Any) -> GitHub:
    settings = _settings(GITHUB_TOKEN="ghp-test", **settings_overrides)
    return GitHub(settings, httpx.Client(transport=httpx.MockTransport(handler.handle)))


def test_open_pr_reuses_existing_before_any_post() -> None:
    handler = FakeHandler()
    handler.open_prs_sequence = [[{"number": 3, "html_url": "u3", "head": {"sha": "c" * 40}}]]
    github = _github(handler)
    pr = github.open_pr("acme", "widgets", head="ao/AO-9", base="main", title="t", body="b")
    assert pr["number"] == 3
    assert ("POST", "/repos/acme/widgets/pulls") not in handler.calls


def test_open_pr_422_already_exists_reuses() -> None:
    handler = FakeHandler()
    handler.open_prs_sequence = [[], [{"number": 9, "html_url": "u9", "head": {"sha": "e" * 40}}]]
    handler.post_status = 422
    handler.post_body = {
        "message": "Validation Failed",
        "errors": [{"resource": "PullRequest", "field": "head",
                    "message": "A pull request already exists for acme:ao/AO-9."}],
    }
    github = _github(handler)
    pr = github.open_pr("acme", "widgets", head="ao/AO-9", base="main", title="t", body="b")
    assert pr["number"] == 9
    assert handler.calls.count(("POST", "/repos/acme/widgets/pulls")) == 1
    assert handler.calls.count(("GET", "/repos/acme/widgets/pulls")) == 2


def test_open_pr_422_not_already_exists_raises_refused() -> None:
    handler = FakeHandler()
    handler.post_status = 422
    handler.post_body = {"message": "Validation Failed", "errors": [{"message": "field is required"}]}
    github = _github(handler)
    with pytest.raises(GitHubRefused) as exc_info:
        github.open_pr("acme", "widgets", head="ao/AO-9", base="main", title="t", body="b")
    assert exc_info.value.status == 422


@pytest.mark.parametrize("status", [405, 409, 422])
def test_merge_pr_405_409_422_raises_refused_with_message(status: int) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": "refused by github"})

    settings = _settings(GITHUB_TOKEN="ghp-test")
    github = GitHub(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(GitHubRefused) as exc_info:
        github.merge_pr("acme", "widgets", 7, sha="abc1234", merge_method="merge")
    assert exc_info.value.status == status
    assert exc_info.value.message == "refused by github"


def test_disabled_client_refuses_before_any_request() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("no request should be made when the token is empty")

    settings = _settings(GITHUB_TOKEN="")
    github = GitHub(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    assert not github.enabled
    with pytest.raises(GitHubRefused) as exc_info:
        github.default_branch("acme", "widgets")
    assert exc_info.value.status == 401
    assert exc_info.value.message == "GITHUB_TOKEN is not set"
    assert calls == []


def test_default_branch_is_cached_per_instance() -> None:
    handler = FakeHandler()
    handler.default_branch = "trunk"
    github = _github(handler)
    assert github.default_branch("acme", "widgets") == "trunk"
    assert github.default_branch("acme", "widgets") == "trunk"
    assert handler.calls.count(("GET", "/repos/acme/widgets")) == 1


def test_retry_after_header_becomes_retry_after_seconds() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "rate limited"}, headers={"Retry-After": "120"})

    settings = _settings(GITHUB_TOKEN="ghp-test")
    github = GitHub(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(GitHubRefused) as exc_info:
        github.default_branch("acme", "widgets")
    assert exc_info.value.retry_after == 120.0


# -- branch_head (parallel step 3, unit U3) -----------------------------------

def test_branch_head_returns_commit_sha() -> None:
    handler = FakeHandler()
    handler.branch_sha = "e" * 40
    github = _github(handler)
    assert github.branch_head("acme", "widgets", "main") == "e" * 40
    assert ("GET", "/repos/acme/widgets/branches/main") in handler.calls


@pytest.mark.parametrize("status", [404, 409, 422])
def test_branch_head_refusal_raises_refused_with_message(status: int) -> None:
    handler = FakeHandler()
    handler.branch_status = status
    handler.branch_message = "Branch not found"
    github = _github(handler)
    with pytest.raises(GitHubRefused) as exc_info:
        github.branch_head("acme", "widgets", "main")
    assert exc_info.value.status == status
    assert exc_info.value.message == "Branch not found"


def test_branch_head_disabled_client_refuses_before_any_request() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("no request should be made when the token is empty")

    settings = _settings(GITHUB_TOKEN="")
    github = GitHub(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(GitHubRefused) as exc_info:
        github.branch_head("acme", "widgets", "main")
    assert exc_info.value.status == 401
    assert calls == []


def test_ratelimit_reset_becomes_retry_after_when_remaining_is_zero() -> None:
    import time

    reset_at = time.time() + 30
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "rate limited"},
                              headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset_at)})

    settings = _settings(GITHUB_TOKEN="ghp-test")
    github = GitHub(settings, httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(GitHubRefused) as exc_info:
        github.default_branch("acme", "widgets")
    assert exc_info.value.retry_after is not None
    assert 0 < exc_info.value.retry_after <= 30
