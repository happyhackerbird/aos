from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_os.config import Settings
from agent_os.store import Store, utcnow
from agent_os.telegram import Telegram


class Health:
    def __init__(self, store: Store, telegram: Telegram, settings: Settings,
                 started: datetime | None = None) -> None:
        self.store = store
        self.telegram = telegram
        self.settings = settings
        self.started = started or datetime.now(timezone.utc)

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        return self._snapshot(self.store.lanes(), now)

    def _snapshot(self, rows: list[dict[str, Any]], now: datetime | None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        lanes = []
        for lane in rows:
            last = datetime.fromisoformat(lane["last_run_at"]) if lane["last_run_at"] else self.started
            cadence = lane["cadence_seconds"]
            stale = bool(cadence and (now - last).total_seconds() > 2 * cadence)
            erroring = bool(cadence and lane["error_since"] and
                            (now - datetime.fromisoformat(lane["error_since"])).total_seconds() > 2 * cadence)
            lanes.append({key: lane[key] for key in (
                "lane", "cadence_seconds", "last_run_at", "last_outcome", "error_since")
            } | {"stale": stale, "erroring": erroring})
        return {"lanes": lanes, "ok": not any(lane["stale"] or lane["erroring"] for lane in lanes)}

    def check(self, now: datetime | None = None) -> dict[str, Any]:
        rows = self.store.lanes()
        result = self._snapshot(rows, now)
        stamp = now.isoformat(timespec="seconds").replace("+00:00", "Z") if now else utcnow()
        # The health worker is serial. Persist only confirmed sends so an outage
        # (or a crash before delivery) leaves the warning eligible next time.
        for lane, row in zip(result["lanes"], rows):
            if lane["stale"] and row["stale_notified_at"] is None:
                sent = self.telegram.send(self.settings.TELEGRAM_CHAT_NEEDS_YOU,
                                          f"⚠️ lane {lane['lane']} silent since {lane['last_run_at']}")
                if sent is not None:
                    self.store.mark_stale(lane["lane"], lane["last_run_at"], stamp)
            if lane["erroring"] and row["error_notified_at"] is None:
                sent = self.telegram.send(self.settings.TELEGRAM_CHAT_NEEDS_YOU,
                                          f"⚠️ lane {lane['lane']} erroring since {lane['error_since']}: "
                                          f"{lane['last_outcome'][:200]}")
                if sent is not None:
                    self.store.mark_error(lane["lane"], lane["error_since"], stamp)
        return result
