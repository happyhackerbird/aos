#!/usr/bin/env python3
"""Codex-mechanism probe driver: one real `codex exec` run per mode inside an OpenShell sandbox,
measuring whether the mechanisms a statiker run would use on Codex (skills from the user, home and
repository locations, hooks, subagents with a custom role and its own model, worktrees, the state
directory) work there and whether the aos-example policy's boundaries hold through each of them.

Probe tooling (docs/probes/2026-10-14-codex-mechanisms.md), not the production adapter. Built
beside the statiker_mech kit (scripts/openshell_probe/statiker_mech/), whose helpers it copies. It
imports the runner's own building blocks so that the sandbox create call, the main script's shape
and the Codex command are the runner's; the deviations are the per-mode feature flags and the
hook-trust bypass (README.md). No bus, no task, no run file. Every `openshell` argv is logged to
stderr. The Codex auth file is staged 0600 in the private state directory, uploaded and unlinked
at once; no secret value is ever printed, logged or passed on a command line, and the secret scan
(checks.py) counts occurrences only.

    uv run python scripts/openshell_probe/codex_mech/driver.py prepare --base-tarball B --out J --plugins-out P
    uv run python scripts/openshell_probe/codex_mech/driver.py run --name cxm-mech --mode mech \\
        --image aos-harness:comms --policy Y --tarball J --plugins-tarball P \\
        --codex-auth ~/.aos/worker/codex-home/auth.json --state S --downloads D --evidence E
    uv run python scripts/openshell_probe/codex_mech/driver.py cleanup --name cxm-mech
"""
from __future__ import annotations

import argparse
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
from agent_os.openshell_runner import DEFAULT_ENV, SERVICES_ENV, SWEEP, compare_effective, deadline_lines, harness_command, wait_job_lines

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import checks  # noqa: E402  (the kit's own module, beside this file)

OPENSHELL = os.environ.get("OPENSHELL_BIN", "openshell")
GATEWAY = "https://localhost:17670"
NAME_PREFIX = "cxm-"
MODES = ("mech", "control", "notrust", "agents-off")
PROMPT_FILES = {"mech": "mech.md", "control": "control.md", "notrust": "notrust.md", "agents-off": "control.md"}
AGENTS_OFF = "-c agents.enabled=false"
FIXTURES = HERE / "fixtures"
PLUGIN_DIR = "/sandbox/plugins/codex-probe"
REPO_SKILL = ".agents/skills/repo-skill/SKILL.md"
FIXTURE_COMMIT = "probe: codex fixtures (repository skill)"
# The runner's flags (hooks and plugins off, subagents on since the subagents-default-on change), the control's
# (everything off: agents.enabled=false is the switch, finding 2) and what the mech modes put in their place. Every
# mode builds the command it built at b10d932; control and agents-off are one command, runs 3 and 5 predate that.
RUNNER_FLAGS = "-c features.hooks=false -c features.multi_agent=true -c agents.enabled=true -c features.plugins=false"
OFF_FLAGS = "-c features.hooks=false -c features.multi_agent=false -c agents.enabled=false -c features.plugins=false"
ON_FLAGS = "-c features.hooks=true -c features.multi_agent=true -c features.plugins=true"
TRUST_FLAG = "--dangerously-bypass-hook-trust"
WORKTREE_MODEL = "gpt-5.6-terra"
# Where the fixtures land inside the sandbox (HOME is /sandbox; CODEX_HOME is /sandbox/codex-home).
# Skills: the documented USER location $HOME/.agents/skills, the undocumented $CODEX_HOME/skills the
# operator's own home carries, and the repository's .agents/skills (committed by `prepare`).
INSTALL = (
    f"mkdir -p /sandbox/.agents/skills /sandbox/codex-home/skills /sandbox/codex-home/agents && "
    f"cp -r {PLUGIN_DIR}/skills/user-skill /sandbox/.agents/skills/ && "
    f"cp -r {PLUGIN_DIR}/skills/home-skill /sandbox/codex-home/skills/ && "
    f"cp {PLUGIN_DIR}/hooks.json /sandbox/codex-home/hooks.json && "
    f"cp {PLUGIN_DIR}/agents/probe-mini.toml /sandbox/codex-home/agents/probe-mini.toml && "
    f"ls -laR /sandbox/.agents /sandbox/codex-home/skills /sandbox/codex-home/agents /sandbox/codex-home/hooks.json"
)
# A plugin from a local marketplace (the operator's own ~/.agents/plugins/marketplace.json shape):
# installed by exec before the harness, its skill asked for as `plugin-skill`; the install output
# is recorded whatever happens.
MARKETPLACE = "/sandbox/.agents/plugins/marketplace.json"
PLUGIN_INSTALL = (
    f"mkdir -p /sandbox/.agents/plugins && cp {PLUGIN_DIR}/marketplace.json {MARKETPLACE} && cd /sandbox/repo && "
    f"export CODEX_HOME=/sandbox/codex-home; echo '@@ marketplace-add'; timeout 60 codex plugin marketplace add {MARKETPLACE} </dev/null 2>&1 | tail -n 8; "
    f"echo '@@ marketplace-list'; timeout 60 codex plugin marketplace list </dev/null 2>&1 | tail -n 12; "
    f"echo '@@ plugin-add'; timeout 90 codex plugin add plugin-probe@probe-market </dev/null 2>&1 | tail -n 12; echo \"rc=$?\"; "
    f"echo '@@ plugin-list'; timeout 60 codex plugin list </dev/null 2>&1 | tail -n 20; "
    f"echo '@@ cache'; ls -laR /sandbox/codex-home/plugins 2>&1 | head -n 30; echo '@@ config'; cat /sandbox/codex-home/config.toml 2>&1 | head -n 30"
)


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

def codex_command(mode: str) -> str:
    """The runner's Codex command (harness_command); control and agents-off switch subagents off with the
    documented switch (AGENTS_OFF), mech and notrust turn the three features on, mech also bypasses hook trust for
    the invocation (the documented automation flag)."""
    base = harness_command("codex", None)
    assert RUNNER_FLAGS in base, "the runner's Codex flags changed; update RUNNER_FLAGS"
    if mode in ("control", "agents-off"):
        command = base.replace(RUNNER_FLAGS, OFF_FLAGS, 1)
        assert AGENTS_OFF in command
        return command
    command = base.replace(RUNNER_FLAGS, ON_FLAGS, 1)
    if mode == "mech":
        command = command.replace(" -c approval_policy=", f" {TRUST_FLAG} -c approval_policy=", 1)
        assert TRUST_FLAG in command
    return command


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


def assert_runner_shape(harness_timeout: int, linger: int) -> None:
    """With the runner's own command in the slot, the probe's main script IS the runner's."""
    ours = probe_main_script(harness_command("codex", None), harness_timeout, linger)
    theirs = openshell_runner.main_script("codex", None, harness_timeout=harness_timeout, linger=linger)
    if ours != theirs:
        raise RuntimeError("the runner's main_script changed shape; update probe_main_script")


def prompt_for(mode: str, codeword: str) -> str:
    return (HERE / "prompts" / PROMPT_FILES[mode]).read_text().replace("@CODEWORD@", codeword)


# -- in-sandbox reads -----------------------------------------------------------------------

def blocks_script(blocks: list[tuple[str, str]]) -> str:
    return "\n".join(f"echo '== {label} =='; ( {body} ) 2>&1" for label, body in blocks)


PRE_READS = [
    ("pre-env", 'echo "uid=$(id -u) HOME=$HOME CODEX_HOME=${CODEX_HOME-unset}"; '
                'echo "exec env CLAUDE_CODE_OAUTH_TOKEN entries: $(env | grep -c "^CLAUDE_CODE_OAUTH_TOKEN=")"; '
                'codex --version 2>&1 | tail -n 1'),
    ("pre-fixtures", "ls -laR /sandbox/.agents /sandbox/codex-home 2>&1 | head -n 60"),
    ("pre-codex-home", "stat -c '%a %U %n' /sandbox/codex-home /sandbox/codex-home/auth.json"),
]
EARLY_READS = [
    ("early-repo", "cd /sandbox/repo && { test -d .git && echo 'dot-git is a directory (full clone)'; git rev-parse HEAD; "
                   "git log --oneline -3; git ls-files .agents; ls -la .git/worktrees; }"),
    ("early-procs", "for p in $(pgrep -u \"$(id -u)\" -f 'codex|^timeout'); do [ \"$p\" = \"$$\" ] && continue; "
                    "echo \"pid $p: $(tr '\\0' ' ' < /proc/$p/cmdline | cut -c1-120)\"; done 2>&1 | head -n 20"),
]
SUB_FILES = " ".join(f"/sandbox/sub-{m}.txt /sandbox/sub-{m}-curl.txt /sandbox/sub-{m}-env.txt" for m in ("mini", "default"))
# Every rollout Codex wrote under CODEX_HOME: its path, the session_meta head and the models of its
# turn_context records, so a child thread's model is read from Codex's own record, not a self-report.
ROLLOUTS = r"""python3 - <<'PY'
import glob, json, os
root = os.environ.get("CODEX_HOME", "/sandbox/codex-home")
files = sorted(glob.glob(os.path.join(root, "sessions", "**", "*.jsonl"), recursive=True))
print(f"rollouts under {root}/sessions: {len(files)}")
for path in files:
    models, meta, kinds = [], None, {}
    with open(path, errors="replace") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            t = rec.get("type"); kinds[t] = kinds.get(t, 0) + 1
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if t == "session_meta" and meta is None:
                meta = {k: payload.get(k) for k in ("id", "cwd", "originator", "cli_version", "source", "model_provider")}
                for k in ("agent_role", "agent_nickname", "parent_thread_id", "thread_name", "agent_id", "parent_id"):
                    if k in payload: meta[k] = payload.get(k)
                for k in ("forked_from_id", "model"):
                    if k in payload: meta[k] = payload.get(k)
            if t == "turn_context":
                m = payload.get("model")
                if m and (not models or models[-1] != m): models.append(m)
    print(f"-- {os.path.relpath(path, root)} size={os.path.getsize(path)}")
    print(f"   meta={json.dumps(meta)}")
    print(f"   turn_context models={models} kinds={json.dumps(kinds)}")
PY"""
POST_READS = [
    ("subagent-files", f'for f in {SUB_FILES}; do echo "-- $f"; cat "$f"; done'),
    ("firelog", 'f="$HOME/.local/share/codex-probe/fires.jsonl"; echo "HOME=$HOME"; ls -la "$HOME/.local/share/codex-probe" 2>&1; '
                'if [ -f "$f" ]; then echo "-- $f lines=$(wc -l < "$f")"; cat "$f"; else echo "no fire log"; fi'),
    ("state", 'ls -laR "$HOME/.local/state" 2>&1 | head -n 40'),
    ("worktrees", "cd /sandbox/repo && ls -la .git/worktrees; git worktree list"),
    ("usr-paths", "ls -ld /usr/local/cx-wt /usr/cx-mark /usr/codex-probe-mark"),
    ("codex-home", "cd /sandbox/codex-home && find . -maxdepth 3 -not -path './sessions/*' | sort | head -n 80; "
                   "echo '-- trust files'; grep -rl 'hook' --include='*.json' --include='*.toml' . 2>/dev/null | grep -v hooks.json | head"),
    ("codex-auth", "sha256sum /sandbox/codex-home/auth.json | cut -c1-64; stat -c '%a' /sandbox/codex-home/auth.json; "
                   "ls -la /sandbox/codex-home"),
    ("repo-after", "cd /sandbox/repo && git log --oneline -5 && git status --porcelain"),
    ("tmp", "ls -la /tmp"),
]
# `codex exec --worktree` from an exec after the harness: a second session that creates a managed
# worktree (feature `worktrees`); where it lands is the question. Bounded, stdin closed.
WORKTREE_STEP = (f"cd /sandbox/repo && git worktree prune; CODEX_HOME=/sandbox/codex-home timeout --kill-after=10 240 codex exec --json "
                 f"--skip-git-repo-check --sandbox danger-full-access -c approval_policy=\"never\" {OFF_FLAGS} -c features.apps=false "
                 f"--worktree -m {WORKTREE_MODEL} 'Run exactly one shell command: pwd; git rev-parse --show-toplevel; git worktree list. "
                 f"Then reply with the single word WT-DONE.' </dev/null >/tmp/codex-wt.out 2>/tmp/codex-wt.err; echo \"rc=$?\"; "
                 f"echo '@@ stdout'; cut -c1-400 /tmp/codex-wt.out; echo '@@ stderr-tail'; tail -n 20 /tmp/codex-wt.err | cut -c1-300; "
                 f"echo '@@ worktrees'; git worktree list; ls -la .git/worktrees 2>&1; echo '@@ codex-home-worktrees'; "
                 f"ls -laR /sandbox/codex-home/worktrees 2>&1 | head -n 30; find / -xdev -maxdepth 4 -type d -name '*worktree*' 2>/dev/null | head")
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
for root, control in (("/sandbox", b"aos-policy"), ("/tmp", b"codex"), ("/home/sandbox", b"PS1")):
    found = {k: count(root, v) for k, v in values.items()}
    print(f"files under {root} holding a codex token: {found}; control {control.decode()!r}: {count(root, control)}")
objs = subprocess.run(["git", "-C", "/sandbox/repo", "cat-file", "--batch-all-objects", "--batch"], capture_output=True).stdout
in_objs = {k: objs.count(v) for k, v in values.items()}
print(f"codex tokens in the decompressed git objects: {in_objs}; control 'codex fixtures': {objs.count(b'codex fixtures')}")
PY"""


# -- prepare --------------------------------------------------------------------------------

def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} exited {result.returncode}: {result.stderr.strip()[-300:]}")
    return result.stdout


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def skill_text(name: str, where: str, phrase: str) -> str:
    return (FIXTURES / "skills" / "SKILL.md.tmpl").read_text().replace("@NAME@", name).replace("@WHERE@", where).replace("@PHRASE@", phrase)


def prepare(args: argparse.Namespace) -> int:
    """Rebuild the provisioned job tarball with the repository skill committed, and build the plugins
    tarball (the hook, hooks.json, the agent role and the two other skills). Each skill carries its
    own fresh code phrase, written to the summary so checks.py can look for exactly those strings."""
    phrases = {name: f"PHRASE-{name.split('-')[0].upper()}-{secrets.token_hex(3)}"
               for name in ("user-skill", "home-skill", "repo-skill", "plugin-skill")}
    summary: dict[str, object] = {"phrases": phrases}
    with tempfile.TemporaryDirectory(prefix="cxm-prepare-") as scratch:
        root = Path(scratch) / "job"
        root.mkdir()
        with tarfile.open(args.base_tarball, "r:gz") as tar:
            tar.extractall(root, filter="tar")
        summary["base_commit"] = git("rev-parse", "HEAD", cwd=root).strip()
        skill = root / REPO_SKILL
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(skill_text("repo-skill", "the repository's .agents/skills", phrases["repo-skill"]))
        git("add", "--", REPO_SKILL, cwd=root)
        git("-c", "user.name=codex-probe", "-c", "user.email=probe@localhost.invalid", "commit", "-q", "-m", FIXTURE_COMMIT, cwd=root)
        summary["fixture_commit"] = git("rev-parse", "HEAD", cwd=root).strip()
        summary["committed"] = git("ls-tree", "-r", "--name-only", "HEAD", "--", ".agents", cwd=root).split()
        with tarfile.open(args.out, "w:gz") as tar:
            for child in sorted(root.iterdir()):
                tar.add(child, arcname=child.name)
        plug = Path(scratch) / "codex-probe"
        (plug / "skills" / "user-skill").mkdir(parents=True)
        (plug / "skills" / "home-skill").mkdir(parents=True)
        (plug / "agents").mkdir()
        (plug / "skills" / "user-skill" / "SKILL.md").write_text(skill_text("user-skill", "$HOME/.agents/skills", phrases["user-skill"]))
        (plug / "skills" / "home-skill" / "SKILL.md").write_text(skill_text("home-skill", "$CODEX_HOME/skills", phrases["home-skill"]))
        shutil.copyfile(FIXTURES / "hook.py", plug / "hook.py")
        shutil.copyfile(FIXTURES / "hooks.json", plug / "hooks.json")
        shutil.copyfile(FIXTURES / "agents" / "probe-mini.toml", plug / "agents" / "probe-mini.toml")
        json.loads((plug / "hooks.json").read_text())  # must parse
        # The plugin and its marketplace (the operator's ~/.agents/plugins/marketplace.json shape, absolute path).
        plugin = plug / "plugin-probe"
        (plugin / ".codex-plugin").mkdir(parents=True)
        (plugin / "skills" / "plugin-skill").mkdir(parents=True)
        (plugin / ".codex-plugin" / "plugin.json").write_text(json.dumps({
            "name": "plugin-probe", "version": "0.0.1", "description": "Mechanism probe plugin carrying one skill.",
            "author": {"name": "aos probe"}, "skills": "./skills/"}, indent=2) + "\n")
        (plugin / "skills" / "plugin-skill" / "SKILL.md").write_text(skill_text("plugin-skill", "a plugin installed from a local marketplace", phrases["plugin-skill"]))
        (plug / "marketplace.json").write_text(json.dumps({
            "name": "probe-market", "interface": {"displayName": "Probe market"},
            # A local source path is relative to the marketplace ROOT, which `codex plugin marketplace list`
            # showed as $HOME (/sandbox) for the auto-discovered ~/.agents/plugins/marketplace.json; an
            # absolute path answered "plugin ... was not found in marketplace" (round 2).
            "plugins": [{"name": "plugin-probe", "source": {"source": "local", "path": "./plugins/codex-probe/plugin-probe"},
                         "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"}]}, indent=2) + "\n")
        with tarfile.open(args.plugins_out, "w:gz") as tar:
            tar.add(plug, arcname="codex-probe")
        with tarfile.open(args.plugins_out, "r:gz") as tar:
            names = tar.getnames()
    summary["tarball"] = {"path": args.out, "bytes": os.path.getsize(args.out), "sha256": sha256(Path(args.out))}
    summary["plugins"] = {"path": args.plugins_out, "bytes": os.path.getsize(args.plugins_out),
                          "sha256": sha256(Path(args.plugins_out)), "members": names}
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
    phrases = json.loads(Path(args.prepare_summary).read_text())["phrases"] if args.prepare_summary else {}
    timeline: list[dict] = []
    summary: dict[str, object] = {"name": args.name, "mode": args.mode, "phrases": phrases}

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
    prompt = prompt_for(args.mode, codeword)
    command = codex_command(args.mode)
    linger = args.harness_timeout + 900 + openshell_runner.LINGER_MARGIN
    assert_runner_shape(args.harness_timeout, linger)
    script = probe_main_script(command, args.harness_timeout, linger)
    (evidence / "main-script.sh").write_text(script)
    (evidence / "prompt.txt").write_text(prompt)
    (evidence / "codex-command.txt").write_text(command + "\n")
    summary["codeword"] = codeword
    summary["command"] = command
    exec_text: list[str] = []

    # The runner's create call for a Codex run on this host: no provider (AOS_OPENSHELL_PROVIDER_CODEX
    # unset; the worker's auth.json is the credential), the two DEFAULT_ENV entries.
    openshell("sandbox", "create", "--name", args.name, "--from", args.image, "--policy", args.policy,
              *sum((["--env", e] for e in DEFAULT_ENV), []),
              "--cpu", args.cpu, "--memory", args.memory, "--detach", "--no-tty", "--", "bash", "-lc", script)
    mark("sandbox.created", name=args.name, image=args.image)
    try:
        wait_for_file(args.name, "/sandbox/incoming", 180)
        effective = openshell("policy", "get", args.name, "--full", timeout=60).stdout
        (evidence / "effective-policy.yaml").write_text(effective)
        problems = compare_effective(Path(args.policy).read_text(), effective, strict=False)
        summary["effective_policy_problems"] = problems
        mark("policy.effective", problems=problems)

        # The fixtures: the tarball up, extracted and installed into their Codex locations by exec
        # before the job is released.
        upload(args.name, Path(args.plugins_tarball), "/sandbox/plugins.tgz")
        sandbox_exec(args.name, "mkdir -p /sandbox/plugins && tar xzf /sandbox/plugins.tgz -C /sandbox/plugins "
                                "&& rm -f /sandbox/plugins.tgz", check=True, timeout=120)
        installed = sandbox_exec(args.name, INSTALL, check=True, timeout=60)
        exec_text.append("== install ==\n" + installed.stdout + installed.stderr)
        if not args.skip_plugin_install:
            plugin = sandbox_exec(args.name, PLUGIN_INSTALL, timeout=300)
            exec_text.append("== plugin-install ==\n" + plugin.stdout + plugin.stderr)
        mark("fixtures.installed")

        # Codex auth: a 0600 private copy, uploaded, unlinked whether or not the upload succeeds
        # (the runner's 1294-1299 form).
        auth_local = state / "auth.json"
        private_copy(Path(args.codex_auth).expanduser(), auth_local)
        try:
            upload(args.name, auth_local, "/sandbox/codex-home/auth.json")
        finally:
            auth_local.unlink(missing_ok=True)
        sandbox_exec(args.name, "chmod 600 /sandbox/codex-home/auth.json", check=True)
        mark("codex_auth.uploaded")

        local = state / "prompt.txt"
        local.write_text(prompt)
        upload(args.name, local, "/sandbox/prompt.txt")
        local.unlink()
        mark("prompt.uploaded")

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

        if args.mode == "mech" and not args.skip_worktree_step:
            wt = sandbox_exec(args.name, WORKTREE_STEP, timeout=330)
            exec_text.append("== worktree-step ==\n" + wt.stdout + wt.stderr)
            mark("worktree_step.done", rc=wt.returncode)
        post = sandbox_exec(args.name, blocks_script(POST_READS), timeout=180)
        exec_text.append(post.stdout + post.stderr)
        # The two python heredocs run as their own execs: inside blocks_script's `( ... )` the
        # terminator line would read `PY ) 2>&1`, which never ends the heredoc (run 1 lost every
        # block after it).
        rollouts = sandbox_exec(args.name, ROLLOUTS, timeout=120)
        exec_text.append("== rollouts ==\n" + rollouts.stdout + rollouts.stderr)
        scan_in = sandbox_exec(args.name, IN_SANDBOX_SCAN, timeout=300)
        exec_text.append("== in-sandbox-scan ==\n" + scan_in.stdout + scan_in.stderr)
        joined = redact("\n".join(exec_text), values)
        (downloads / "exec-reads.txt").write_text(joined)
        digest_line = checks.exec_blocks(joined).get("codex-auth", "").strip().splitlines()[:1]
        # True, False, or "unknown" when the digest read itself failed (never a false "changed").
        summary["codex_auth_unchanged"] = (digest_line[0] == host_auth_digest) if digest_line else "unknown"
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

    _, text = checks.analyze(args.mode, downloads, evidence, codeword, values, phrases)
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
    parser = argparse.ArgumentParser(description="Codex mechanisms inside an OpenShell sandbox.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="rebuild the job tarball with the repository skill; build the plugins tarball")
    p.add_argument("--base-tarball", required=True, help="run_job.py provision output")
    p.add_argument("--out", required=True, help="the rebuilt job tarball")
    p.add_argument("--plugins-out", required=True)
    p.set_defaults(func=prepare)
    p = sub.add_parser("run")
    p.add_argument("--name", required=True, help="sandbox name, cxm-*")
    p.add_argument("--mode", choices=MODES, required=True)
    p.add_argument("--image", required=True)
    p.add_argument("--policy", required=True, help="rendered OpenShell policy YAML")
    p.add_argument("--tarball", required=True, help="job tarball from `prepare`")
    p.add_argument("--plugins-tarball", required=True)
    p.add_argument("--prepare-summary", help="prepare's JSON output (the code phrases checks.py looks for)")
    p.add_argument("--codex-auth", required=True, help="the worker's Codex auth.json (uploaded, never printed)")
    p.add_argument("--claude-token-file", default="~/.aos/worker/claude-oauth-token",
                   help="the worker's Claude token, read only for the scan's counts")
    p.add_argument("--state", required=True, help="private scratch dir for files staged for upload")
    p.add_argument("--downloads", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--harness-timeout", type=int, default=2700)
    p.add_argument("--cpu", default="2")
    p.add_argument("--memory", default="2Gi")
    p.add_argument("--skip-worktree-step", action="store_true", help="mech: do not run the post-harness `codex exec --worktree` step")
    p.add_argument("--skip-plugin-install", action="store_true", help="do not install the probe plugin from its local marketplace")
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
