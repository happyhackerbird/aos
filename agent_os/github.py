"""A thin httpx client over the GitHub REST API.

Injected as ``client`` for tests exactly as :class:`agent_os.telegram.Telegram`
takes one. GITHUB_TOKEN is read from settings and never logged; every log
line here names the method and the path only.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from agent_os.config import Settings

logger = logging.getLogger("agent_os.github")

_REMOTE_PATTERNS = (
    re.compile(r"^git@github\.com:(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?$"),
    re.compile(r"^ssh://git@github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?$"),
    re.compile(r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"),
)


def parse_remote(remote: str | None) -> tuple[str, str] | None:
    """Match a GitHub remote in one of the three documented shapes, else None."""
    if not remote:
        return None
    for pattern in _REMOTE_PATTERNS:
        match = pattern.match(remote)
        if match:
            return match.group("owner"), match.group("repo")
    return None


class GitHubRefused(Exception):
    """A 4xx GitHub answered with its own message (or the status text)."""

    def __init__(self, status: int, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


class GitHub:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=30)
        self.enabled = bool(settings.GITHUB_TOKEN)
        self._default_branch_cache: dict[tuple[str, str], str] = {}

    # -- transport -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        header = response.headers.get("Retry-After")
        if header is not None:
            try:
                return float(header)
            except ValueError:
                return None
        if response.headers.get("x-ratelimit-remaining") == "0":
            reset = response.headers.get("x-ratelimit-reset")
            if reset is not None:
                try:
                    return float(reset) - time.time()
                except ValueError:
                    return None
        return None

    @staticmethod
    def _message(response: httpx.Response) -> str:
        try:
            data = response.json()
        except ValueError:
            return response.reason_phrase or str(response.status_code)
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            return data["message"]
        return response.reason_phrase or str(response.status_code)

    def _refuse(self, response: httpx.Response) -> GitHubRefused:
        return GitHubRefused(response.status_code, self._message(response),
                             retry_after=self._retry_after(response))

    def _send(self, method: str, path: str, *, params: dict[str, Any] | None = None,
              json_body: dict[str, Any] | None = None) -> httpx.Response:
        if not self.enabled:
            raise GitHubRefused(401, "GITHUB_TOKEN is not set")
        url = f"{self.settings.GITHUB_API_BASE.rstrip('/')}{path}"
        logger.debug("GitHub %s %s", method, path)
        return self.client.request(method, url, params=params, json=json_body, headers=self._headers())

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._send(method, path, **kwargs)
        if 400 <= response.status_code < 500:
            raise self._refuse(response)
        response.raise_for_status()
        return response

    # -- API -------------------------------------------------------------------

    def default_branch(self, owner: str, repo: str) -> str:
        key = (owner, repo)
        if key not in self._default_branch_cache:
            response = self._request("GET", f"/repos/{owner}/{repo}")
            self._default_branch_cache[key] = response.json()["default_branch"]
        return self._default_branch_cache[key]

    def list_open_prs(self, owner: str, repo: str, *, head: str) -> list[dict[str, Any]]:
        response = self._request("GET", f"/repos/{owner}/{repo}/pulls",
                                 params={"head": f"{owner}:{head}", "state": "open"})
        return response.json()

    @staticmethod
    def _already_exists(response: httpx.Response) -> bool:
        try:
            data = response.json()
        except ValueError:
            return False
        if not isinstance(data, dict):
            return False
        texts = [data.get("message") or ""]
        errors = data.get("errors")
        if isinstance(errors, list):
            texts.extend(entry.get("message", "") for entry in errors if isinstance(entry, dict))
        return any("already exists" in text.lower() for text in texts)

    def open_pr(self, owner: str, repo: str, *, head: str, base: str, title: str,
               body: str) -> dict[str, Any]:
        existing = self.list_open_prs(owner, repo, head=head)
        if existing:
            return existing[0]
        response = self._send("POST", f"/repos/{owner}/{repo}/pulls",
                              json_body={"head": head, "base": base, "title": title, "body": body})
        if response.status_code == 422 and self._already_exists(response):
            existing = self.list_open_prs(owner, repo, head=head)
            if existing:
                return existing[0]
            raise self._refuse(response)
        if 400 <= response.status_code < 500:
            raise self._refuse(response)
        response.raise_for_status()
        return response.json()

    def get_pr(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}").json()

    def branch_head(self, owner: str, repo: str, branch: str) -> str:
        response = self._request("GET", f"/repos/{owner}/{repo}/branches/{branch}")
        return response.json()["commit"]["sha"]

    def merge_pr(self, owner: str, repo: str, number: int, *, sha: str,
                merge_method: str) -> dict[str, Any]:
        return self._request("PUT", f"/repos/{owner}/{repo}/pulls/{number}/merge",
                             json_body={"sha": sha, "merge_method": merge_method}).json()
