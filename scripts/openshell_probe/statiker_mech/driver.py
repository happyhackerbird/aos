#!/usr/bin/env python3
"""Statiker-mechanism probe driver: one real `claude -p` run per mode inside an OpenShell sandbox,
with the statiker plugin (0.2.104) and the dispatch-guards plugin shipped in, under the aos-example
policy, measuring whether each mechanism a statiker run uses works there and whether the policy's
boundaries hold through it.

Probe tooling (docs/probes/2026-10-14-statiker-mechanisms.md), not the production adapter. It
imports the runner's own building blocks so that the sandbox create call, the main script's shape
and the base settings are the runner's; the deviations are the per-mode settings, the plugin
flags, the stream-json instrumentation and the cost and turn caps (README.md). No bus, no task, no
run file, no MCP configuration. Every `openshell` argv is logged to stderr. The Codex auth file is
staged 0600 in the private state directory, uploaded and unlinked at once; no secret value is ever
printed, logged or passed on a command line, and the secret scan (checks.py) counts occurrences only.

    uv run python scripts/openshell_probe/statiker_mech/driver.py prepare --base-tarball B --out J \\
        --plugins-out P [--project-skill]
    uv run python scripts/openshell_probe/statiker_mech/driver.py run --name stkm-plugin --mode plugin \\
        --image aos-harness:comms --policy Y --tarball J --plugins-tarball P \\
        --codex-auth ~/.aos/worker/codex-home/auth.json --state S --downloads D --evidence E
    uv run python scripts/openshell_probe/statiker_mech/driver.py cleanup --name stkm-plugin
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from agent_os import openshell_runner, pi
from agent_os.openshell_runner import (CLAUDE_SETTINGS, DEFAULT_ENV, SERVICES_ENV, SWEEP, compare_effective,
                                       deadline_lines, harness_command, wait_job_lines)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import checks  # noqa: E402  (the kit's own module, beside this file)

OPENSHELL = os.environ.get("OPENSHELL_BIN", "openshell")
REPO_ROOT = HERE.parents[2]
GATEWAY = "https://localhost:17670"
NAME_PREFIX = "stkm-"
MODES = ("plugin", "settings-hooks", "control", "stopguard")
# settings-hooks runs the plugin prompt unchanged (the brief: "the same prompt"); control runs it
# with one more Skill call in R1, to an existing non-plugin skill ("verify"): control run 1 (plugin
# prompt) got only "Unknown skill" answers, so the Skill tool's permission was never reached.
PROMPT_FILES = {"plugin": "plugin.md", "settings-hooks": "plugin.md", "control": "control.md",
                "stopguard": "stopguard.md"}
STATIKER_SRC = Path("~/.claude/plugins/cache/statiker/statiker/0.2.104").expanduser()
DISPATCH_SRC = Path("~/code/dispatch-guards/plugin").expanduser()
STEP1D = ".clippy/runs/2026-10-05-repo-policy-step1d.md"
PROBE_TRACKER = ".clippy/runs/2026-10-14-probe-statiker.md"
PROJECT_SKILL = ".claude/skills/statiker/SKILL.md"
PLUGIN_FLAGS = "--plugin-dir /sandbox/plugins/statiker --plugin-dir /sandbox/plugins/dispatch-guards"
STOP_GUARD = "python3 /sandbox/plugins/statiker/hooks/statiker_stop_guard.py"
FIXTURE_COMMIT = "probe: statiker fixtures (step1d tracker copy, stop-guard tracker)"

# The Stop guard fires when (statiker_stop_guard.py): the first `^Status:` line reads in-progress
# (:96, :167-171); no `Mode: attended` line (:97, :173-177; SKILL.md:352, absent = unattended); the
# last `- A<n> [TAG]` line carries BIT, ZERO-DELTA or VOID (:94, :99, :127-135, :179-191); and the
# [PENDING] set is not solely one operator-authority entry (:95, :138-157, :193-199). The tracker
# glob is `<cwd>/.clippy/runs/*-statiker.md` (:92, :105-109). This fixture meets all four.
PROBE_TRACKER_TEXT = """# Run: statiker mechanism probe fixture (Stop guard)
Status: in-progress
Phase: investigate-design
Skill: statiker 0.2.104

INTENT: a fixture for the statiker Stop guard inside an OpenShell sandbox, not a real run. Status in-progress, no Mode line (unattended), the last A-line terminal [BIT], no [PENDING] entry.

## Cycle 1

- A1 [BIT] fixture round: the Stop guard reads this as the terminal A-line.
"""


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


# -- openshell ------------------------------------------------------------------------------

def openshell(*args: str, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess:
    print("+ openshell " + shlex.join(args), file=sys.stderr, flush=True)
    result = subprocess.run([OPENSHELL, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            errors="replace", timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"openshell {' '.join(args[:2])} exited {result.returncode}: {result.stderr.strip()[-400:]}")
    return result


def sandbox_exec(name: str, script: str, *, timeout: int = 60, check: bool = False) -> subprocess.CompletedProcess:
    return openshell("sandbox", "exec", "-n", name, "--no-tty", "--timeout", str(timeout), "--", "bash", "-lc", script,
                     check=check, timeout=timeout + 60)


def wait_for_file(name: str, path: str, timeout: int, interval: float = 2) -> float:
    started = time.monotonic()
    while True:
        if sandbox_exec(name, f"test -e {shlex.quote(path)}", timeout=20).returncode == 0:
            return time.monotonic() - started
        if time.monotonic() - started > timeout:
            raise TimeoutError(f"{path} did not appear within {timeout} s")
        time.sleep(interval)


def upload(name: str, local: Path, dest: str) -> None:
    openshell("sandbox", "upload", name, str(local), dest)


def listed(listing: str, name: str) -> bool:
    return any(line.split() and line.split()[0] == name for line in listing.splitlines())


def gateway_check() -> str:
    """Only the local gateway: `openshell status` must name it and be connected before a create."""
    out = openshell("status", check=False, timeout=60).stdout
    if f"Server: {GATEWAY}" not in out or "Status: Connected" not in out:
        raise RuntimeError(f"openshell status does not show a connected {GATEWAY}:\n{out}")
    return out


# -- what the sandbox receives --------------------------------------------------------------

def dispatch_hooks_as_settings() -> dict:
    """dispatch-guards' hooks.json with ${CLAUDE_PLUGIN_ROOT} spelled out (the settings-hooks mode)."""
    text = (DISPATCH_SRC / "hooks" / "hooks.json").read_text()
    return json.loads(text.replace("${CLAUDE_PLUGIN_ROOT}", "/sandbox/plugins/dispatch-guards"))["hooks"]


def settings_for(mode: str) -> dict:
    """The runner's CLAUDE_SETTINGS; control keeps it unchanged, every other mode allows Skill(*)
    and turns hooks on (plugin: no hooks key, the plugins' own hooks.json must register them)."""
    settings = copy.deepcopy(CLAUDE_SETTINGS)
    if mode == "control":
        return settings
    settings["permissions"]["allow"].append("Skill(*)")
    settings["disableAllHooks"] = False
    if mode == "settings-hooks":
        settings["hooks"] = dispatch_hooks_as_settings()
    elif mode == "stopguard":
        settings["hooks"] = {"Stop": [{"hooks": [{"type": "command", "command": STOP_GUARD, "timeout": 10}]}]}
    return settings


def claude_command(mode: str, model: str, budget_usd: float, max_turns: int) -> str:
    """The runner's Claude command; plugin and stopguard add the two --plugin-dir flags after
    --settings; then the instrumentation: stream-json with hook events, and the caps."""
    base = harness_command("claude", model)
    marker = "--settings /sandbox/claude-settings.json"
    assert marker in base
    if mode in ("plugin", "stopguard"):
        base = base.replace(marker, f"{marker} {PLUGIN_FLAGS}", 1)
    command = base.replace("--output-format json", "--output-format stream-json --verbose --include-hook-events", 1)
    return command + f" --max-budget-usd {budget_usd:g} --max-turns {max_turns}"


def probe_main_script(command: str, harness_timeout: int, linger: int) -> str:
    """openshell_runner.main_script line for line, with the probe's command in the harness slot."""
    *definition, invocation = deadline_lines(command, "/dev/null", harness_timeout)
    return "\n".join([
        "set -u",
        f"mkdir -p /tmp/claude-home {pi.AGENT_DIR} /tmp/pi-home /sandbox/codex-home /sandbox/incoming {pi.SESSION_DIR}",
        f"export CLAUDE_CONFIG_DIR=/tmp/claude-home PI_CODING_AGENT_DIR={pi.AGENT_DIR} CODEX_HOME=/sandbox/codex-home",
        *wait_job_lines(linger),
        SERVICES_ENV,
        "mkdir -p /sandbox/repo && tar xzf /sandbox/job.tgz -C /sandbox/repo",
        "cd /sandbox/repo",
        *definition,
        "date +%s%N > /sandbox/harness.started",
        invocation,
        "rc=$?; " + SWEEP + "; echo $rc > /sandbox/harness.rc",
        f"sleep {linger}",
    ]) + "\n"


def assert_runner_shape(model: str, harness_timeout: int, linger: int) -> None:
    """With the runner's own command in the slot, the probe's main script IS the runner's."""
    ours = probe_main_script(harness_command("claude", model), harness_timeout, linger)
    theirs = openshell_runner.main_script("claude", model, harness_timeout=harness_timeout, linger=linger)
    if ours != theirs:
        raise RuntimeError("the runner's main_script changed shape; update probe_main_script")


def prompt_for(mode: str, codeword: str) -> str:
    return (HERE / "prompts" / PROMPT_FILES[mode]).read_text().replace("@CODEWORD@", codeword)


# -- in-sandbox reads -----------------------------------------------------------------------

def blocks_script(blocks: list[tuple[str, str]]) -> str:
    return "\n".join(f"echo '== {label} =='; ( {body} ) 2>&1" for label, body in blocks)


PRE_READS = [
    ("pre-env", 'echo "uid=$(id -u) HOME=$HOME CODEX_HOME=${CODEX_HOME-unset} CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR-unset}"; '
                'echo "exec env CLAUDE_CODE_OAUTH_TOKEN entries: $(env | grep -c "^CLAUDE_CODE_OAUTH_TOKEN=")"; '
                'echo "exec env placeholder prefix: $(printenv CLAUDE_CODE_OAUTH_TOKEN | cut -c1-12)"'),
    ("pre-plugins", "ls -la /sandbox/plugins /sandbox/plugins/statiker /sandbox/plugins/dispatch-guards "
                    "/sandbox/plugins/dispatch-guards/hooks | head -n 80"),
    ("pre-codex-home", "stat -c '%a %U %n' /sandbox/codex-home /sandbox/codex-home/auth.json"),
]
EARLY_READS = [
    ("early-repo", "cd /sandbox/repo && { test -d .git && echo 'dot-git is a directory (full clone)'; git rev-parse HEAD; "
                   "git log --oneline -3; git ls-files .clippy .claude/skills/statiker; ls -la .git/worktrees; }"),
    # The harness processes (this exec's own shell excluded): their command line head, and from their
    # environment the four paths plus whether a CLAUDE_CODE_OAUTH_TOKEN entry exists (a count, no value).
    ("early-main-env", "for p in $(pgrep -u \"$(id -u)\" -f 'claude[.]exe|^claude -p|^timeout'); do "
                       "[ \"$p\" = \"$$\" ] && continue; echo \"pid $p: $(tr '\\0' ' ' < /proc/$p/cmdline | cut -c1-70)\"; "
                       "tr '\\0' '\\n' < /proc/$p/environ | grep -E '^(HOME|CODEX_HOME|CLAUDE_CONFIG_DIR|PATH)='; "
                       "echo \"  token entries: $(tr '\\0' '\\n' < /proc/$p/environ | grep -c '^CLAUDE_CODE_OAUTH_TOKEN=')\"; "
                       "done 2>&1 | head -n 40"),
]
SUB_FILES = " ".join(f"/sandbox/sub-{m}.txt /sandbox/sub-{m}-curl.txt" for m in ("opus", "haiku", "sonnet"))
POST_READS = [
    ("subagent-files", f'for f in {SUB_FILES}; do echo "-- $f"; cat "$f"; done'),
    ("filter-artifact", 'if test -f /tmp/stk-filter.md; then echo "exists $(wc -c < /tmp/stk-filter.md) bytes"; '
                        'head -n 5 /tmp/stk-filter.md; else echo absent; fi'),
    # HOME inside an OpenShell sandbox is /sandbox (measured on stkm-plugin run 1), so statiker's
    # ~/.local/state/statiker and the dispatch-guards fire log resolve under $HOME; /home/sandbox
    # (the image's HOME, writable by the policy) is listed beside it.
    ("statiker-state", 'echo "HOME=$HOME"; for d in "$HOME/.local/state/statiker" /home/sandbox/.local/state/statiker; do '
                       'echo "-- $d"; ls -laR "$d" | head -n 40; done'),
    ("firelog", 'for d in "$HOME/.local/share/claude" /home/sandbox/.local/share/claude; do echo "-- $d"; ls -la "$d"; '
                'for f in "$d"/*.jsonl; do [ -f "$f" ] || continue; echo "-- $f lines=$(wc -l < "$f")"; '
                'tail -n 40 "$f"; done; done'),
    ("worktrees", "cd /sandbox/repo && ls -la .git/worktrees; git worktree list"),
    ("usr-paths", "ls -ld /usr/local/stk-wt /usr/stk-mark"),
    ("codex-hello", "cat /sandbox/repo/codex-hello.txt"),
    ("stopguard-file", "cat /sandbox/repo/stopguard-ran.txt"),
    ("repo-after", "cd /sandbox/repo && git log --oneline -5 && git status --porcelain"),
    ("codex-auth", "sha256sum /sandbox/codex-home/auth.json | cut -c1-64; stat -c '%a' /sandbox/codex-home/auth.json; "
                   "ls -la /sandbox/codex-home"),
    ("claude-home", "find /tmp/claude-home -maxdepth 4 | head -n 60"),
    ("tmp", "ls -la /tmp"),
]
# Counts only (P1b's Q6 form): the Codex token values are read from the auth file inside the
# sandbox and never leave it; every count is paired with a control string known to be present.
IN_SANDBOX_SCAN = r"""python3 - <<'PY'
import json, os, subprocess
AUTH = "/sandbox/codex-home/auth.json"
tokens = json.load(open(AUTH)).get("tokens") or {}
values = {k: str(v).encode() for k, v in tokens.items() if k in ("refresh_token", "access_token", "id_token") and v}
def count(root, needle):
    n = 0
    for d, dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            if p == AUTH or os.path.islink(p):
                continue
            try:
                with open(p, "rb") as fh:
                    n += needle in fh.read()
            except OSError:
                pass
    return n
for root, control in (("/sandbox", b"aos-policy"), ("/tmp", b'"type"'), ("/home/sandbox", b"PS1")):
    found = {k: count(root, v) for k, v in values.items()}
    print(f"files under {root} holding a codex token: {found}; control {control.decode()!r}: {count(root, control)}")
objs = subprocess.run(["git", "-C", "/sandbox/repo", "cat-file", "--batch-all-objects", "--batch"], capture_output=True).stdout
in_objs = {k: objs.count(v) for k, v in values.items()}
print(f"codex tokens in the decompressed git objects: {in_objs}; control 'statiker fixtures': {objs.count(b'statiker fixtures')}")
PY"""


# -- prepare --------------------------------------------------------------------------------

def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} exited {result.returncode}: {result.stderr.strip()[-300:]}")
    return result.stdout


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(args: argparse.Namespace) -> int:
    """Rebuild the provisioned job tarball with the two tracker fixtures committed, and build the
    plugins tarball. The Stop guard is run once on this host against the rebuilt tree as a check
    that the fixture makes it fire."""
    summary: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="stkm-prepare-") as scratch:
        root = Path(scratch) / "job"
        root.mkdir()
        with tarfile.open(args.base_tarball, "r:gz") as tar:
            tar.extractall(root, filter="tar")
        summary["base_commit"] = git("rev-parse", "HEAD", cwd=root).strip()
        source = REPO_ROOT / STEP1D
        target = root / STEP1D
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if sha256(source) != sha256(target):
            raise RuntimeError("step1d copy differs from its source")
        (root / PROBE_TRACKER).write_text(PROBE_TRACKER_TEXT)
        paths = [STEP1D, PROBE_TRACKER]
        if args.project_skill:
            skill = root / PROJECT_SKILL
            skill.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(STATIKER_SRC / "skills" / "statiker" / "SKILL.md", skill)
            paths.append(PROJECT_SKILL)
        guard = subprocess.run([sys.executable, str(STATIKER_SRC / "hooks" / "statiker_stop_guard.py")],
                               input=json.dumps({"hook_event_name": "Stop", "cwd": str(root)}),
                               capture_output=True, text=True, timeout=30)
        summary["stop_guard_on_host"] = {"rc": guard.returncode, "stdout": guard.stdout.replace(str(root), "<job>"),
                                         "stderr": guard.stderr.replace(str(root), "<job>")}
        git("add", "--", *paths, cwd=root)
        git("-c", "user.name=statiker-probe", "-c", "user.email=probe@localhost.invalid", "commit", "-q", "-m",
            FIXTURE_COMMIT, cwd=root)
        summary["fixture_commit"] = git("rev-parse", "HEAD", cwd=root).strip()
        summary["committed"] = git("ls-tree", "-r", "--name-only", "HEAD", "--", *paths, cwd=root).split()
        summary["status"] = git("status", "--porcelain", cwd=root)
        with tarfile.open(args.out, "w:gz") as tar:
            for child in sorted(root.iterdir()):
                tar.add(child, arcname=child.name)
    summary["tarball"] = {"path": args.out, "bytes": os.path.getsize(args.out), "sha256": sha256(Path(args.out))}

    def no_pycache(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        return None if "__pycache__" in Path(info.name).parts else info

    with tarfile.open(args.plugins_out, "w:gz") as tar:
        tar.add(STATIKER_SRC, arcname="statiker")                          # the whole directory
        tar.add(DISPATCH_SRC, arcname="dispatch-guards", filter=no_pycache)  # without __pycache__
    with tarfile.open(args.plugins_out, "r:gz") as tar:
        names = tar.getnames()
    summary["plugins"] = {"path": args.plugins_out, "bytes": os.path.getsize(args.plugins_out),
                          "sha256": sha256(Path(args.plugins_out)), "members": len(names),
                          "statiker_version": json.loads((STATIKER_SRC / ".claude-plugin" / "plugin.json").read_text())["version"],
                          "dispatch_guards_version": json.loads((DISPATCH_SRC / ".claude-plugin" / "plugin.json").read_text())["version"],
                          "pycache_members": sum("__pycache__" in n for n in names)}
    print(json.dumps(summary, indent=2))
    return 0


# -- the run --------------------------------------------------------------------------------

def private_copy(source: Path, target: Path) -> None:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, source.read_bytes())
    finally:
        os.close(fd)


def redact(text: str, values: dict[str, str]) -> str:
    for label, value in values.items():
        if value:
            text = text.replace(value, f"<{label}>")
    return text


def run(args: argparse.Namespace) -> int:
    if not args.name.startswith(NAME_PREFIX):
        raise RuntimeError(f"sandbox names start with {NAME_PREFIX}")
    evidence = Path(args.evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    downloads = Path(args.downloads) / args.name
    downloads.mkdir(parents=True, exist_ok=True)
    values = checks.load_secrets(args.claude_token_file, args.codex_auth)
    host_auth_digest = sha256(Path(args.codex_auth).expanduser())
    timeline: list[dict] = []
    summary: dict[str, object] = {"name": args.name, "mode": args.mode, "model": args.model,
                                  "budget_usd": args.budget_usd, "max_turns": args.max_turns}

    def mark(event: str, **fields: object) -> None:
        entry = {"t": round(time.time(), 3), "event": event, **fields}
        timeline.append(entry)
        log(json.dumps(entry))

    status = gateway_check()
    (evidence / "gateway-status.txt").write_text(status)
    before = openshell("sandbox", "list", check=False, timeout=60)
    (evidence / "sandboxes-before.txt").write_text(before.stdout + before.stderr)
    if listed(before.stdout, args.name):
        raise RuntimeError(f"{args.name} already exists; delete it first (cleanup) if it is ours")

    codeword = f"CODEWORD-{secrets.token_hex(3)}"
    settings = settings_for(args.mode)
    prompt = prompt_for(args.mode, codeword)
    command = claude_command(args.mode, args.model, args.budget_usd, args.max_turns)
    linger = args.harness_timeout + 900 + openshell_runner.LINGER_MARGIN
    assert_runner_shape(args.model, args.harness_timeout, linger)
    script = probe_main_script(command, args.harness_timeout, linger)
    (evidence / "main-script.sh").write_text(script)
    (evidence / "claude-settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    (evidence / "prompt.txt").write_text(prompt)
    summary["codeword"] = codeword
    exec_text: list[str] = []

    openshell("sandbox", "create", "--name", args.name, "--from", args.image, "--policy", args.policy,
              "--provider", args.provider, *sum((["--env", e] for e in DEFAULT_ENV), []),
              "--cpu", args.cpu, "--memory", args.memory, "--detach", "--no-tty", "--", "bash", "-lc", script)
    mark("sandbox.created", name=args.name, image=args.image)
    try:
        wait_for_file(args.name, "/sandbox/incoming", 180)
        effective = openshell("policy", "get", args.name, "--full", timeout=60).stdout
        (evidence / "effective-policy.yaml").write_text(effective)
        problems = compare_effective(Path(args.policy).read_text(), effective, strict=False)
        summary["effective_policy_problems"] = problems
        mark("policy.effective", problems=problems)

        # The plugins: the tarball up, extracted by an exec before the job is released.
        upload(args.name, Path(args.plugins_tarball), "/sandbox/plugins.tgz")
        sandbox_exec(args.name, "mkdir -p /sandbox/plugins && tar xzf /sandbox/plugins.tgz -C /sandbox/plugins "
                                "&& rm -f /sandbox/plugins.tgz", check=True, timeout=120)
        mark("plugins.extracted")

        # Codex auth: a 0600 private copy, uploaded, unlinked whether or not the upload succeeds.
        auth_local = state / "auth.json"
        private_copy(Path(args.codex_auth).expanduser(), auth_local)
        try:
            upload(args.name, auth_local, "/sandbox/codex-home/auth.json")
        finally:
            auth_local.unlink(missing_ok=True)
        sandbox_exec(args.name, "chmod 600 /sandbox/codex-home/auth.json", check=True)
        mark("codex_auth.uploaded")

        for filename, text in (("claude-settings.json", json.dumps(settings, indent=2)), ("prompt.txt", prompt)):
            local = state / filename
            local.write_text(text)
            upload(args.name, local, f"/sandbox/{filename}")
            local.unlink()
        mark("settings_prompt.uploaded")

        pre = sandbox_exec(args.name, blocks_script(PRE_READS), timeout=60)
        exec_text.append(pre.stdout + pre.stderr)

        size = os.path.getsize(args.tarball)
        if size > openshell_runner.UPLOAD_PART_BYTES:
            raise RuntimeError(f"job tarball {size} bytes exceeds one upload part; use the runner's upload_job")
        upload(args.name, Path(args.tarball), "/sandbox/incoming/job.tgz")
        sandbox_exec(args.name, "mv /sandbox/incoming/job.tgz /sandbox/job.tgz", check=True)
        mark("job.released", bytes=size)
        wait_for_file(args.name, "/sandbox/harness.started", 120)
        early = sandbox_exec(args.name, blocks_script(EARLY_READS), timeout=60)
        exec_text.append(early.stdout + early.stderr)
        mark("harness.started")

        elapsed = wait_for_file(args.name, "/sandbox/harness.rc", args.harness_timeout + 300, interval=5)
        summary["harness_seconds"] = round(elapsed, 1)
        mark("harness.finished", seconds=round(elapsed, 1))

        post = sandbox_exec(args.name, blocks_script(POST_READS), timeout=120)
        exec_text.append(post.stdout + post.stderr)
        scan_in = sandbox_exec(args.name, IN_SANDBOX_SCAN, timeout=300)
        exec_text.append("== in-sandbox-scan ==\n" + scan_in.stdout + scan_in.stderr)
        joined = redact("\n".join(exec_text), values)
        (downloads / "exec-reads.txt").write_text(joined)
        digest_line = checks.exec_blocks(joined).get("codex-auth", "").strip().splitlines()[:1]
        summary["codex_auth_unchanged"] = bool(digest_line) and digest_line[0] == host_auth_digest
        if digest_line and digest_line[0] != host_auth_digest:
            # A rotated lineage lives only in the sandbox now: keep a private copy in the state dir
            # (never under ~/.aos) and say so; the operator decides what to do with it.
            rotated = state / "auth.json.rotated"
            got = openshell("sandbox", "download", args.name, "/sandbox/codex-home/auth.json", str(rotated),
                            check=False, timeout=120)
            if rotated.exists():
                os.chmod(rotated, 0o600)
            summary["codex_auth_rotated_copy"] = str(rotated) if got.returncode == 0 else f"download failed rc={got.returncode}"
        mark("exec.reads_done", codex_auth_unchanged=summary["codex_auth_unchanged"])

        for remote in ("/sandbox/harness.out", "/sandbox/harness.err", "/sandbox/harness.rc",
                       "/sandbox/repo/probe-report.md"):
            local = downloads / os.path.basename(remote)
            fetched = openshell("sandbox", "download", args.name, remote, str(local), check=False, timeout=300)
            if fetched.returncode != 0 or not local.exists():
                mark("download.failed", path=remote, rc=fetched.returncode, stderr=redact(fetched.stderr[-300:], values))
        # Codex's own output files under /tmp come back by exec (a /tmp download came back empty in P1b).
        for label in ("r6a", "r6a2", "r6b", "r6c", "r6d"):
            for ext in ("out", "err"):
                got = sandbox_exec(args.name, f"cat /tmp/codex-{label}.{ext}", timeout=60)
                if got.returncode == 0:
                    (downloads / f"codex-{label}.{ext}.txt").write_text(got.stdout)
        if not (downloads / "probe-report.md").exists() or not (downloads / "probe-report.md").stat().st_size:
            got = sandbox_exec(args.name, "cat /sandbox/repo/probe-report.md", timeout=60)
            if got.returncode == 0:
                (downloads / "probe-report.md").write_text(got.stdout)
        mark("downloads.done", files=sorted(p.name for p in downloads.iterdir()))

        gateway_log = openshell("logs", args.name, "-n", "4000", check=False, timeout=120).stdout
        (evidence / "gateway-log.txt").write_text(redact(gateway_log, values))
        summary["gateway_denied_lines"] = sum("DENIED" in line for line in gateway_log.splitlines())
        mark("gateway.log_saved", lines=len(gateway_log.splitlines()))
        summary["harness_rc"] = (downloads / "harness.rc").read_text().strip() if (downloads / "harness.rc").exists() else "?"
    finally:
        if args.keep:
            log(f"sandbox {args.name} kept (--keep)")
        else:
            openshell("sandbox", "delete", args.name, check=False, timeout=120)
            for _ in range(30):
                listing = openshell("sandbox", "list", check=False, timeout=60)
                if not listed(listing.stdout, args.name):
                    break
                time.sleep(2)
            (evidence / "sandboxes-after.txt").write_text(listing.stdout + listing.stderr)
            mark("sandbox.deleted", gone=not listed(listing.stdout, args.name))
        summary["timeline"] = timeline
        (evidence / "run-summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    _, text = checks.analyze(args.mode, downloads, evidence, codeword, values)
    for name in ("probe-report.md", "exec-reads.txt"):
        if (downloads / name).exists():
            shutil.copyfile(downloads / name, evidence / name)
    # The evidence directory itself, scanned before anything in it is copied into the record.
    print(text + "\n" + checks.append_evidence_scan(evidence, values))
    print(json.dumps({k: v for k, v in summary.items() if k != "timeline"}, indent=2))
    return 0


def cleanup(args: argparse.Namespace) -> int:
    if not args.name.startswith(NAME_PREFIX):
        raise RuntimeError(f"only {NAME_PREFIX}* sandboxes are this kit's to delete")
    openshell("sandbox", "delete", args.name, check=False, timeout=120)
    print(openshell("sandbox", "list", check=False, timeout=60).stdout)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Statiker mechanisms inside an OpenShell sandbox.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="rebuild the job tarball with the tracker fixtures; build the plugins tarball")
    p.add_argument("--base-tarball", required=True, help="run_job.py provision output")
    p.add_argument("--out", required=True, help="the rebuilt job tarball")
    p.add_argument("--plugins-out", required=True)
    p.add_argument("--project-skill", action="store_true", help="also commit .claude/skills/statiker/SKILL.md (settings-hooks)")
    p.set_defaults(func=prepare)
    p = sub.add_parser("run")
    p.add_argument("--name", required=True, help="sandbox name, stkm-*")
    p.add_argument("--mode", choices=MODES, required=True)
    p.add_argument("--image", required=True)
    p.add_argument("--policy", required=True, help="rendered OpenShell policy YAML")
    p.add_argument("--tarball", required=True, help="job tarball from `prepare`")
    p.add_argument("--plugins-tarball", required=True)
    p.add_argument("--codex-auth", required=True, help="the worker's Codex auth.json (uploaded, never printed)")
    p.add_argument("--claude-token-file", default="~/.aos/worker/claude-oauth-token",
                   help="the worker's Claude token, read only for the scan's counts")
    p.add_argument("--state", required=True, help="private scratch dir for files staged for upload")
    p.add_argument("--downloads", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--provider", default="aos-claude-oauth")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--budget-usd", type=float, default=8)
    p.add_argument("--max-turns", type=int, default=120)
    p.add_argument("--harness-timeout", type=int, default=2700)
    p.add_argument("--cpu", default="2")
    p.add_argument("--memory", default="2Gi")
    p.add_argument("--keep", action="store_true", help="do not delete the sandbox (debugging only)")
    p.set_defaults(func=run)
    p = sub.add_parser("cleanup")
    p.add_argument("--name", required=True)
    p.set_defaults(func=cleanup)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, TimeoutError, OSError) as exc:
        print(f"driver: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
