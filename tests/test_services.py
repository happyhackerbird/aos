from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from agent_os import resources
from agent_os.resources import POSTGRES_IMAGE, docker_available, load_services

DIGEST = "a" * 64
PINNED = "registry.example/team/postgres:16@sha256:" + DIGEST
VERSION_CALL = ["version", "--format", "{{.Server.Version}}"]


def _write(source: Path, text: str) -> None:
    directory = source / ".aos"
    directory.mkdir(exist_ok=True)
    (directory / "services.toml").write_text(text)


# == load_services ==========================================================

def test_absent_file_requests_nothing(tmp_path: Path) -> None:
    assert load_services(str(tmp_path)) == {}
    (tmp_path / ".aos").mkdir()
    assert load_services(str(tmp_path)) == {}


def test_empty_file_requests_nothing(tmp_path: Path) -> None:
    _write(tmp_path, "")
    assert load_services(str(tmp_path)) == {}


def test_empty_postgres_table_takes_the_default_image(tmp_path: Path) -> None:
    _write(tmp_path, "[postgres]\n")
    assert load_services(str(tmp_path)) == {"postgres": {"image": POSTGRES_IMAGE}}
    assert POSTGRES_IMAGE == ("postgres:15-alpine@sha256:"
                              "f7d23353e1b15400d22ebe31189f4d314b87a4c129cc400c8c2d8d4ca127bf81")


def test_explicit_pinned_image_is_returned(tmp_path: Path) -> None:
    _write(tmp_path, f'[postgres]\nimage = "{PINNED}"\n')
    assert load_services(str(tmp_path)) == {"postgres": {"image": PINNED}}


@pytest.mark.parametrize("text, message", [
    ("[mysql]\n", "services.toml: unknown table or key mysql"),
    ('redis = "x"\n', "services.toml: unknown table or key redis"),
    ("[postgres]\n[zeta]\n[alpha]\n", "services.toml: unknown table or key alpha"),
    ("[postgres]\nversion = 15\n", "services.toml: unknown key postgres.version"),
    (f'[postgres]\nzeta = 1\nimage = "{PINNED}"\nalpha = 2\n', "services.toml: unknown key postgres.alpha"),
    ('postgres = "yes"\n', "services.toml: postgres must be a table"),
    ("[[postgres]]\n", "services.toml: postgres must be a table"),
    ("[postgres]\nimage = 15\n", "services.toml: postgres.image must be a string"),
    ('[postgres]\nimage = "postgres:15"\n',
     "services.toml: postgres.image must be pinned by an @sha256: digest"),
    ('[postgres]\nimage = "postgres@sha256:abc"\n',
     "services.toml: postgres.image must be pinned by an @sha256: digest"),
    (f'[postgres]\nimage = "-v/host:/data@sha256:{DIGEST}"\n',
     "services.toml: postgres.image must be pinned by an @sha256: digest"),
    (f'[postgres]\nimage = "post gres@sha256:{DIGEST}"\n',
     "services.toml: postgres.image must be pinned by an @sha256: digest"),
    (f'[postgres]\nimage = "postgres@sha256:{DIGEST.upper()}"\n',
     "services.toml: postgres.image must be pinned by an @sha256: digest"),
])
def test_refusals_carry_the_exact_message(tmp_path: Path, text: str, message: str) -> None:
    _write(tmp_path, text)
    with pytest.raises(ValueError) as raised:
        load_services(str(tmp_path))
    assert str(raised.value) == message


def test_invalid_toml_is_a_value_error_with_the_parser_message(tmp_path: Path) -> None:
    text = "[postgres\n"
    _write(tmp_path, text)
    with pytest.raises(tomllib.TOMLDecodeError) as parsed:
        tomllib.loads(text)
    with pytest.raises(ValueError) as raised:
        load_services(str(tmp_path))
    assert str(raised.value) == f"services.toml: {parsed.value}"


def test_unreadable_file_is_a_value_error_with_the_os_message(tmp_path: Path) -> None:
    (tmp_path / ".aos" / "services.toml").mkdir(parents=True)
    with pytest.raises(OSError) as opened:
        (tmp_path / ".aos" / "services.toml").open("rb")
    with pytest.raises(ValueError) as raised:
        load_services(str(tmp_path))
    assert str(raised.value) == f"services.toml: {opened.value}"


def test_unreadable_aos_directory_refuses_with_services_prefix(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 directory")
    _write(tmp_path, "[postgres]\n")
    directory = tmp_path / ".aos"
    directory.chmod(0o000)
    try:
        with pytest.raises(ValueError) as caught:
            load_services(str(tmp_path))
    finally:
        directory.chmod(0o755)
    assert str(caught.value).startswith("services.toml: ")
    assert "Permission denied" in str(caught.value)


# == docker_available =======================================================

def test_docker_unavailable_without_docker(tmp_path: Path) -> None:
    # The autouse fixture hides docker; a PATH entry holding one is not enough.
    assert docker_available({"PATH": os.environ["PATH"]}) is False
    assert docker_available({}) is False


def test_docker_available_checks_context_then_daemon(docker_stub) -> None:
    assert docker_available({"PATH": os.environ["PATH"]}) is True
    assert docker_stub.calls() == [["context", "show"], VERSION_CALL]


def test_docker_host_skips_context_discovery(docker_stub) -> None:
    environ = {"PATH": os.environ["PATH"], "DOCKER_HOST": "unix:///stub.sock"}
    assert docker_available(environ) is True
    assert docker_stub.calls() == [VERSION_CALL]


def test_docker_context_variable_skips_context_discovery(docker_stub) -> None:
    environ = {"PATH": os.environ["PATH"], "DOCKER_CONTEXT": "remote"}
    assert docker_available(environ) is True
    assert docker_stub.calls() == [VERSION_CALL]


def test_empty_docker_host_does_not_skip_context_discovery(docker_stub) -> None:
    environ = {"PATH": os.environ["PATH"], "DOCKER_HOST": "", "DOCKER_CONTEXT": ""}
    assert docker_available(environ) is True
    assert docker_stub.calls() == [["context", "show"], VERSION_CALL]


def test_docker_unavailable_when_context_discovery_fails(docker_stub) -> None:
    docker_stub.fail("context")
    assert docker_available({"PATH": os.environ["PATH"]}) is False
    assert docker_stub.calls() == [["context", "show"]]


def test_docker_unavailable_when_the_daemon_is_unreachable(docker_stub) -> None:
    docker_stub.fail("daemon")
    assert docker_available({"PATH": os.environ["PATH"]}) is False
    assert docker_stub.calls() == [["context", "show"], VERSION_CALL]


# == start_postgres =========================================================

RUN_ID = "AO-900-r1"
CONTAINER = hashlib.sha256(f"aos-{RUN_ID}-postgres".encode()).hexdigest()
CONTEXT_PAIR = ["--context", "stub-context"]


@pytest.fixture
def database(tmp_path: Path, docker_stub) -> resources.RunResources:
    worktree = tmp_path / "wt"
    (worktree / ".aos").mkdir(parents=True)
    environ = {"PATH": os.environ["PATH"], "AOS_RESOURCE_ROOT": str(tmp_path / "resources")}
    return resources.RunResources(RUN_ID, str(worktree), environ)


def _raw_argvs(docker_stub) -> list[list[str]]:
    """Every recorded argv exactly as the stub received it, context pair included."""
    lines = (docker_stub.directory / "calls.jsonl").read_text().splitlines()
    return [json.loads(line)["argv"] for line in lines if line]


def _seed_image(docker_stub, image: str) -> None:
    (docker_stub.directory / "state.json").write_text(
        json.dumps({"images": [image], "containers": {}, "exec_calls": 0}))


def test_start_postgres_runs_the_pinned_sequence(database, docker_stub) -> None:
    url = database.start_postgres(POSTGRES_IMAGE)
    password = docker_stub.password()
    assert password is not None and len(password) == 32
    assert url == f"postgresql://aos:{password}@127.0.0.1:55432/aos"
    raw = _raw_argvs(docker_stub)
    assert raw[0] == ["context", "show"]
    assert all(argv[:2] == CONTEXT_PAIR for argv in raw[1:])
    run = ["run", "-d", "--name", f"aos-{RUN_ID}-postgres", "--label", f"aos.run={RUN_ID}",
           "--label", "aos.service=postgres", "--tmpfs", "/var/lib/postgresql/data",
           "-p", "127.0.0.1::5432", "-e", "POSTGRES_USER=aos", "-e", "POSTGRES_DB=aos",
           "-e", "POSTGRES_PASSWORD", POSTGRES_IMAGE]
    assert [argv[2:] for argv in raw[1:]] == [
        ["image", "inspect", POSTGRES_IMAGE],
        ["pull", POSTGRES_IMAGE],
        run,
        ["port", CONTAINER, "5432/tcp"],
        ["exec", CONTAINER, "pg_isready", "-h", "127.0.0.1", "-U", "aos", "-d", "aos"],
    ]
    assert docker_stub.calls()[1:] == [argv[2:] for argv in raw[1:]]
    # The password travels only in the docker client's environment, never in an argv.
    assert not any(password in part for argv in raw for part in argv)
    assert database.postgres_container == CONTAINER
    assert database.postgres_image == POSTGRES_IMAGE
    assert f"started Docker container {CONTAINER[:12]} (postgres)" in database.events
    assert password not in json.dumps(database.events)
    assert set(docker_stub.containers()) == {CONTAINER}
    assert docker_stub.containers()[CONTAINER]["labels"] == {"aos.run": RUN_ID, "aos.service": "postgres"}


def test_start_postgres_skips_the_pull_when_the_image_is_present(database, docker_stub) -> None:
    _seed_image(docker_stub, POSTGRES_IMAGE)
    database.start_postgres(POSTGRES_IMAGE)
    names = [call[0] for call in docker_stub.calls()[1:]]
    assert names == ["image", "run", "port", "exec"]


def test_start_postgres_passes_the_image_it_is_given(database, docker_stub) -> None:
    image = "registry.example/team/postgres:16@sha256:" + "d" * 64
    database.start_postgres(image)
    assert docker_stub.calls()[1] == ["image", "inspect", image]
    assert docker_stub.calls()[2] == ["pull", image]
    assert docker_stub.calls()[3][-1] == image
    assert database.postgres_image == image


def test_start_postgres_polls_readiness_until_the_probe_answers(database, docker_stub) -> None:
    docker_stub.ready_after(3)
    database.start_postgres(POSTGRES_IMAGE)
    assert [call[0] for call in docker_stub.calls() if call[:1] == ["exec"]] == ["exec"] * 3


def test_start_postgres_reads_the_timeouts_at_call_time(database, docker_stub, monkeypatch) -> None:
    monkeypatch.setattr(resources, "POSTGRES_PULL_SECONDS", 123)
    timeouts: dict[str, object] = {}
    real_run = subprocess.run

    def spy(argv, **kwargs):
        # argv is [docker, --context, stub-context, <verb>, ...]
        timeouts[argv[3]] = kwargs.get("timeout")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(resources.subprocess, "run", spy)
    database.start_postgres(POSTGRES_IMAGE)
    assert timeouts["pull"] == 123
    assert timeouts["image"] == 30 and timeouts["run"] == 30 and timeouts["port"] == 30
    assert 1.0 <= timeouts["exec"] <= 30.0


@pytest.mark.parametrize("output", ["0.0.0.0:55432", "garbage", "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:"])
def test_start_postgres_rejects_a_port_mapping_off_loopback_or_malformed(database, docker_stub, output) -> None:
    docker_stub.port_output(output)
    with pytest.raises(resources.ServiceError) as raised:
        database.start_postgres(POSTGRES_IMAGE)
    assert str(raised.value) == f"postgres: port: unexpected port mapping {output}"
    assert not [call for call in docker_stub.calls() if call[:1] == ["exec"]]


@pytest.mark.parametrize("stage, message", [
    ("pull", "postgres: pull: Error response from daemon: pull access denied for stub"),
    ("run", "postgres: start: docker: Error response from daemon: driver failed programming external connectivity"),
    ("port", "postgres: port: Error: No public port '5432/tcp' published"),
])
def test_start_postgres_maps_each_docker_failure_to_its_stage(database, docker_stub, stage, message) -> None:
    docker_stub.fail(stage)
    with pytest.raises(resources.ServiceError) as raised:
        database.start_postgres(POSTGRES_IMAGE)
    assert str(raised.value) == message
    assert raised.value.__cause__ is None
    # A failed `run` leaves the attribute unset; the label teardown still finds the container.
    assert database.postgres_container == (None if stage in ("pull", "run") else CONTAINER)


def test_start_postgres_readiness_failure_reports_the_probe_output(database, docker_stub, monkeypatch) -> None:
    monkeypatch.setattr(resources, "POSTGRES_READY_SECONDS", 1)
    docker_stub.fail("readiness")
    with pytest.raises(resources.ServiceError) as raised:
        database.start_postgres(POSTGRES_IMAGE)
    assert str(raised.value) == "postgres: readiness: 127.0.0.1:5432 - no response"
    assert len([call for call in docker_stub.calls() if call[:1] == ["exec"]]) >= 2
    assert database.postgres_container == CONTAINER


def test_start_postgres_without_docker_is_a_service_error(tmp_path: Path) -> None:
    # The autouse fixture hides docker, so this instance has none.
    bare = resources.RunResources(RUN_ID, str(tmp_path), {"PATH": os.environ["PATH"]})
    assert bare.docker is None
    with pytest.raises(resources.ServiceError) as raised:
        bare.start_postgres(POSTGRES_IMAGE)
    assert str(raised.value) == "postgres: start: Docker is unavailable"
    assert isinstance(raised.value, RuntimeError)


def test_record_names_the_container_and_image_but_never_the_password(database, docker_stub) -> None:
    assert database.postgres_container is None and database.postgres_image is None
    database.record()
    for path in (database.record_path, Path(database.worktree) / ".aos" / f"resources-{RUN_ID}.json"):
        data = json.loads(path.read_text())
        assert data["postgres_container"] is None and data["postgres_image"] is None
    database.start_postgres(POSTGRES_IMAGE)
    database.record()
    password = docker_stub.password()
    mirror = Path(database.worktree) / ".aos" / f"resources-{RUN_ID}.json"
    for path in (database.record_path, mirror):
        text = path.read_text()
        data = json.loads(text)
        assert data["postgres_container"] == CONTAINER
        assert data["postgres_image"] == POSTGRES_IMAGE
        assert password not in text


def test_restore_docker_restores_the_postgres_attributes(database, docker_stub, tmp_path: Path) -> None:
    database.start_postgres(POSTGRES_IMAGE)
    database.record()
    record = json.loads(database.record_path.read_text())
    environ = {"PATH": os.environ["PATH"], "AOS_RESOURCE_ROOT": str(tmp_path / "resources")}
    previous = resources.RunResources(RUN_ID, database.worktree, environ)
    assert previous.postgres_container is None
    previous.restore_docker(record)
    assert previous.postgres_container == CONTAINER
    assert previous.postgres_image == POSTGRES_IMAGE
    # An older record without the fields restores nothing.
    older = resources.RunResources(RUN_ID, database.worktree, environ)
    older.postgres_container = older.postgres_image = "stale"
    older.restore_docker({"docker_status": "not_applicable"})
    assert older.postgres_container is None and older.postgres_image is None


def test_label_teardown_removes_the_postgres_container(database, docker_stub) -> None:
    database.start_postgres(POSTGRES_IMAGE)
    database.stop_docker()
    assert docker_stub.removed(CONTAINER)
    assert f"removed owned Docker container {CONTAINER[:12]}" in database.events


def test_recover_runs_removes_the_postgres_container_of_an_unclosed_run(database, docker_stub, tmp_path: Path) -> None:
    database.start_postgres(POSTGRES_IMAGE)
    # No live process owns the record, as a crashed runner leaves it.
    database.runner_pid = 999999999
    database.runner_created = 1
    database.record()
    assert json.loads(database.record_path.read_text())["closed"] is False
    assert CONTAINER in docker_stub.containers()
    environ = {"PATH": os.environ["PATH"], "AOS_RESOURCE_ROOT": str(tmp_path / "resources")}
    current = resources.RunResources("AO-900-r2", database.worktree, environ)
    events = resources.recover_runs([{"id": RUN_ID, "status": "failed"}], current)
    assert docker_stub.removed(CONTAINER)
    assert docker_stub.containers() == {}
    assert any(f"removed owned Docker container {CONTAINER[:12]}" in event for event in events), events
    record = json.loads(database.record_path.read_text())
    assert record["closed"] is True
    assert record["postgres_container"] == CONTAINER
    assert record["postgres_image"] == POSTGRES_IMAGE


@pytest.mark.parametrize("exc, expected", [
    (subprocess.CalledProcessError(1, "docker", stderr="\n  first line  \nsecond\n", output="out"), "first line"),
    (subprocess.CalledProcessError(1, "docker", stderr="  \n", output="\nfrom stdout\n"), "from stdout"),
    (subprocess.CalledProcessError(7, "docker", stderr="", output=""), "exit 7"),
    (subprocess.CalledProcessError(7, "docker"), "exit 7"),
    (subprocess.TimeoutExpired("docker", 600), "timed out after 600 s"),
    (subprocess.TimeoutExpired("docker", 2.5), "timed out after 2.5 s"),
    (OSError("no such file"), "no such file"),
    (ValueError(), "ValueError"),
])
def test_first_line_reduces_an_exception_to_one_line(exc, expected) -> None:
    assert resources._first_line(exc) == expected
