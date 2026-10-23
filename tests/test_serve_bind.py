"""`python -m agent_os serve` binds where AOS_BIND and AOS_PORT say (endpoint lane E4); the defaults keep the
server container's 0.0.0.0:8010, and a Mac bus may bind 127.0.0.1 and still be reached from a sandbox at
192.168.5.2 (docs/probes/2026-10-14-agent-comms-p1c.md)."""
from __future__ import annotations

import pytest

from agent_os import __main__ as entry
from agent_os.config import Settings


def _serve(monkeypatch, tmp_path, **env: str) -> dict:
    monkeypatch.setenv("BUS_TOKEN", "t")
    monkeypatch.setenv("BUS_DATA_DIR", str(tmp_path))
    for key in ("AOS_BIND", "AOS_PORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    seen: dict = {}

    def fake_run(app, **kwargs):
        seen["app"] = app
        seen.update(kwargs)

    monkeypatch.setattr(entry.uvicorn, "run", fake_run)
    monkeypatch.setattr(entry, "create_app", lambda settings: ("app", settings))
    monkeypatch.setattr("sys.argv", ["python -m agent_os", "serve"])
    entry.main()
    return seen


def test_serve_defaults_keep_the_container_bind(monkeypatch, tmp_path):
    seen = _serve(monkeypatch, tmp_path)
    assert seen["host"] == "0.0.0.0" and seen["port"] == 8010
    settings = seen["app"][1]
    assert settings.AOS_BIND == "0.0.0.0" and settings.AOS_PORT == 8010


def test_serve_binds_where_the_settings_say(monkeypatch, tmp_path):
    seen = _serve(monkeypatch, tmp_path, AOS_BIND="127.0.0.1", AOS_PORT="8012")
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8012


@pytest.mark.parametrize("port", ["0", "65536", "-1", "eight"])
def test_serve_refuses_a_port_outside_the_range(monkeypatch, tmp_path, port):
    monkeypatch.setenv("BUS_TOKEN", "t")
    monkeypatch.setenv("AOS_PORT", port)
    with pytest.raises(ValueError):
        Settings()
