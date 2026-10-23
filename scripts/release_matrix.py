"""MVP release matrix driver for one host (probe tooling, not product code).

For each selected harness and scenario it queues a task on a disposable local bus, runs
``aos run --backend openshell`` with the operator environment for that harness, checks the
outcome against the expectation, and writes ``<work>/matrix.json`` plus a markdown record.
See scripts/release_matrix.md. Values of credentials and tokens are never printed; the bus
token travels in the environment only.

    uv run python scripts/release_matrix.py --host mac --fixture demo-app --harness claude \\
        --work-dir ~/.aos/release-matrix/mac-claude-demo
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_os import openshell_runner  # noqa: E402  (sandbox naming rule, one source of truth)

# -- tables -------------------------------------------------------------------------------

# Per matrix harness: the task's --harness, the runner environment, and the files it needs.
HARNESS_ENV: dict[str, dict[str, Any]] = {
    "claude": {"task_harness": "claude", "provider": "aos-claude-oauth", "files": (),
               "env": {"AOS_OPENSHELL_PROVIDER_CLAUDE": "aos-claude-oauth"}},
    "codex": {"task_harness": "codex", "provider": None, "files": ("AOS_OPENSHELL_CODEX_AUTH",),
              "env": {"AOS_OPENSHELL_CODEX_AUTH": "~/.aos/worker/codex-home/auth.json",
                      "AOS_OPENSHELL_HARNESS_HOSTS_CODEX":
                          "chatgpt.com:443,auth.openai.com:443,ab.chatgpt.com:443"}},
    "pi": {"task_harness": "pi", "provider": None, "files": ("AOS_OPENSHELL_PI_AUTH",),
           "env": {"AOS_OPENSHELL_PI_AUTH": "~/.aos/worker/pi-auth.json",
                   "AOS_OPENSHELL_PI_MODEL": "gpt-6-luna",
                   "AOS_OPENSHELL_HARNESS_HOSTS_PI": "chatgpt.com:443"}},
    "omp": {"task_harness": "pi", "provider": None, "files": ("AOS_OPENSHELL_PI_AUTH",),
            "env": {"AOS_OPENSHELL_PI_AUTH": "~/.aos/worker/pi-auth.json",
                    "AOS_OPENSHELL_PI_FLAVOUR": "omp",
                    "AOS_OPENSHELL_PI_MODEL": "openai-codex/gpt-6-luna",
                    "AOS_OPENSHELL_HARNESS_HOSTS_PI": "chatgpt.com:443"}},
}

FIXTURES: dict[str, dict[str, Any]] = {
    "demo-app": {
        "copy_from": ROOT / "examples" / "demo-app",
        "positive_file": "TASK.md",
        "acceptance_file": "acceptance.txt",
        # The brief must not contradict the prompt's "acceptance must pass" without saying why, or a
        # model stops without committing (claude, 2026-10-13: "no commits") instead of failing acceptance.
        "negative": "Document the `GET /` endpoint in README.md in one paragraph and commit with the "
                    "message `docs only`. Do not change app.py or verify.sh: the failing `bash verify.sh` "
                    "is expected and is handled by another task, so commit your documentation change "
                    "regardless.",
        "untracked": {},
    },
    "aos-example": {
        "clone_from": Path("~/code/aos-example").expanduser(),
        "positive": "After saving a note on the start page it is missing from the list once there are more "
                    "than five notes. Repair it and add a unit test for the order of the start-page list. "
                    "Run scripts/check.sh before you finish.",
        "acceptance": ["uv sync --frozen", "bash scripts/check.sh"],
        "negative": "Document the start-page route in README.md in one paragraph and commit with the "
                    "message `docs only`. Do not change anything under notes/: the failing "
                    "`bash scripts/check.sh` is expected and is handled by another task, so commit your "
                    "documentation change regardless.",
        "untracked": {".aos/services.toml": "[postgres]\n", ".aos/evidence.toml": 'dir = "evidence"\n'},
    },
    # The operator's own repositories (MVP decision 6c): a marker task under each repository's
    # policy; the acceptance commands are the ones measured in the harness image on 2026-10-13.
    # Point `--clone-from freelance_bot=<path>` at a clone whose default branch carries the policy.
    "freelance_bot": {
        "clone_from": Path("~/.aos/release-matrix/own-repos/freelance_bot").expanduser(),
        "positive": "Create the file docs/aos-openshell-marker.md with exactly one line: "
                    "`freelance_bot ran under its own aos-policy.toml on OpenShell.` Commit it with the "
                    "message `probe: AOS OpenShell marker`. Do not change anything else.",
        # Eight test files are left out so the acceptance measures the sandbox, not the repository's
        # state on 2026-10-13: five fail at the default-branch tip in the image as on the host (test
        # fakes without the new `pre_frame` keyword, detached SQLAlchemy instances), three depend on
        # the operator's machine (a sibling scope-agent checkout under the home directory, the
        # `.env` database URL) and only pass in the full run through a fixture of the first five.
        "acceptance": ["poetry install --no-root",
                       "poetry run python -m pytest -q -p no:cacheprovider tests "
                       "--ignore=tests/test_publish_draft.py --ignore=tests/test_review.py "
                       "--ignore=tests/test_skills_years.py --ignore=tests/test_task5_local_e2e.py "
                       "--ignore=tests/test_voice_pass.py --ignore=tests/test_apply_worker.py "
                       "--ignore=tests/test_apply_worker_evidence_steps.py --ignore=tests/test_decisions_ui.py"],
        "negative": "Document the marker file in README.md in one paragraph and commit with the message "
                    "`docs only`. Do not change anything else; the failing test run is expected and is "
                    "handled by another task, so commit your documentation change regardless.",
        "untracked": {},
    },
    "super-productivity": {
        "clone_from": Path("~/.aos/release-matrix/own-repos/super-productivity").expanduser(),
        "positive": "Create the file docs/aos-openshell-marker.md with exactly one line: "
                    "`super-productivity ran under its own aos-policy.toml on OpenShell.` Commit it with "
                    "the message `probe: AOS OpenShell marker`. Do not change anything else. If you run "
                    "`npm ci` yourself, pass `--ignore-scripts --maxsockets 4` (the sandbox egress refuses "
                    "npm's default parallel connections) with ELECTRON_SKIP_BINARY_DOWNLOAD=1.",
        # --maxsockets 4: with npm's default 15 parallel sockets the sandbox egress refuses most
        # connects to the allowed registry host (EACCES on connect, measured 2026-10-13); with four
        # the install went through on Linux, while one Mac acceptance still lost a single connect,
        # so the command gets a second attempt (npm reuses what the first one cached).
        "acceptance": ["export ELECTRON_SKIP_BINARY_DOWNLOAD=1; "
                       "npm ci --ignore-scripts --no-audit --no-fund --maxsockets 4 --fetch-retries 5 || "
                       "npm ci --ignore-scripts --no-audit --no-fund --maxsockets 2 --fetch-retries 5",
                       "npx ts-patch install && npm run env && npm run sync-core:build && "
                       "npm run sync-providers:build && npm run shared-schema:build && npm run plugin-api:build",
                       "npm run test:tools"],
        "negative": "Document the marker file in README.md in one paragraph and commit with the message "
                    "`docs only`. Do not change anything else; the failing test run is expected and is "
                    "handled by another task, so commit your documentation change regardless.",
        "untracked": {},
    },
}

# brief: which brief the task gets; expect: what the final state must be.
SCENARIOS: dict[str, dict[str, str]] = {
    "success": {"brief": "positive",
                "expect": "task review, verdict pass, result commit on ao/<id>"},
    "failed-verification": {"brief": "negative", "expect": "task failed, verdict fail"},
    "interruption": {"brief": "positive",
                     "expect": "aos cancel while running: task cancelled, runner exits within 180 s"},
    "retry": {"brief": "positive",
              "expect": "requeue of the interrupted task, run r2: task review, verdict pass"},
    "runner-kill": {"brief": "positive",
                    "expect": "SIGKILL of aos run: lease expiry blocks the task, reconcile deletes "
                              "the sandbox"},
}
DEFAULT_SCENARIOS = ("success", "failed-verification", "interruption", "retry")
PLATFORMS = {"mac": "colima-mac", "server": "linux"}
INTERRUPT_AFTER = 45     # default seconds of `running` before an interruption (--interrupt-after)
EXIT_AFTER_CANCEL = 180  # the runner must exit this soon after an interruption
POLL = 5
LEASE_SECONDS, SWEEP_SECONDS = 120, 15
COMMON_RUNNER_VARS = ("AOS_URL", "AOS_TOKEN", "AOS_HOST", "AOS_REPO_ROOT", "AOS_OPENSHELL_PLATFORM",
                      "AOS_OPENSHELL_IMAGE", "AOS_OPENSHELL_BUS_PORT", "AOS_OPENSHELL_STATE_DIR",
                      "AOS_HARNESS_TIMEOUT_SECONDS")


def log(message: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {message}", flush=True)


def run(argv: list[str], *, check: bool = False, timeout: int = 120,
        **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout,
                          check=check, **kw)


def expand(value: str) -> str:
    return os.path.expanduser(value) if value.startswith("~") else value


# -- prerequisites --------------------------------------------------------------------------

def prerequisites(args: argparse.Namespace) -> list[str]:
    """Everything checkable before a bus starts; one line per missing item."""
    missing: list[str] = []
    aos = Path(sys.executable).parent / "aos"
    if not aos.is_file():
        missing.append(f"aos entry point not found next to {sys.executable} (run under `uv run`)")
    for tool in ("docker", "openshell", "git"):
        if shutil.which(tool) is None:
            missing.append(f"{tool} is not on PATH")
    if shutil.which("docker") and run(["docker", "image", "inspect", args.image]).returncode != 0:
        missing.append(f"image {args.image} is missing (docker image inspect failed)")
    if shutil.which("openshell"):
        status = run(["openshell", "status"], timeout=60)
        if status.returncode != 0 or not re.search(r"Status:\s*Connected", status.stdout):
            missing.append("openshell gateway is not connected (`openshell status`)")
        providers = {HARNESS_ENV[h]["provider"] for h in args.harness} - {None}
        if providers:
            listing = run(["openshell", "provider", "list"], timeout=60).stdout
            names = {line.split()[0] for line in listing.splitlines()[1:] if line.split()}
            missing += [f"gateway provider {p} is missing (`openshell provider list`)"
                        for p in sorted(providers - names)]
    for harness in args.harness:
        spec = HARNESS_ENV[harness]
        for key in spec["files"]:
            path = expand(spec["env"][key])
            if not os.path.isfile(path):
                missing.append(f"{harness}: {key} file {path} is missing")
        if spec["task_harness"] == "pi" and os.path.isfile(expand(spec["env"]["AOS_OPENSHELL_PI_AUTH"])):
            from agent_os import pi
            try:
                pi.selected_auth(expand(spec["env"]["AOS_OPENSHELL_PI_AUTH"]),
                                 min_validity_seconds=args.harness_timeout + 300)
            except pi.AuthError as exc:
                missing.append(f"{harness}: worker auth refused: {exc}")
    for fixture in args.fixture:
        spec = FIXTURES[fixture]
        source = spec.get("copy_from") or spec.get("clone_from")
        if not Path(source).is_dir():
            missing.append(f"fixture {fixture}: source {source} is missing")
    if args.port in (8010, 8011):
        missing.append(f"port {args.port} belongs to another bus; choose another --port")
    return missing


# -- bus ------------------------------------------------------------------------------------

class Bus:
    def __init__(self, work: Path, port: int, bind: str = "127.0.0.1") -> None:
        self.work, self.port, self.bind = work, port, bind
        self.url = f"http://{bind}:{port}"
        token_file = work / "bus-token"
        if not token_file.exists():
            token_file.write_text(secrets.token_urlsafe(32))
            os.chmod(token_file, 0o600)
        self.token = token_file.read_text().strip()
        self.proc: subprocess.Popen | None = None
        self.client = httpx.Client(base_url=self.url, timeout=30,
                                   headers={"Authorization": f"Bearer {self.token}"})

    def _answers(self) -> int | None:
        try:
            return self.client.get("/tasks", params={"status": "ready"}).status_code
        except httpx.HTTPError:
            return None

    def start(self) -> None:
        code = self._answers()
        if code == 200:
            log(f"bus: reusing the bus already on {self.url} (this work dir's token is accepted)")
            return
        if code is not None:
            raise SystemExit(f"port {self.port} answers HTTP {code} to this work dir's token; not our bus")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "LANG": "C.UTF-8",
               "BUS_TOKEN": self.token, "BUS_DATA_DIR": str(self.work / "bus-data"),
               "TASK_LEASE_SECONDS": str(LEASE_SECONDS), "TASK_SWEEP_INTERVAL_SECONDS": str(SWEEP_SECONDS)}
        (self.work / "bus-data").mkdir(exist_ok=True)
        stream = open(self.work / "logs" / "bus.log", "a")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "agent_os.api:create_app", "--factory",
             "--host", self.bind, "--port", str(self.port)],
            cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise SystemExit(f"bus exited {self.proc.returncode} at start; see {self.work}/logs/bus.log")
            try:
                if httpx.get(f"{self.url}/health", timeout=5).status_code == 200:
                    log(f"bus: started pid {self.proc.pid} on {self.url}")
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise SystemExit("bus did not answer /health within 60 s")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            log("bus: stopped")

    def task(self, task_id: str) -> dict[str, Any]:
        response = self.client.get(f"/tasks/{task_id}")
        response.raise_for_status()
        return response.json()


# -- fixtures -------------------------------------------------------------------------------

def git(*argv: str, cwd: Path) -> subprocess.CompletedProcess:
    return run(["git", "-c", "user.name=aos release matrix", "-c", "user.email=release-matrix@aos.invalid",
                "-c", "commit.gpgsign=false", *argv], cwd=cwd, check=True)


def head(repo: Path) -> str:
    return git("rev-parse", "--short", "HEAD", cwd=repo).stdout.strip()


def prepare_fixture(work: Path, fixture: str) -> tuple[Path, dict[str, Any]]:
    """The fixture repository (created once per work dir) and its briefs and acceptance commands."""
    spec = FIXTURES[fixture]
    repo = work / "repos" / fixture
    if "copy_from" in spec:
        source = Path(spec["copy_from"])
        lines = (source / spec["acceptance_file"]).read_text().splitlines()
        plan = {"positive": (source / spec["positive_file"]).read_text().strip(),
                "acceptance": [line.strip() for line in lines if line.strip()]}
    else:
        source = Path(spec["clone_from"])
        plan = {"positive": spec["positive"], "acceptance": list(spec["acceptance"])}
    plan["negative"] = spec["negative"]
    if (repo / ".git").is_dir():
        log(f"fixture {fixture}: reusing {repo} at {head(repo)}")
        return repo, plan
    repo.parent.mkdir(parents=True, exist_ok=True)
    if "copy_from" in spec:
        shutil.copytree(source, repo,
                        ignore=shutil.ignore_patterns(".venv", "venv", "__pycache__", ".pytest_cache"))
        git("init", "-q", "-b", "main", cwd=repo)
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", f"{fixture} fixture", cwd=repo)
    else:
        run(["git", "clone", "-q", "--no-hardlinks", str(source), str(repo)], check=True, timeout=300)
        git("remote", "remove", "origin", cwd=repo)
    for rel, text in spec["untracked"].items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    log(f"fixture {fixture}: prepared {repo} at {head(repo)}")
    return repo, plan


# -- the driver -----------------------------------------------------------------------------

def latest_run(task: dict[str, Any]) -> dict[str, Any]:
    return (task.get("runs") or [{}])[0]  # the bus lists runs newest first


def ended(task: dict[str, Any]) -> str:
    latest = latest_run(task)
    return f"ended {task['status']}/{latest.get('verdict')}: {(latest.get('error') or '')[:200]}"


class Driver:
    def __init__(self, args: argparse.Namespace, work: Path, bus: Bus) -> None:
        self.args, self.work, self.bus = args, work, bus
        self.aos = str(Path(sys.executable).parent / "aos")
        self.matrix_path = work / "matrix.json"
        self.matrix: dict[str, Any] = {"cells": []}
        if self.matrix_path.exists():
            self.matrix = json.loads(self.matrix_path.read_text())
        self.active: subprocess.Popen | None = None

    # environment
    def cli_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("AOS_", "BUS_")) and k != "NODE_EXTRA_CA_CERTS"}
        env.update(AOS_URL=self.bus.url, AOS_TOKEN=self.bus.token)
        return env

    def runner_env(self, harness: str) -> dict[str, str]:
        env = self.cli_env()  # NODE_EXTRA_CA_CERTS dropped: the known leak (`env -u NODE_EXTRA_CA_CERTS`)
        env.update(AOS_HOST=self.args.host, AOS_REPO_ROOT=str(self.work / "repos"),
                   AOS_OPENSHELL_PLATFORM=self.args.platform, AOS_OPENSHELL_IMAGE=self.args.image,
                   AOS_OPENSHELL_BUS_PORT=str(self.bus.port),
                   AOS_OPENSHELL_STATE_DIR=str(self.work / "openshell-state"),
                   AOS_HARNESS_TIMEOUT_SECONDS=str(self.args.harness_timeout))
        # AOS_OPENSHELL_FRESH_ACCEPTANCE is left to the runner's default (on since 2026-10-13), so the
        # matrix exercises the default; `--env AOS_OPENSHELL_FRESH_ACCEPTANCE=0` runs in-job acceptance.
        env.update({k: expand(v) for k, v in HARNESS_ENV[harness]["env"].items()})
        # --env KEY=VALUE: runner settings the table does not know (AOS_EVIDENCE_ROOT,
        # AOS_OPENSHELL_DB_BIND/HOST on linux); applied last, so they win.
        env.update(self.args.env)
        return env

    def aos_cli(self, *argv: str) -> subprocess.CompletedProcess:
        result = run([self.aos, *argv], env=self.cli_env(), cwd=ROOT, timeout=120)
        log(f"aos {' '.join(argv[:2])}: exit {result.returncode}"
            + (f" ({result.stderr.strip()[:200]})" if result.returncode else ""))
        return result

    # tasks and runner
    def add_task(self, fixture: str, harness: str, scenario: str, plan: dict[str, Any]) -> str:
        kind = SCENARIOS[scenario]["brief"]
        brief = self.work / "briefs" / f"{fixture}-{kind}.md"
        brief.write_text(plan[kind] + "\n")
        accepts = sum((["--accept", command] for command in plan["acceptance"]), [])
        title = f"release matrix {fixture} {harness} {scenario}"
        added = self.aos_cli("add", "--repo", fixture, "--title", title,
                             "--brief-file", str(brief), *accepts,
                             "--harness", HARNESS_ENV[harness]["task_harness"], "--host", self.args.host)
        if added.returncode != 0:
            raise RuntimeError(f"aos add refused: {added.stderr.strip()[:300]}")
        task_id = json.loads(added.stdout)["id"]
        if self.aos_cli("ready", task_id).returncode != 0:
            raise RuntimeError(f"aos ready {task_id} refused")
        log(f"task {task_id}: {fixture} {harness} {scenario} ready")
        return task_id

    def save_task(self, task_id: str) -> dict[str, Any]:
        task = self.bus.task(task_id)
        (self.work / "tasks" / f"{task_id}.json").write_text(json.dumps(task, indent=2))
        return task

    def launch(self, task_id: str, harness: str) -> tuple[subprocess.Popen, Path]:
        n = 1
        while (self.work / "logs" / f"{task_id}-r{n}.log").exists():
            n += 1
        path = self.work / "logs" / f"{task_id}-r{n}.log"
        with open(path, "w") as stream:
            proc = subprocess.Popen([self.aos, "run", "--backend", "openshell", task_id],
                                    env=self.runner_env(harness), cwd=ROOT, stdout=stream,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self.active = proc
        log(f"runner: aos run {task_id} pid {proc.pid}, log {path}")
        return proc, path

    def watch(self, proc: subprocess.Popen, task_id: str,
              on_poll: Callable[[dict[str, Any], float | None], None] | None = None,
              exit_deadline: Callable[[], float | None] | None = None) -> tuple[int | None, str | None]:
        """Poll the bus until the runner exits; one line per state change. Returns (exit code, failure)."""
        budget = self.args.harness_timeout + 2700
        started, running_since, last = time.monotonic(), None, None
        failure = None
        while True:
            try:
                task = self.bus.task(task_id)
                latest = latest_run(task)
                state = (task["status"], latest.get("id"), latest.get("status"), latest.get("verdict"))
                if state != last:
                    log(f"task {task_id}: {state[0]}; run {state[1]} {state[2]} verdict {state[3]}")
                    last = state
                if task["status"] == "running" and running_since is None:
                    running_since = time.monotonic()
                if on_poll is not None:
                    on_poll(task, None if running_since is None else time.monotonic() - running_since)
            except httpx.HTTPError as exc:
                log(f"bus poll failed: {type(exc).__name__}")
            code = proc.poll()
            if code is not None:
                log(f"runner pid {proc.pid} exited {code}")
                self.active = None
                return code, failure
            limit = exit_deadline() if exit_deadline else None
            if limit is not None and time.monotonic() > limit and failure is None:
                failure = f"runner still alive {EXIT_AFTER_CANCEL} s after the interruption; sent SIGTERM"
                log(failure)
                proc.terminate()
            if time.monotonic() - started > budget and failure is None:
                failure = f"runner exceeded {budget} s; sent SIGTERM"
                log(failure)
                proc.terminate()
            time.sleep(POLL)

    def sandboxes_left(self, task: dict[str, Any]) -> list[str] | None:
        listing = run(["openshell", "sandbox", "list"], timeout=60)
        if listing.returncode != 0:
            return None
        present = {line.split()[0] for line in listing.stdout.splitlines() if line.split()}
        ours = set()
        prefix = self.args.env.get("AOS_OPENSHELL_SANDBOX_PREFIX") or openshell_runner.bus_prefix(
            self.args.env.get("AOS_URL", self.bus.url))
        for run_ in task.get("runs") or []:
            try:
                name = openshell_runner.sandbox_name(run_["id"], prefix)
            except openshell_runner.AdapterError:
                continue
            ours |= {name, f"{name}-acc"}
        return sorted(present & ours)

    # scenarios
    def scenario(self, fixture: str, harness: str, scenario: str, repo: Path,
                 plan: dict[str, Any]) -> dict[str, Any]:
        cell: dict[str, Any] = {"fixture": fixture, "harness": harness, "scenario": scenario,
                                "expect": SCENARIOS[scenario]["expect"],
                                "notes": [], "logs": [], "exits": [],
                                "brief": plan[SCENARIOS[scenario]["brief"]]}
        started = time.monotonic()
        reasons: list[str] = []
        try:
            getattr(self, "_" + scenario.replace("-", "_"))(cell, fixture, harness, repo, plan, reasons)
        except (RuntimeError, httpx.HTTPError, subprocess.SubprocessError, OSError) as exc:
            reasons.append(f"driver: {type(exc).__name__}: {exc}")
        cell["duration_s"] = round(time.monotonic() - started)
        if cell.get("task"):
            task = self.save_task(cell["task"])
            self.describe(cell, task, repo)
            left = self.sandboxes_left(task)
            cell["sandbox_gone"] = None if left is None else not left
            if left is None:
                reasons.append("`openshell sandbox list` failed; deletion not confirmed")
            elif left:
                reasons.append("sandbox still present: " + ", ".join(left))
        cell["outcome"] = "FAIL" if reasons else "PASS"
        cell["reasons"] = reasons
        log(f"{fixture} {harness} {scenario}: {cell['outcome']}"
            + (f" ({'; '.join(reasons)})" if reasons else ""))
        return cell

    def describe(self, cell: dict[str, Any], task: dict[str, Any], repo: Path) -> None:
        runs = task.get("runs") or []
        latest = latest_run(task)
        cell.update(status=task["status"], runs=[r["id"] for r in reversed(runs)],
                    run_status=latest.get("status"), verdict=latest.get("verdict"),
                    result_commit=latest.get("result_commit"), branch=latest.get("branch"),
                    error=latest.get("error"), evidence=latest.get("evidence"))
        branch = latest.get("branch")
        tip = None
        if branch:
            tip = run(["git", "rev-parse", "-q", "--verify", f"refs/heads/{branch}"], cwd=repo)
        cell["branch_tip"] = tip.stdout.strip() if tip is not None and tip.returncode == 0 else None
        cell["first_failing_acceptance"] = first_failing(latest.get("acceptance_output"),
                                                         latest.get("error"))

    def run_once(self, cell: dict[str, Any], task_id: str, harness: str) -> int | None:
        proc, path = self.launch(task_id, harness)
        cell["logs"].append(str(path))
        code, failure = self.watch(proc, task_id)
        cell["exits"].append(code)
        if failure:
            cell["notes"].append(failure)
        return code

    def expect_pass(self, task_id: str, repo: Path, reasons: list[str]) -> None:
        task = self.bus.task(task_id)
        latest = latest_run(task)
        if task["status"] != "review" or latest.get("verdict") != "pass":
            reasons.append(ended(task))
            return
        commit, branch = latest.get("result_commit"), latest.get("branch")
        if not commit:
            reasons.append("no result commit")
        tip = run(["git", "rev-parse", "-q", "--verify", f"refs/heads/{branch}"], cwd=repo).stdout.strip()
        if branch != f"ao/{task_id}" or not tip or tip != commit:
            reasons.append(f"branch {branch} tip {tip[:12] or 'missing'} is not the result commit")

    def _success(self, cell, fixture, harness, repo, plan, reasons) -> None:
        cell["task"] = self.add_task(fixture, harness, "success", plan)
        self.run_once(cell, cell["task"], harness)
        self.expect_pass(cell["task"], repo, reasons)

    def _failed_verification(self, cell, fixture, harness, repo, plan, reasons) -> None:
        cell["task"] = task_id = self.add_task(fixture, harness, "failed-verification", plan)
        self.run_once(cell, task_id, harness)
        task = self.bus.task(task_id)
        if task["status"] != "failed" or latest_run(task).get("verdict") != "fail":
            reasons.append(ended(task))

    def interrupt(self, cell: dict[str, Any], task_id: str, harness: str, action: str) -> float:
        """Run; after --interrupt-after s of `running` apply `action` (cancel|term|kill).
        Returns the seconds from the action to the runner's exit."""
        acted: dict[str, float] = {}
        after = self.args.interrupt_after
        proc, path = self.launch(task_id, harness)
        cell["logs"].append(str(path))

        def on_poll(task: dict[str, Any], running_for: float | None) -> None:
            if acted or running_for is None or running_for < after or task["status"] != "running":
                return
            acted["at"] = time.monotonic()
            if action == "cancel":
                self.aos_cli("cancel", task_id)
            elif action == "term":
                log(f"runner: SIGTERM to pid {proc.pid}")
                proc.send_signal(signal.SIGTERM)
            else:
                log(f"runner: SIGKILL to pid {proc.pid}")
                proc.kill()

        code, failure = self.watch(proc, task_id, on_poll=on_poll,
                                   exit_deadline=lambda: acted["at"] + EXIT_AFTER_CANCEL if acted else None)
        cell["exits"].append(code)
        if failure:
            cell["notes"].append(failure)
        if not acted:
            raise RuntimeError(f"the run ended before {after} s of `running`; nothing interrupted")
        exit_after = time.monotonic() - acted["at"]  # upper bound, POLL granularity
        cell["notes"].append(f"{action} after {after} s of running: runner exited {code} "
                             f"within {exit_after:.0f} s")
        return exit_after

    def _interruption(self, cell, fixture, harness, repo, plan, reasons) -> None:
        cell["task"] = task_id = self.add_task(fixture, harness, "interruption", plan)
        exit_after = self.interrupt(cell, task_id, harness, "cancel")
        task = self.bus.task(task_id)
        if task["status"] != "cancelled":
            reasons.append(f"task ended {task['status']}, not cancelled")
        if exit_after > EXIT_AFTER_CANCEL + POLL:
            reasons.append(f"runner took {exit_after:.0f} s to exit after the cancel")

    def _retry(self, cell, fixture, harness, repo, plan, reasons) -> None:
        """Requeue the interrupted task as specified; a bus cancel is terminal (store TASK_ALLOWED), so a
        refusal is recorded and the retry then runs on a new task interrupted by SIGTERM (ends blocked)."""
        wanted = (fixture, harness, "interruption")
        previous = next((c for c in self.matrix["cells"]
                         if c.get("task") and (c["fixture"], c["harness"], c["scenario"]) == wanted), None)
        task_id = None
        fallback = "retried on a new task interrupted by SIGTERM instead"
        if previous is not None:
            requeue = self.aos_cli("requeue", previous["task"], "--ready")
            if requeue.returncode == 0:
                task_id = previous["task"]
            else:
                detail = " ".join((requeue.stderr or requeue.stdout).split())[:200]
                cell["notes"].append(f"`aos requeue {previous['task']} --ready` after the cancel refused: "
                                     f"{detail}; {fallback}")
        else:
            cell["notes"].append(f"no interruption task in this work dir; {fallback}")
        if task_id is None:
            cell["task"] = task_id = self.add_task(fixture, harness, "retry", plan)
            self.interrupt(cell, task_id, harness, "term")
            task = self.bus.task(task_id)
            if task["status"] != "blocked":
                reasons.append(f"after SIGTERM the task is {task['status']}, not blocked")
                return
            cell["notes"].append("SIGTERM run ended blocked: " + (latest_run(task).get("error") or "")[:120])
            if self.aos_cli("requeue", task_id, "--ready").returncode != 0:
                reasons.append(f"aos requeue {task_id} --ready refused")
                return
        cell["task"] = task_id
        self.run_once(cell, task_id, harness)
        runs = self.bus.task(task_id).get("runs") or []
        if len(runs) < 2:
            reasons.append(f"expected a second run, found {len(runs)}")
        self.expect_pass(task_id, repo, reasons)

    def _runner_kill(self, cell, fixture, harness, repo, plan, reasons) -> None:
        cell["task"] = task_id = self.add_task(fixture, harness, "runner-kill", plan)
        self.interrupt(cell, task_id, harness, "kill")
        wait = LEASE_SECONDS + SWEEP_SECONDS + 90
        deadline = time.monotonic() + wait
        while self.bus.task(task_id)["status"] != "blocked" and time.monotonic() < deadline:
            time.sleep(POLL)
        task = self.bus.task(task_id)
        log(f"task {task_id}: {task['status']} after the kill; run error {latest_run(task).get('error')}")
        if task["status"] != "blocked":
            reasons.append(f"task is {task['status']} {wait} s after the kill, not blocked")
        recon = run([sys.executable, "-m", "agent_os.openshell_runner", "reconcile"],
                    env=self.runner_env(harness), cwd=ROOT, timeout=600)
        path = self.work / "logs" / f"{task_id}-reconcile.log"
        path.write_text(recon.stdout + recon.stderr)
        cell["logs"].append(str(path))
        cell["notes"].append(f"reconcile exit {recon.returncode}: {' '.join(recon.stdout.split())[:120]}")
        log(f"reconcile: exit {recon.returncode} {' '.join((recon.stdout + recon.stderr).split())[:200]}")

    # bookkeeping
    def record_cell(self, cell: dict[str, Any]) -> None:
        """The newest attempt per (fixture, harness, scenario) is the cell; earlier ones are superseded."""
        key = (cell["fixture"], cell["harness"], cell["scenario"])
        old = [c for c in self.matrix["cells"] if (c["fixture"], c["harness"], c["scenario"]) == key]
        self.matrix.setdefault("superseded", []).extend(old)
        self.matrix["cells"] = [c for c in self.matrix["cells"] if c not in old] + [cell]
        self.save()

    def save(self) -> None:
        self.matrix_path.write_text(json.dumps(self.matrix, indent=2))


def first_failing(output: str | None, error: str | None) -> str | None:
    """The first acceptance command with a non-zero exit, with the last output line before its exit."""
    if not error or not error.startswith("acceptance failed: "):
        return None
    command = error[len("acceptance failed: "):]
    lines = [line for line in (output or "").splitlines() if line.strip()]
    detail = next((lines[i - 1] for i, line in enumerate(lines)
                   if i and re.match(r"\[exit [1-9]", line)), None)
    return command + (f" -> {detail.strip()[:100]}" if detail else "")


# -- environment and record -----------------------------------------------------------------

def environment(args: argparse.Namespace) -> dict[str, Any]:
    status = run(["openshell", "status"], timeout=60).stdout
    version = re.search(r"Version:\s*(\S+)", status)
    image_id = run(["docker", "image", "inspect", args.image, "--format", "{{.Id}}"]).stdout.strip()
    versions = run(["docker", "run", "--rm", args.image, "cat", "/etc/aos-harness/versions"], timeout=300)
    commit = run(["git", "rev-parse", "HEAD"], cwd=ROOT).stdout.strip()
    dirty = bool(run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT).stdout.strip())
    return {"date": datetime.now().astimezone().isoformat(timespec="seconds"), "host": args.host,
            "platform": args.platform, "image": args.image, "image_id": image_id,
            "image_versions": versions.stdout.strip(),
            "gateway_version": version.group(1) if version else None,
            "agent_os_commit": commit, "dirty": dirty, "harness_timeout": args.harness_timeout,
            "lease_seconds": LEASE_SECONDS, "sweep_seconds": SWEEP_SECONDS,
            "interrupt_after": args.interrupt_after}


def short(value: str | None, n: int = 12) -> str:
    return value[:n] if value else "-"


def render_record(matrices: list[tuple[Path, dict[str, Any]]]) -> str:
    env = next((m["environment"] for _, m in matrices if m.get("environment")), {})
    dirty = " (tracked changes present)" if env.get("dirty") else ""
    lines = [f"# Release matrix, {env.get('host', '?')} ({env.get('date', '?')[:10]})", "",
             "Written by `scripts/release_matrix.py` (see scripts/release_matrix.md). Every run is a real",
             "`aos run --backend openshell` against a disposable local bus; the work dirs keep the logs,",
             "task JSON and fixture repositories with their `ao/<task>` branches.", "",
             "## Environment", "", "| Item | Value |", "|---|---|",
             f"| Host | `{env.get('host')}`, platform `{env.get('platform')}` |",
             f"| Image | `{env.get('image')}` `{short(env.get('image_id'), 19)}` |",
             f"| Gateway | OpenShell {env.get('gateway_version')} |",
             f"| agent-os | `{short(env.get('agent_os_commit'))}`{dirty} |",
             f"| Bus | lease {env.get('lease_seconds')} s, sweep {env.get('sweep_seconds')} s; harness "
             f"timeout {env.get('harness_timeout')} s; interruptions after "
             f"{env.get('interrupt_after', 45)} s of `running`; fresh acceptance on |",
             "| Work dirs | " + ", ".join(f"`{p}`" for p, _ in matrices) + " |", "",
             "`/etc/aos-harness/versions` in the image:", "",
             "```", env.get("image_versions", ""), "```", ""]
    cells = [c for _, m in matrices for c in m.get("cells", [])]
    cells += [{**c, "superseded": True} for _, m in matrices for c in m.get("superseded", [])]
    order = list(HARNESS_ENV)
    for fixture in sorted({c["fixture"] for c in cells}):
        lines += [f"## {fixture}", "",
                  "| Harness | Scenario | Task (runs) | Duration | Status / verdict | Result commit "
                  "| Branch | Sandbox gone | Evidence | First failing acceptance | Result |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        notes: list[str] = []
        rows = sorted((c for c in cells if c["fixture"] == fixture),
                      key=lambda c: (order.index(c["harness"]), list(SCENARIOS).index(c["scenario"]),
                                     not c.get("superseded")))
        for c in rows:
            label = c["scenario"] + (" (earlier attempt)" if c.get("superseded") else "")
            branch = "-"
            if c.get("branch"):
                branch = f"`{c['branch']}`" + ("" if c.get("branch_tip") else " missing")
            gone = {True: "yes", False: "no", None: "unknown"}[c.get("sandbox_gone")]
            failing = (c.get("first_failing_acceptance") or "-").replace("|", "/")
            ev = c.get("evidence")
            if isinstance(ev, dict):  # the bus summary: counts, PASS/FAIL totals and flags, not the manifest
                totals = ""
                if isinstance(ev.get("pass_total"), int) and isinstance(ev.get("fail_total"), int):
                    totals = f", {ev['pass_total']} PASS / {ev['fail_total']} FAIL"
                ev = (f"{ev.get('file_count', 0)} files, {len(ev.get('screenshots') or [])} screenshots{totals}"
                      + ("; " + "; ".join(ev["flags"]) if ev.get("flags") else ""))
            lines.append(f"| {c['harness']} | {label} | {c.get('task', '-')} "
                         f"({', '.join(c.get('runs') or []) or '-'}) | {c.get('duration_s', '-')} s | "
                         f"{c.get('status', '-')} / {c.get('verdict') or '-'} | "
                         f"{short(c.get('result_commit'))} | {branch} | {gone} | "
                         f"{ev or '-'} | "
                         f"{failing} | **{c['outcome']}** |")
            notes += [f"- {c['harness']} {label} FAIL: {text}" for text in c.get("reasons", [])]
            notes += [f"- {c['harness']} {label}: {text}" for text in c.get("notes", [])]
            if c.get("superseded") or c["scenario"] == "failed-verification":
                notes.append(f"- {c['harness']} {label}: brief \"{c.get('brief', 'not recorded')}\"")
        lines += ["", *notes, ""] if notes else [""]
    return "\n".join(lines).rstrip() + "\n"


# -- entry point ----------------------------------------------------------------------------

def parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", choices=tuple(PLATFORMS), required=True)
    parser.add_argument("--platform", choices=tuple(PLATFORMS.values()), help="default: from --host")
    parser.add_argument("--fixture", nargs="+", choices=tuple(FIXTURES), default=["demo-app"])
    parser.add_argument("--harness", nargs="+", choices=tuple(HARNESS_ENV), default=list(HARNESS_ENV))
    parser.add_argument("--scenario", nargs="+", choices=tuple(SCENARIOS), default=list(DEFAULT_SCENARIOS))
    parser.add_argument("--only", nargs="+", metavar="HARNESS:SCENARIO", help="run only these cells")
    parser.add_argument("--work-dir", required=True, type=lambda p: Path(p).expanduser().resolve())
    parser.add_argument("--image", default="aos-harness:mvp")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                        help="extra runner environment (repeatable), e.g. AOS_EVIDENCE_ROOT=...")
    parser.add_argument("--clone-from", action="append", default=[], metavar="FIXTURE=PATH",
                        help="override a cloned fixture's source repository (repeatable)")
    parser.add_argument("--port", type=int, default=8012)
    parser.add_argument("--bind", default="127.0.0.1",
                        help="disposable bus bind/reachable host; Linux Docker bridge example: 172.17.0.1")
    parser.add_argument("--harness-timeout", type=int, default=1800)
    parser.add_argument("--interrupt-after", type=int, default=INTERRUPT_AFTER,
                        help="seconds of `running` before cancel/SIGTERM/SIGKILL (default 45)")
    parser.add_argument("--record", type=Path, help="markdown record path (default <work>/record.md)")
    parser.add_argument("--include", nargs="+", type=lambda p: Path(p).expanduser().resolve(), default=[],
                        help="other work dirs whose matrix.json joins the record")
    parser.add_argument("--record-only", action="store_true", help="only re-render the record")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    args.platform = args.platform or PLATFORMS[args.host]
    args.env = dict(item.split("=", 1) for item in args.env)
    for item in args.clone_from:
        name, _, path = item.partition("=")
        if name not in FIXTURES or "clone_from" not in FIXTURES[name] or not path:
            parser.error(f"--clone-from expects FIXTURE=PATH for a cloned fixture (got {item!r})")
        FIXTURES[name]["clone_from"] = Path(path).expanduser().resolve()
    return args


def cells_of(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    only = {tuple(item.split(":", 1)) for item in args.only or []}
    return [(f, h, s) for f in args.fixture for h in args.harness for s in args.scenario
            if not only or (h, s) in only]


def dry_run(args: argparse.Namespace) -> int:
    print(f"work dir {args.work_dir}; bus {args.bind}:{args.port} (BUS_TOKEN, BUS_DATA_DIR, "
          f"TASK_LEASE_SECONDS={LEASE_SECONDS}, TASK_SWEEP_INTERVAL_SECONDS={SWEEP_SECONDS})")
    print("runner environment (every harness): " + ", ".join(COMMON_RUNNER_VARS)
          + "; NODE_EXTRA_CA_CERTS removed")
    for harness in args.harness:
        print(f"  {harness}: task --harness {HARNESS_ENV[harness]['task_harness']}; "
              + ", ".join(HARNESS_ENV[harness]["env"]))
    for fixture, harness, scenario in cells_of(args):
        spec = FIXTURES[fixture]
        accept = spec.get("acceptance") or [f"(lines of {spec['acceptance_file']})"]
        print(f"task: {fixture} {harness} {scenario}: {SCENARIOS[scenario]['brief']} brief, accept "
              f"{' && '.join(accept)}; expect {SCENARIOS[scenario]['expect']}")
    return 0


def load_matrices(dirs: list[Path]) -> list[tuple[Path, dict[str, Any]]]:
    return [(d, json.loads((d / "matrix.json").read_text())) for d in dirs]


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    if args.dry_run:
        return dry_run(args)
    work = args.work_dir
    record = args.record or work / "record.md"
    if args.record_only:
        record.write_text(render_record(load_matrices([work, *args.include])))
        log(f"record: {record}")
        return 0
    missing = prerequisites(args)
    if missing:
        for line in missing:
            print(f"missing: {line}", file=sys.stderr, flush=True)
        return 2
    for sub in ("logs", "repos", "tasks", "briefs", "openshell-state"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    bus = Bus(work, args.port, args.bind)
    driver = Driver(args, work, bus)
    driver.matrix["environment"] = environment(args)
    driver.save()
    log(f"matrix: {len(cells_of(args))} cell(s) on {args.host} ({args.platform}), image {args.image}")
    bus.start()
    try:
        repos = {fixture: prepare_fixture(work, fixture) for fixture in args.fixture}
        for fixture, harness, scenario in cells_of(args):
            log(f"== {fixture} {harness} {scenario}")
            repo, plan = repos[fixture]
            driver.record_cell(driver.scenario(fixture, harness, scenario, repo, plan))
    except KeyboardInterrupt:
        if driver.active is not None and driver.active.poll() is None:
            log(f"interrupted: SIGTERM to runner pid {driver.active.pid}, waiting for its cleanup")
            driver.active.terminate()
            driver.active.wait(300)
        raise
    finally:
        bus.stop()
        record.write_text(render_record([(work, driver.matrix)] + load_matrices(args.include)))
        log(f"record: {record}; matrix: {driver.matrix_path}")
    return 0 if all(c["outcome"] == "PASS" for c in driver.matrix["cells"]) else 1


if __name__ == "__main__":
    sys.exit(main())
