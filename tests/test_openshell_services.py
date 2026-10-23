"""Per-run PostgreSQL for the OpenShell backend (unit U2): plan refusals, start argv and SQL, reset, stop.

A stub `docker` executable on PATH answers every call (no test reaches a real daemon). It records the argv, the
standard input and the two password variables of every call, so the tests can show where each secret travelled.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_os import openshell_services as services
from agent_os import resources

STUB = r'''#!/usr/bin/env python3
import hashlib, json, os, sys

STATE = __STATE_DIR__


def knob(name, default=None):
    try:
        return open(os.path.join(STATE, name)).read().strip()
    except OSError:
        return default


def load():
    try:
        return json.load(open(os.path.join(STATE, "state.json")))
    except OSError:
        return {"images": [], "containers": {}}


def save(state):
    json.dump(state, open(os.path.join(STATE, "state.json"), "w"))


def fail(message, code=1):
    sys.stderr.write(message + "\n")
    sys.exit(code)


args = sys.argv[1:]
if args[:1] == ["--context"]:
    args = args[2:]
exec_call = args[:1] == ["exec"]
stdin = sys.stdin.read() if exec_call and "-i" in args else None
with open(os.path.join(STATE, "calls.jsonl"), "a") as stream:
    stream.write(json.dumps({"argv": args, "stdin": stdin, "admin": os.environ.get("POSTGRES_PASSWORD"),
                             "login": os.environ.get("AOS_DB_PASSWORD")}) + "\n")
state = load()
mode = knob("fail")
if args[:2] == ["context", "show"]:
    print("stub-context")
elif args[:1] == ["version"]:
    print("29.0.0-stub")
elif args[:2] == ["image", "inspect"]:
    if args[2] not in state["images"]:
        fail("Error: No such image: " + args[2])
    print("[{}]")
elif args[:1] == ["pull"]:
    state["images"].append(args[-1])
    save(state)
elif args[:1] == ["run"]:
    name, labels = None, {}
    for i, token in enumerate(args):
        if token == "--name":
            name = args[i + 1]
        if token == "--label":
            key, _, value = args[i + 1].partition("=")
            labels[key] = value
    identity = hashlib.sha256(name.encode()).hexdigest()
    state["containers"][identity] = {"name": name, "labels": labels}
    save(state)
    print(identity)
elif args[:1] == ["port"]:
    print(knob("port_output", "127.0.0.1:55432"))
elif exec_call and "pg_isready" in args:
    if mode == "readiness":
        print("no response")
        sys.exit(2)
    print("accepting connections")
elif exec_call:
    if mode == "sql":
        fail('psql: error: connection to server failed\nERROR:  syntax near PASSWORD ' + (os.environ.get("AOS_DB_PASSWORD") or ""))
elif args[:1] == ["ps"]:
    wanted = [args[i + 1][len("label="):].partition("=") for i, t in enumerate(args) if t == "--filter"]
    for identity, record in sorted(state["containers"].items()):
        if all(record["labels"].get(key) == value for key, _, value in wanted):
            print(identity[:12])
elif args[:2] == ["rm", "-f"]:
    if mode != "stick":
        for prefix in args[2:]:
            for identity in [i for i in state["containers"] if i.startswith(prefix)]:
                del state["containers"][identity]
        save(state)
else:
    fail("stub: unsupported " + " ".join(args))
'''

IMAGE = resources.POSTGRES_IMAGE


class Stub:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def records(self) -> list[dict]:
        try:
            return [json.loads(line) for line in (self.directory / "calls.jsonl").read_text().splitlines() if line]
        except OSError:
            return []

    def calls(self, verb: str) -> list[dict]:
        return [r for r in self.records() if r["argv"][:1] == [verb]]

    def containers(self) -> dict:
        try:
            return json.loads((self.directory / "state.json").read_text())["containers"]
        except OSError:
            return {}

    def knob(self, name: str, value: str) -> None:
        (self.directory / name).write_text(value)

    def execs(self) -> list[dict]:
        return [r for r in self.calls("exec") if "pg_isready" not in r["argv"]]


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stub:
    directory = tmp_path / "docker-stub"
    directory.mkdir()
    script = directory / "docker"
    script.write_text(STUB.replace("__STATE_DIR__", json.dumps(str(directory))))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    current = shutil.which
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: str(script) if cmd == "docker" else current(cmd, *a, **k))
    monkeypatch.setattr(services.time, "sleep", lambda seconds: None)
    return Stub(directory)


def plan_for(**env: str) -> services.Plan:
    return services.Plan(IMAGE, env.get("bind", "127.0.0.1"), env.get("host", "192.168.5.2"))


def environ() -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "AOS_TOKEN": "bus-token-never-forwarded"}


def source_with(tmp_path: Path, text: str | None = "[postgres]\n") -> str:
    source = tmp_path / "repo"
    (source / ".aos").mkdir(parents=True, exist_ok=True)
    if text is not None:
        (source / ".aos" / "services.toml").write_text(text)
    return str(source)


# -- plan ---------------------------------------------------------------------------------------

def test_plan_without_a_postgres_request_is_none(tmp_path, stub):
    assert services.plan(source_with(tmp_path, None), "colima-mac", environ()) is None
    assert services.plan(source_with(tmp_path, ""), "colima-mac", environ()) is None


def test_plan_colima_mac_uses_the_measured_defaults(tmp_path, stub):
    assert services.DB_DEFAULTS["colima-mac"] == ("127.0.0.1", "192.168.5.2")
    assert services.plan(source_with(tmp_path), "colima-mac", environ()) == services.Plan(IMAGE, "127.0.0.1", "192.168.5.2")


def test_plan_honours_the_image_and_the_env_overrides(tmp_path, stub):
    pinned = "postgres:16-alpine@sha256:" + "a" * 64
    source = source_with(tmp_path, f'[postgres]\nimage = "{pinned}"\n')
    env = environ() | {"AOS_OPENSHELL_DB_BIND": "10.0.0.5", "AOS_OPENSHELL_DB_HOST": "10.0.0.9"}
    assert services.plan(source, "linux", env) == services.Plan(pinned, "10.0.0.5", "10.0.0.9")
    assert services.plan(source, "colima-mac", environ() | {"AOS_OPENSHELL_DB_HOST": "172.17.0.1"}) \
        == services.Plan(pinned, "127.0.0.1", "172.17.0.1")


def test_plan_linux_defaults_to_the_docker0_bridge_gateway(tmp_path, stub):
    assert services.DB_DEFAULTS["linux"] == ("172.17.0.1", "172.17.0.1")
    assert services.plan(source_with(tmp_path), "linux", environ()) == services.Plan(IMAGE, "172.17.0.1", "172.17.0.1")
    assert services.plan(source_with(tmp_path), "linux", environ() | {"AOS_OPENSHELL_DB_BIND": "10.0.0.5"}) \
        == services.Plan(IMAGE, "10.0.0.5", "172.17.0.1")
    assert services.plan(source_with(tmp_path), "linux", environ() | {"AOS_OPENSHELL_DB_HOST": "10.0.0.9"}) \
        == services.Plan(IMAGE, "172.17.0.1", "10.0.0.9")


@pytest.mark.parametrize("env", [{}, {"AOS_OPENSHELL_DB_BIND": "10.0.0.5"}, {"AOS_OPENSHELL_DB_HOST": "10.0.0.9"}])
def test_plan_a_platform_without_a_default_needs_both_settings(tmp_path, stub, env):
    with pytest.raises(ValueError) as excinfo:
        services.plan(source_with(tmp_path), "plan9", environ() | env)
    assert str(excinfo.value) == "services: postgres on plan9 needs AOS_OPENSHELL_DB_BIND and AOS_OPENSHELL_DB_HOST"
    assert stub.records() == []  # refused before Docker is touched


@pytest.mark.parametrize("name", ["AOS_OPENSHELL_DB_BIND", "AOS_OPENSHELL_DB_HOST"])
@pytest.mark.parametrize("value", ["localhost", "::1", "host.docker.internal", "999.1.1.1", "10.0.0.1:5432", ""])
def test_plan_refuses_a_non_ipv4_address(tmp_path, stub, name, value):
    env = environ() | {"AOS_OPENSHELL_DB_BIND": "127.0.0.1", "AOS_OPENSHELL_DB_HOST": "192.168.5.2", name: value}
    with pytest.raises(ValueError) as excinfo:
        services.plan(source_with(tmp_path), "colima-mac", env)
    assert name in str(excinfo.value) and "IPv4" in str(excinfo.value)


def test_plan_refuses_without_docker(tmp_path):
    # no `stub` fixture: the autouse fixture hides every docker
    with pytest.raises(ValueError) as excinfo:
        services.plan(source_with(tmp_path), "colima-mac", {"PATH": "/nonexistent"})
    assert str(excinfo.value) == "services: postgres requested but Docker is unavailable"


def test_plan_passes_the_loader_refusals_through(tmp_path, stub):
    with pytest.raises(ValueError) as excinfo:
        services.plan(source_with(tmp_path, "[redis]\n"), "colima-mac", environ())
    assert "services.toml: unknown table or key redis" in str(excinfo.value)


# -- Database -------------------------------------------------------------------------------------

def test_database_url_encodes_the_password_and_names_the_login_database():
    database = services.Database("c1", 55432, "p@ss/w:rd'%", "192.168.5.2")
    assert database.url == "postgresql://aos:p%40ss%2Fw%3Ard%27%25@192.168.5.2:55432/aos"
    assert database.grants == (("192.168.5.2", 55432),)
    assert database.env_file_text() == "export DATABASE_URL=" + database.url + "\n"  # shlex leaves the encoded URL bare


def test_database_redact_replaces_the_password_in_both_spellings():
    database = services.Database("c1", 1, "p@ss", "192.168.5.2")
    assert database.redact("login p@ss and url aos:p%40ss@h") == "login *** and url aos:***@h"
    assert database.redact(None) is None
    assert database.redact("nothing") == "nothing"


def test_redact_bytes_replaces_both_spellings():
    # F111 (D5 iv): the evidence files and the Pi session file are bytes; the same spellings, longest first.
    database = services.Database("c1", 1, "p@ss/w", "192.168.5.2")
    assert database.redact_bytes(b"login p@ss/w and url aos:p%40ss%2Fw@h\n") == b"login *** and url aos:***@h\n"
    assert database.redact_bytes(b"\xff\xfe binary p@ss/w") == b"\xff\xfe binary ***"
    assert database.redact_bytes(b"nothing") == b"nothing"
    assert database.redact("login p@ss/w") == "login ***"  # redact unchanged


# -- start ----------------------------------------------------------------------------------------

def test_start_argv_sql_and_secret_routes(stub):
    database = services.start(plan_for(), "AO-1-r1", environ())
    records = stub.records()
    run = stub.calls("run")[0]
    argv = run["argv"]
    assert argv[:2] == ["run", "-d"] and argv[argv.index("--name") + 1] == "aos-AO-1-r1-postgres"
    assert ["--label", "aos.run=AO-1-r1"] == argv[argv.index("--label"):argv.index("--label") + 2]
    for label in ("aos.service=postgres", "aos.backend=openshell"):
        assert label in argv
    assert argv[argv.index("--tmpfs") + 1] == "/var/lib/postgresql/data" and argv[argv.index("-p") + 1] == "127.0.0.1::5432"
    assert "POSTGRES_USER=aos_admin" in argv and "POSTGRES_DB=postgres" in argv and "POSTGRES_USER=aos" not in argv
    assert "POSTGRES_PASSWORD" in argv and argv[-1] == IMAGE
    admin = run["admin"]
    assert admin and admin != database.password
    # the image is inspected, pulled on a miss, and the readiness probe is the TCP one
    assert [r["argv"][0] for r in records if r["argv"][:2] == ["image", "inspect"] or r["argv"][0] in ("pull", "run", "port")] \
        == ["image", "pull", "run", "port"]
    assert any(r["argv"][:1] == ["exec"] and "pg_isready" in r["argv"] and "127.0.0.1" in r["argv"] for r in records)
    # no argv anywhere carries either password
    for record in records:
        assert not any(admin in token or database.password in token for token in record["argv"])
    # the setup SQL arrives on stdin; the login password only in the exec's environment
    [setup] = stub.execs()
    assert setup["argv"][:3] == ["exec", "-i", "-e"] and "AOS_DB_PASSWORD" in setup["argv"]
    assert "sh" in setup["argv"] and "-c" in setup["argv"]
    command = setup["argv"][-1]
    assert command == "psql -q -U aos_admin -d postgres -v ON_ERROR_STOP=1 -v pw=\"$AOS_DB_PASSWORD\""
    assert setup["login"] == database.password and database.password not in setup["stdin"] and admin not in setup["stdin"]
    assert setup["stdin"].strip() == ("CREATE ROLE aos LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'pw'; "
                                      "CREATE DATABASE aos OWNER aos TEMPLATE template0; "
                                      "REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;")
    assert database.container == next(iter(stub.containers())) and database.port == 55432 and database.host == "192.168.5.2"
    assert database.url.startswith("postgresql://aos:") and database.url.endswith("@192.168.5.2:55432/aos")


def test_start_does_not_pass_the_bus_token_to_docker(stub, monkeypatch):
    seen = []
    real = subprocess.run
    monkeypatch.setattr(services.subprocess, "run", lambda *a, **k: seen.append(k["env"]) or real(*a, **k))
    services.start(plan_for(), "AO-1-r1", environ())
    assert seen and all("AOS_TOKEN" not in env for env in seen)


def test_start_skips_the_pull_when_the_image_is_present(stub):
    (stub.directory / "state.json").write_text(json.dumps({"images": [IMAGE], "containers": {}}))
    services.start(plan_for(), "AO-1-r1", environ())
    assert stub.calls("pull") == []


def test_start_refuses_a_port_published_on_another_address(stub):
    stub.knob("port_output", "0.0.0.0:55432")
    with pytest.raises(resources.ServiceError) as excinfo:
        services.start(plan_for(), "AO-1-r1", environ())
    assert str(excinfo.value).startswith("postgres: port: ")
    assert stub.containers() == {}  # stop() ran, best effort


def test_start_readiness_timeout_removes_the_container(stub, monkeypatch):
    stub.knob("fail", "readiness")
    monkeypatch.setattr(services, "POSTGRES_READY_SECONDS", 0)
    with pytest.raises(resources.ServiceError) as excinfo:
        services.start(plan_for(), "AO-1-r1", environ())
    assert str(excinfo.value).startswith("postgres: readiness: ") and stub.containers() == {}


def test_start_setup_failure_names_the_stage_and_never_the_password(stub):
    stub.knob("fail", "sql")
    with pytest.raises(resources.ServiceError) as excinfo:
        services.start(plan_for(), "AO-1-r1", environ())
    message = str(excinfo.value)
    assert message.startswith("postgres: setup: ") and "psql: error" in message
    login = stub.execs()[0]["login"]
    assert login not in message and stub.containers() == {}


# -- reset ----------------------------------------------------------------------------------------

def test_reset_sql_on_stdin_starts_by_clearing_role_settings(stub):
    database = services.start(plan_for(), "AO-1-r1", environ())
    services.reset(database, environ())
    reset = stub.execs()[-1]
    assert len(stub.execs()) == 2 and reset["argv"][:3] == ["exec", "-i", "-e"]
    assert reset["argv"][-1] == "psql -q -U aos_admin -d postgres -v ON_ERROR_STOP=1 -v pw=\"$AOS_DB_PASSWORD\""
    assert reset["login"] == database.password and database.password not in reset["stdin"]
    assert reset["stdin"].strip() == (
        "ALTER ROLE aos RESET ALL; "
        "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE datname = 'aos' AND pid <> pg_backend_pid(); "
        "DROP DATABASE aos; CREATE DATABASE aos OWNER aos TEMPLATE template0; ALTER ROLE aos PASSWORD :'pw';")
    assert not any(database.password in token for r in stub.records() for token in r["argv"])


def test_reset_failure_is_a_service_error_without_the_password(stub):
    database = services.start(plan_for(), "AO-1-r1", environ())
    stub.knob("fail", "sql")
    with pytest.raises(resources.ServiceError) as excinfo:
        services.reset(database, environ())
    assert str(excinfo.value).startswith("postgres: reset: psql: error") and database.password not in str(excinfo.value)


# -- stop -----------------------------------------------------------------------------------------

def test_stop_removes_only_this_runs_labelled_containers_and_relists(stub):
    services.start(plan_for(), "AO-1-r1", environ())
    other = services.start(plan_for(), "AO-2-r1", environ())
    events = services.stop("AO-1-r1", environ())
    assert events and list(stub.containers()) == [other.container]
    removal = [r["argv"] for r in stub.calls("rm")]
    assert removal and removal[0][:2] == ["rm", "-f"]
    listings = [r["argv"] for r in stub.calls("ps")]
    assert len(listings) >= 2 and all("label=aos.run=AO-1-r1" in a and "label=aos.service=postgres" in a for a in listings[-2:])
    assert services.stop("AO-1-r1", environ()) == []  # nothing left is not a failure


def test_stop_raises_when_a_container_remains(stub):
    services.start(plan_for(), "AO-1-r1", environ())
    stub.knob("fail", "stick")
    with pytest.raises(resources.ServiceError) as excinfo:
        services.stop("AO-1-r1", environ())
    assert str(excinfo.value).startswith("postgres: stop: ")


def test_stop_without_docker_raises():
    with pytest.raises(resources.ServiceError) as excinfo:
        services.stop("AO-1-r1", {"PATH": "/nonexistent"})
    assert str(excinfo.value) == "postgres: stop: Docker is unavailable"
