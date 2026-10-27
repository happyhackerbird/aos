"""A disposable PostgreSQL for one OpenShell run (records D25, D45, D50, D64 of the verify-acceptance run).

The runner starts the container on the runner host before the job sandbox is released, publishes it on a
configured address, and hands the job and acceptance sandboxes a ``DATABASE_URL`` for a non-superuser login
through a per-run network grant naming that one IPv4 endpoint. The container's superuser (``aos_admin``) and its
password never leave the runner: setup and reset run through the container's local socket with the SQL on
standard input and the login password only in the ``docker exec`` environment, never in an argv.

Import-only precedent (never edited): ``agent_os.resources`` ``load_services``, ``docker_available``,
``RunResources.start_postgres`` and ``stop_docker``.
"""
from __future__ import annotations

import dataclasses
import ipaddress
import secrets
import shlex
import shutil
import subprocess
import time
from collections.abc import Mapping
from urllib.parse import quote

from agent_os.resources import (POSTGRES_PULL_SECONDS, POSTGRES_READY_SECONDS, ServiceError, _first_line,
                                docker_available, load_services)

# platform -> (publish address on the runner host, address the sandbox reaches it by); F14 measured colima-mac,
# linux is the docker0 bridge gateway (a host with another bridge address sets both settings).
DB_DEFAULTS = {"colima-mac": ("127.0.0.1", "192.168.5.2"), "linux": ("172.17.0.1", "172.17.0.1")}
BIND_VAR = "AOS_OPENSHELL_DB_BIND"
HOST_VAR = "AOS_OPENSHELL_DB_HOST"
ADMIN = "aos_admin"
PSQL = f'psql -q -U {ADMIN} -d postgres -v ON_ERROR_STOP=1 -v pw="$AOS_DB_PASSWORD"'
SETUP_SQL = ("CREATE ROLE aos LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'pw'; "
             "CREATE DATABASE aos OWNER aos TEMPLATE template0; "
             "REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;\n")
RESET_SQL = ("ALTER ROLE aos RESET ALL; "
             "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE datname = 'aos' AND pid <> pg_backend_pid(); "
             "DROP DATABASE aos; CREATE DATABASE aos OWNER aos TEMPLATE template0; ALTER ROLE aos PASSWORD :'pw';\n")


@dataclasses.dataclass(frozen=True)
class Plan:
    image: str
    bind: str
    host: str


def _ipv4(name: str, value: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise ValueError(f"services: {name} must be an IPv4 literal (got {value!r})") from None


def plan(source: str, platform: str, environ: Mapping[str, str]) -> Plan | None:
    """None when the repository requests no database; ValueError when the request cannot be met."""
    requested = load_services(source).get("postgres")
    if requested is None:
        return None
    bind, host = DB_DEFAULTS.get(platform, (None, None))
    bind, host = environ.get(BIND_VAR) or bind, environ.get(HOST_VAR) or host
    if platform not in DB_DEFAULTS and not (environ.get(BIND_VAR) and environ.get(HOST_VAR)):
        raise ValueError(f"services: postgres on {platform} needs {BIND_VAR} and {HOST_VAR}")
    for name, value in ((BIND_VAR, environ.get(BIND_VAR)), (HOST_VAR, environ.get(HOST_VAR))):
        if value is not None:
            _ipv4(name, value)
    if not docker_available(environ):
        raise ValueError("services: postgres requested but Docker is unavailable")
    return Plan(requested["image"], _ipv4(BIND_VAR, bind), _ipv4(HOST_VAR, host))


@dataclasses.dataclass(frozen=True)
class Database:
    container: str
    port: int
    password: str
    host: str

    @property
    def url(self) -> str:
        return f"postgresql://aos:{quote(self.password, safe='')}@{self.host}:{self.port}/aos"

    @property
    def grants(self) -> tuple[tuple[str, int], ...]:
        return ((self.host, self.port),)

    def env_file_text(self) -> str:
        return "export DATABASE_URL=" + shlex.quote(self.url) + "\n"

    def redact(self, text: str | None) -> str | None:
        if text is None:
            return None
        for spelling in sorted({self.password, quote(self.password, safe="")}, key=len, reverse=True):
            if spelling:
                text = text.replace(spelling, "***")
        return text

    def redact_bytes(self, data: bytes) -> bytes:
        """`redact` over bytes: the evidence files and the retained Pi session file (F111)."""
        for spelling in sorted({self.password, quote(self.password, safe="")}, key=len, reverse=True):
            if spelling:
                data = data.replace(spelling.encode(), b"***")
        return data


class _Docker:
    """One Docker endpoint for a few calls: the environment minus the bus token, the discovered context."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self.env = {k: v for k, v in environ.items() if k != "AOS_TOKEN"}
        docker = shutil.which("docker", path=environ.get("PATH"))
        if docker is None:
            raise ServiceError("Docker is unavailable")
        self.prefix = [docker]
        if not environ.get("DOCKER_HOST") and not environ.get("DOCKER_CONTEXT"):
            context = self.run("context", "show").strip()
            self.prefix += ["--context", context]

    def run(self, *args: str, timeout: float = 30, env: dict[str, str] | None = None, input: str | None = None) -> str:
        argv = [*self.prefix, *args] if self.prefix else list(args)
        return subprocess.run(argv, env=self.env | (env or {}), capture_output=True, text=True, check=True,
                              timeout=timeout, input=input).stdout


def _docker_for(environ: Mapping[str, str], stage: str) -> _Docker:
    try:
        return _Docker(environ)
    except ServiceError as exc:
        raise ServiceError(f"postgres: {stage}: {exc}") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceError(f"postgres: {stage}: {_first_line(exc)}") from None


def _sql(docker: _Docker, container: str, sql: str, password: str) -> None:
    docker.run("exec", "-i", "-e", "AOS_DB_PASSWORD", container, "sh", "-c", PSQL, input=sql,
               env={"AOS_DB_PASSWORD": password}, timeout=60)


def _scrub(text: str, *secret: str) -> str:
    for value in secret:
        text = text.replace(value, "***").replace(quote(value, safe=""), "***")
    return text


def start(plan: Plan, run_id: str, environ: Mapping[str, str]) -> Database:
    """Start the run's container; any failure removes it (best effort) and raises ``postgres: <stage>: <reason>``."""
    docker = _docker_for(environ, "start")
    admin, login = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    stage = "pull"
    try:
        try:
            docker.run("image", "inspect", plan.image)
        except subprocess.CalledProcessError:
            docker.run("pull", plan.image, timeout=POSTGRES_PULL_SECONDS)
        stage = "start"
        container = docker.run(
            "run", "-d", "--name", f"aos-{run_id}-postgres", "--label", f"aos.run={run_id}",
            "--label", "aos.service=postgres", "--label", "aos.backend=openshell",
            "--tmpfs", "/var/lib/postgresql/data", "-p", f"{plan.bind}::5432",
            "-e", f"POSTGRES_USER={ADMIN}", "-e", "POSTGRES_DB=postgres", "-e", "POSTGRES_PASSWORD", plan.image,
            env={"POSTGRES_PASSWORD": admin}).strip()
        stage = "port"
        line = docker.run("port", container, "5432/tcp").splitlines()[0].strip()
        host, _, port = line.rpartition(":")
        if host != plan.bind or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"unexpected port mapping {line}")
        stage = "readiness"
        deadline = time.monotonic() + POSTGRES_READY_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            try:
                docker.run("exec", container, "pg_isready", "-h", "127.0.0.1", "-U", ADMIN, "-d", "postgres",
                           timeout=max(1.0, min(30.0, remaining)))
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
        stage = "setup"
        _sql(docker, container, SETUP_SQL, login)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        reason = _scrub(_first_line(exc), admin, login)
        try:
            stop(run_id, environ)
        except (ServiceError, OSError, subprocess.SubprocessError):
            pass
        raise ServiceError(f"postgres: {stage}: {reason}") from None
    return Database(container, int(port), login, plan.host)


def reset(database: Database, environ: Mapping[str, str]) -> None:
    """A fresh state for acceptance: nothing the harness created in the login's database or role survives."""
    docker = _docker_for(environ, "reset")
    try:
        _sql(docker, database.container, RESET_SQL, database.password)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceError(f"postgres: reset: {_scrub(_first_line(exc), database.password)}") from None


def stop(run_id: str, environ: Mapping[str, str]) -> list[str]:
    """Remove every container labelled for this run's database, re-list, and raise when any remains."""
    docker = _docker_for(environ, "stop")
    listing = ["ps", "-aq", "--filter", f"label=aos.run={run_id}", "--filter", "label=aos.service=postgres"]
    try:
        found = sorted(set(docker.run(*listing).split()))
        events = []
        if found:
            docker.run("rm", "-f", *found)
            events = [f"removed Docker container {identity}" for identity in found]
        remaining = docker.run(*listing).split()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceError(f"postgres: stop: {_first_line(exc)}") from None
    if remaining:
        raise ServiceError(f"postgres: stop: {len(remaining)} container(s) remain")
    return events
