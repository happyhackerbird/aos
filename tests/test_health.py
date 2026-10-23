from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_os.config import Settings
from agent_os.health import Health
from agent_os.store import Store
from agent_os.telegram import Telegram
from conftest import FakeHTTP


def test_stale_once_reset_after_run(store: Store, telegram: Telegram, settings: Settings, fake: FakeHTTP) -> None:
    start = datetime(2026, 9, 12, tzinfo=timezone.utc)
    store.register_lane("code.poll", 300)
    health = Health(store, telegram, settings, started=start)
    assert health.check(start + timedelta(seconds=600))["ok"]
    assert not health.check(start + timedelta(seconds=601))["ok"]
    health.check(start + timedelta(seconds=602))
    assert len(fake.messages("-200")) == 1
    assert store.lanes()[0]["stale_notified_at"]
    store.record_run("code.poll", "2026-09-12T00:11:00Z", "2026-09-12T00:11:01Z", "ok")
    assert store.lanes()[0]["stale_notified_at"] is None
    assert health.check(start + timedelta(seconds=700))["ok"]
    assert not health.check(start + timedelta(seconds=1262))["ok"]
    assert len(fake.messages("-200")) == 2


def test_no_cadence_and_error_run_are_not_silence(store: Store, telegram: Telegram, settings: Settings) -> None:
    start = datetime(2026, 9, 12, tzinfo=timezone.utc)
    store.record_run("one-shot", "2026-09-12T00:00:00Z", "2026-09-12T00:00:00Z", "ok")
    store.register_lane("poll", 300)
    store.record_run("poll", "2026-09-12T00:00:00Z", "2026-09-12T00:00:00Z", "error:offline")
    health = Health(store, telegram, settings, started=start)
    assert health.snapshot(start + timedelta(seconds=1))["ok"]
    body = health.snapshot(start + timedelta(seconds=601))
    assert not body["ok"] and not body["lanes"][0]["stale"]


def test_stale_claim_ignores_concurrent_run(store: Store) -> None:
    store.register_lane("poll", 300)
    store.record_run("poll", "2026-09-12T00:00:00Z", "2026-09-12T00:00:01Z", "ok")
    assert not store.mark_stale("poll", None, "2026-09-12T00:11:00Z")


def test_erroring_once_and_reset_on_ok(store: Store, telegram: Telegram, settings: Settings, fake: FakeHTTP) -> None:
    start = datetime(2026, 9, 12, tzinfo=timezone.utc)
    health = Health(store, telegram, settings, started=start)
    store.register_lane("poll", 300)
    store.record_run("poll", "2026-09-12T00:00:00Z", "2026-09-12T00:00:00Z", "error:first")
    store.record_run("poll", "2026-09-12T00:09:00Z", "2026-09-12T00:09:00Z", "error:" + "x" * 300)
    assert store.lanes()[0]["error_since"] == "2026-09-12T00:00:00Z"
    assert health.check(start + timedelta(seconds=600))["ok"]
    body = health.check(start + timedelta(seconds=601))
    assert not body["ok"] and body["lanes"][0]["erroring"] and not body["lanes"][0]["stale"]
    assert body["lanes"][0]["error_since"] == "2026-09-12T00:00:00Z"
    marker = store.lanes()[0]["error_notified_at"]
    assert marker
    store.record_run("poll", "2026-09-12T00:11:00Z", "2026-09-12T00:11:00Z", "error:still offline")
    health.check(start + timedelta(seconds=700))
    assert store.lanes()[0]["error_notified_at"] == marker
    assert len(fake.messages("-200")) == 1
    store.record_run("poll", "2026-09-12T00:12:00Z", "2026-09-12T00:12:00Z", "ok")
    assert store.lanes()[0]["error_since"] is None
    assert store.lanes()[0]["error_notified_at"] is None
    assert health.check(start + timedelta(seconds=721))["ok"]
    store.record_run("poll", "2026-09-12T00:13:00Z", "2026-09-12T00:13:00Z", "error:new")
    store.record_run("poll", "2026-09-12T00:22:00Z", "2026-09-12T00:22:00Z", "error:again")
    health.check(start + timedelta(seconds=1381))
    assert len(fake.messages("-200")) == 2
    assert store.lanes()[0]["error_since"] == "2026-09-12T00:13:00Z"


def test_error_notification_claim_ignores_recovery(store: Store) -> None:
    store.record_run("poll", "2026-09-12T00:00:00Z", "2026-09-12T00:00:00Z", "error:offline")
    store.record_run("poll", "2026-09-12T00:01:00Z", "2026-09-12T00:01:00Z", "ok")
    assert not store.mark_error("poll", "2026-09-12T00:00:00Z", "2026-09-12T00:11:00Z")


def test_error_without_cadence_does_not_alert(store: Store, telegram: Telegram, settings: Settings, fake: FakeHTTP) -> None:
    start = datetime(2026, 9, 12, tzinfo=timezone.utc)
    store.record_run("one-shot", "2026-09-12T00:00:00Z", "2026-09-12T00:00:00Z", "error:offline")
    health = Health(store, telegram, settings, started=start)
    assert health.check(start + timedelta(days=1))["ok"]
    assert not fake.messages("-200")


@pytest.mark.parametrize("kind", ["stale", "error"])
def test_failed_warning_retries_after_restart_then_stops(
        store: Store, telegram: Telegram, settings: Settings, fake: FakeHTTP, kind: str) -> None:
    start = datetime(2026, 9, 12, tzinfo=timezone.utc)
    store.register_lane("poll", 300)
    if kind == "error":
        store.record_run("poll", start.isoformat(), start.isoformat(), "error:offline")
        recent = (start + timedelta(seconds=590)).isoformat()
        store.record_run("poll", recent, recent, "error:offline")
    now = start + timedelta(seconds=601)
    marker = f"{kind}_notified_at"
    fake.telegram_error = True
    Health(store, telegram, settings, started=start).check(now)
    assert store.lanes()[0][marker] is None
    assert len(fake.messages("-200")) == 1

    fake.telegram_error = False
    restarted = Health(Store(settings.BUS_DATA_DIR), telegram, settings, started=start)
    restarted.check(now + timedelta(seconds=1))
    assert store.lanes()[0][marker] is not None
    assert len(fake.messages("-200")) == 2
    restarted.check(now + timedelta(seconds=2))
    assert len(fake.messages("-200")) == 2


@pytest.mark.parametrize("kind", ["stale", "error"])
def test_delivery_does_not_acknowledge_a_new_incident(
        store: Store, telegram: Telegram, settings: Settings, fake: FakeHTTP,
        monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    start = datetime(2026, 9, 12, tzinfo=timezone.utc)
    store.register_lane("poll", 300)
    if kind == "error":
        store.record_run("poll", start.isoformat(), start.isoformat(), "error:offline")
        recent = (start + timedelta(seconds=590)).isoformat()
        store.record_run("poll", recent, recent, "error:offline")
    send = telegram.send
    recovery = (start + timedelta(seconds=601)).isoformat()

    def recover_during_send(*args, **kwargs):
        message_id = send(*args, **kwargs)
        store.record_run("poll", recovery, recovery, "ok")
        return message_id

    monkeypatch.setattr(telegram, "send", recover_during_send)
    health = Health(store, telegram, settings, started=start)
    health.check(start + timedelta(seconds=601))
    assert store.lanes()[0][f"{kind}_notified_at"] is None
    monkeypatch.setattr(telegram, "send", send)
    if kind == "error":
        store.record_run("poll", recovery, recovery, "error:again")
        recent = (start + timedelta(seconds=1200)).isoformat()
        store.record_run("poll", recent, recent, "error:again")
    health.check(start + timedelta(seconds=1202))
    assert store.lanes()[0][f"{kind}_notified_at"] is not None
    assert len(fake.messages("-200")) == 2
