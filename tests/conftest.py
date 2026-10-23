from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_os import evidence
from agent_os.config import Settings
from agent_os.store import Store
from agent_os.telegram import Telegram



class FakeHTTP:
    """The Telegram Bot API at telegram.test: every call is logged and answered ok."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.telegram_error = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, str(request.url), body))
        if request.url.host == "telegram.test":
            if self.telegram_error:
                return httpx.Response(500)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.calls)}})
        raise AssertionError(f"Unexpected request {request.method} {request.url}")

    def method(self, suffix: str) -> list[tuple[str, str, Any]]:
        return [call for call in self.calls if call[1].endswith(suffix)]

    def messages(self, chat: str) -> list[tuple[str, str, Any]]:
        return [call for call in self.method("sendMessage") if call[2]["chat_id"] == chat]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, BUS_TOKEN="test-bus-token", BUS_DATA_DIR=tmp_path,
                    TELEGRAM_BOT_TOKEN="fake-token",
                    TELEGRAM_CHAT_NEEDS_YOU="-200", TELEGRAM_API_BASE="https://telegram.test")


@pytest.fixture
def store(settings: Settings) -> Store:
    return Store(settings.BUS_DATA_DIR)


@pytest.fixture
def fake() -> FakeHTTP:
    return FakeHTTP()


@pytest.fixture
def telegram(settings: Settings, fake: FakeHTTP) -> Telegram:
    return Telegram(settings, httpx.Client(transport=httpx.MockTransport(fake.handle)))



# -- Evidence-root isolation ------------------------------------------------
#
# The cli's post-decision removal and the runners' start-up prune resolve
# AOS_EVIDENCE_ROOT, else evidence.DEFAULT_ROOT (~/.aos/evidence). Every test
# gets both pointed at its own temporary directory, so no test reads or removes
# the operator's evidence whatever environ mapping it passes, and none sees
# another test's evidence.

@pytest.fixture(autouse=True)
def isolated_evidence_root(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path_factory.mktemp("evidence-isolation")
    monkeypatch.setenv("AOS_EVIDENCE_ROOT", str(base / "env"))
    monkeypatch.setattr(evidence, "DEFAULT_ROOT", str(base / "default"))
    return base


# -- Docker isolation and the stub `docker` ---------------------------------
#
# No test may reach the host's Docker daemon. Every test starts with `docker`
# unresolvable and the Docker environment cleared; a test that needs one
# requests `docker_stub`, whose script is the only `docker` it can find.

@pytest.fixture(autouse=True)
def _hide_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    original_which = shutil.which
    monkeypatch.setattr(shutil, "which",
                        lambda cmd, *args, **kw: None if cmd == "docker" else original_which(cmd, *args, **kw))
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)


# Every call is logged to calls.jsonl before it is answered. __STATE_DIR__ is
# replaced with the fixture's state directory when the script is written.
DOCKER_STUB_SOURCE = r'''#!/usr/bin/env python3
import fcntl
import hashlib
import json
import os
import sys

STATE_DIR = __STATE_DIR__


def knob(name, default=None):
    try:
        with open(os.path.join(STATE_DIR, name)) as stream:
            return stream.read().strip()
    except OSError:
        return default


def fail(message, code=1):
    sys.stderr.write(message + "\n")
    sys.exit(code)


raw = sys.argv[1:]
lock = open(os.path.join(STATE_DIR, "lock"), "a+")
fcntl.flock(lock, fcntl.LOCK_EX)
with open(os.path.join(STATE_DIR, "calls.jsonl"), "a") as stream:
    stream.write(json.dumps({"argv": raw, "password": os.environ.get("POSTGRES_PASSWORD")}) + "\n")

args = list(raw)
if args[:1] == ["--context"] and len(args) >= 2:
    args = args[2:]

state_path = os.path.join(STATE_DIR, "state.json")
try:
    with open(state_path) as stream:
        state = json.load(stream)
except OSError:
    state = {"images": [], "containers": {}, "exec_calls": 0}


def save():
    temporary = state_path + ".tmp"
    with open(temporary, "w") as stream:
        json.dump(state, stream)
    os.replace(temporary, state_path)


def matching(prefix):
    return [identity for identity in sorted(state["containers"]) if prefix and identity.startswith(prefix)]


mode = knob("fail")

if args[:2] == ["context", "show"]:
    if mode == "context":
        fail("context failed")
    print("stub-context")
elif args[:1] == ["version"]:
    if mode == "daemon":
        fail("Cannot connect to the Docker daemon at unix:///stub.sock. Is the docker daemon running?")
    print("29.0.0-stub")
elif args[:2] == ["image", "inspect"]:
    image = args[2] if len(args) > 2 else ""
    if image not in state["images"]:
        fail("Error: No such image: " + image)
    print("[{}]")
elif args[:1] == ["pull"]:
    if mode == "pull":
        fail("Error response from daemon: pull access denied for stub")
    image = args[-1]
    if image not in state["images"]:
        state["images"].append(image)
    save()
    print("pulled")
elif args[:1] == ["run"]:
    rest = args[1:]
    name = None
    labels = {}
    position = 0
    while position < len(rest):
        token = rest[position]
        if token == "--name" and position + 1 < len(rest):
            name = rest[position + 1]
            position += 2
        elif token.startswith("--name="):
            name = token[len("--name="):]
            position += 1
        elif token == "--label" and position + 1 < len(rest):
            key, _, value = rest[position + 1].partition("=")
            labels[key] = value
            position += 2
        elif token.startswith("--label="):
            key, _, value = token[len("--label="):].partition("=")
            labels[key] = value
            position += 1
        else:
            position += 1
    identity = hashlib.sha256((name or " ".join(args)).encode()).hexdigest()
    state["containers"][identity] = {"name": name, "labels": labels}
    save()
    if mode == "run":
        fail("docker: Error response from daemon: driver failed programming external connectivity", 125)
    print(identity)
elif args[:1] == ["port"]:
    if mode == "port":
        fail("Error: No public port '5432/tcp' published")
    print(knob("port_output", "127.0.0.1:55432"))
elif args[:1] == ["exec"]:
    state["exec_calls"] += 1
    save()
    if mode == "readiness" or state["exec_calls"] < int(knob("ready_after", "1")):
        print("127.0.0.1:5432 - no response")
        sys.exit(2)
    print("127.0.0.1:5432 - accepting connections")
elif args[:1] == ["ps"]:
    wanted = []
    for position, token in enumerate(args):
        if token == "--filter" and position + 1 < len(args):
            wanted.append(args[position + 1])
    for identity in sorted(state["containers"]):
        labels = state["containers"][identity]["labels"]
        for condition in wanted:
            key, _, value = condition[len("label="):].partition("=") if condition.startswith("label=") else (None, "", "")
            if key is None or labels.get(key) != value:
                break
        else:
            print(identity[:12])
elif args[:2] == ["network", "ls"]:
    pass
elif args[:2] == ["container", "inspect"]:
    found = matching(args[2] if len(args) > 2 else "")
    if not found:
        fail("Error: No such container")
    record = state["containers"][found[0]]
    print(json.dumps([{"Id": found[0], "Name": "/" + (record["name"] or ""),
                       "Config": {"Labels": record["labels"]}}]))
elif args[:2] == ["rm", "-f"]:
    for prefix in args[2:]:
        for identity in matching(prefix):
            del state["containers"][identity]
    save()
else:
    fail("stub: unsupported")
'''


class DockerStub:
    """Reads and seeds the state the stub `docker` script keeps in `directory`."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _state(self) -> dict[str, Any]:
        try:
            return json.loads((self.directory / "state.json").read_text())
        except OSError:
            return {"images": [], "containers": {}, "exec_calls": 0}

    def _records(self) -> list[dict[str, Any]]:
        try:
            lines = (self.directory / "calls.jsonl").read_text().splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines if line]

    @staticmethod
    def _strip_context(argv: list[str]) -> list[str]:
        return argv[2:] if argv[:1] == ["--context"] and len(argv) >= 2 else argv

    def calls(self) -> list[list[str]]:
        return [self._strip_context(record["argv"]) for record in self._records()]

    def password(self) -> str | None:
        runs = [record for record in self._records() if self._strip_context(record["argv"])[:1] == ["run"]]
        return runs[-1]["password"] if runs else None

    def containers(self) -> dict[str, dict[str, Any]]:
        return self._state()["containers"]

    def add_container(self, name: str, labels: dict[str, str]) -> str:
        state = self._state()
        identity = hashlib.sha256(name.encode()).hexdigest()
        state["containers"][identity] = {"name": name, "labels": labels}
        (self.directory / "state.json").write_text(json.dumps(state))
        return identity

    def _knob(self, name: str, value: str | None) -> None:
        path = self.directory / name
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value)

    def fail(self, stage: str | None) -> None:
        self._knob("fail", stage)

    def ready_after(self, count: int) -> None:
        self._knob("ready_after", str(count))

    def port_output(self, text: str) -> None:
        self._knob("port_output", text)

    def removed(self, full_id: str) -> bool:
        asked = any(argv[:2] == ["rm", "-f"] and len(argv) > 2 and argv[2] and full_id.startswith(argv[2])
                    for argv in self.calls())
        return asked and full_id not in self.containers()


@pytest.fixture
def docker_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DockerStub:
    state = tmp_path / "docker-stub-state"
    state.mkdir()
    directory = tmp_path / "docker-stub"
    directory.mkdir()
    script = directory / "docker"
    script.write_text(DOCKER_STUB_SOURCE.replace("__STATE_DIR__", json.dumps(str(state))))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ.get("PATH", ""))
    current_which = shutil.which
    monkeypatch.setattr(shutil, "which",
                        lambda cmd, *args, **kw: str(script) if cmd == "docker" else current_which(cmd, *args, **kw))
    return DockerStub(state)
