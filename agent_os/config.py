from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    """Host default for BUS_DATA_DIR; the bundled image exports /data explicitly."""
    try:
        return Path.home() / ".aos" / "data"
    except RuntimeError as exc:  # HOME unset and no passwd entry for this user
        raise RuntimeError("BUS_DATA_DIR must be set: no home directory to default under") from exc


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="forbid", env_prefix="", case_sensitive=True)

    BUS_DATA_DIR: Path = Field(default_factory=default_data_dir)
    BUS_TOKEN: str = Field(min_length=1)
    # Where `python -m agent_os serve` listens. The defaults keep the server container's bind; a Mac bus
    # may bind 127.0.0.1 and is still reached from a sandbox at 192.168.5.2 (P1c, 2026-10-14).
    AOS_BIND: str = "0.0.0.0"
    AOS_PORT: int = Field(default=8010, gt=0, lt=65536)
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_NEEDS_YOU: str = ""
    TELEGRAM_API_BASE: str = "https://api.telegram.org"
    HEALTH_INTERVAL_SECONDS: int = Field(default=60, gt=0)
    # Task backlog: claim lease length and the sweep that blocks expired ones.
    TASK_LEASE_SECONDS: int = Field(default=600, gt=0)
    TASK_SWEEP_INTERVAL_SECONDS: int = Field(default=60, gt=0)
    # A result from the run's own claim holder is still accepted this long after its lease
    # expired, and recorded as late; later than that it is refused and only logged.
    TASK_LATE_RESULT_GRACE_SECONDS: int = Field(default=3600, gt=0)
    # How many live runs (claimed or running) a repo may have at once; each task keeps its own worktree.
    TASK_MAX_LIVE_RUNS_PER_REPO: int = Field(default=3, gt=0)
    # Request ledger: how long a delivered-but-unacked request waits before a
    # retry, and how long a never-attempted one waits before a needs-you notice.
    AOS_REQUEST_ACK_SECONDS: int = Field(default=900, gt=0)
    FAIL_THRESHOLD: int = Field(default=3, gt=0)

    # ── Code lane (PR output + Telegram code channel) ──────────────────
    GITHUB_TOKEN: str = ""
    GITHUB_API_BASE: str = "https://api.github.com"
    GITHUB_MERGE_METHOD: Literal["merge", "squash", "rebase"] = "merge"
    TELEGRAM_CHAT_CODE: str = ""
    CODE_POLL_INTERVAL_SECONDS: int = Field(default=60, gt=0)
    # Merge refuses without a passing `aos integrate` check for the PR head on
    # the current base branch; "off" restores pre-step-3 behavior.
    CODE_INTEGRATION_CHECK: Literal["require", "off"] = "require"

    # ── Super Productivity source (tasks read from the app's sync files) ──
    # A Dropbox app of the operator's own with only files.content.read; the refresh
    # token comes from `python -m agent_os.sp_sync auth`. The lane is on when the three
    # credentials, the folder and a project or tag are set.
    AOS_SP_DROPBOX_APP_KEY: str = ""
    AOS_SP_DROPBOX_APP_SECRET: str = ""
    AOS_SP_DROPBOX_REFRESH_TOKEN: str = ""
    # Dropbox folder holding sync-data.json ("/" for an app-folder app's root).
    AOS_SP_DROPBOX_PATH: str = ""
    # Test hooks, like TELEGRAM_API_BASE: the two Dropbox hosts.
    AOS_SP_DROPBOX_API_BASE: str = "https://api.dropboxapi.com"
    AOS_SP_DROPBOX_CONTENT_BASE: str = "https://content.dropboxapi.com"
    # Tasks in this project, or carrying this tag (titles as shown in the app); either or both.
    AOS_SP_PROJECT: str = ""
    AOS_SP_TAG: str = ""
    # Empty: a task is released to ready at the next poll. Set to a tag name: only a task
    # carrying that tag is released, and its removal takes the task back to backlog.
    AOS_SP_READY_TAG: str = ""
    # A task carrying this tag (compared case-insensitively) is never released; if it was
    # ready before a claim it goes back to backlog.
    AOS_SP_HOLD_TAG: str = "hold"
    AOS_SP_POLL_SECONDS: int = Field(default=3600, gt=0)
    # The app's sync encryption password; required only when the sync files are encrypted.
    AOS_SP_SYNC_PASSWORD: str = ""

    @property
    def sp_enabled(self) -> bool:
        return bool(self.AOS_SP_DROPBOX_APP_KEY and self.AOS_SP_DROPBOX_APP_SECRET
                    and self.AOS_SP_DROPBOX_REFRESH_TOKEN and self.AOS_SP_DROPBOX_PATH
                    and (self.AOS_SP_PROJECT or self.AOS_SP_TAG))

    @property
    def code_chat(self) -> str:
        return self.TELEGRAM_CHAT_CODE or self.TELEGRAM_CHAT_NEEDS_YOU

    @model_validator(mode="after")
    def configured_chats(self) -> Settings:
        if self.TELEGRAM_BOT_TOKEN and not self.TELEGRAM_CHAT_NEEDS_YOU:
            raise ValueError("TELEGRAM_CHAT_NEEDS_YOU is required when Telegram is enabled")
        return self
