#!/usr/bin/env python3
"""Pi-family mechanism probe driver: one real `pi --print` or `omp --print` run per flavour and
mode inside an OpenShell sandbox, measuring whether the mechanisms a statiker run would use on Pi
or omp (an extension loaded by an explicit path, its event hooks and custom tool, skills, subagents
with a model of their own, worktrees, the state directories) work there and whether the
aos-example policy's boundaries hold through each of them.

Probe tooling (docs/probes/2026-10-14-pi-mechanisms.md), not the production adapter. Built beside
the codex_mech and statiker_mech kits, whose helpers it copies. It imports the runner's own building
blocks so that the sandbox create call, the main script's shape, the flavour's command and the
worker-auth snapshot are the runner's; the deviations are the per-mode flags appended after the
runner's (README.md). No bus, no task, no run file. Every `openshell` argv is logged to stderr. The
Pi auth snapshot is staged 0600 in the private state directory, uploaded and unlinked at once; no
secret value is ever printed, logged or passed on a command line, and the secret scan (checks.py)
counts occurrences only.

    uv run python scripts/openshell_probe/pi_mech/driver.py prepare --base-tarball B --out J --plugins-out P --pi-npm-out N
    uv run python scripts/openshell_probe/pi_mech/driver.py run --name pim-pi-mech --flavour pi --mode mech \\
        --image aos-harness:comms --policy Y --tarball J --plugins-tarball P --pi-npm-tarball N \\
        --pi-auth ~/.aos/worker/pi-auth.json --state S --downloads D --evidence E
    uv run python scripts/openshell_probe/pi_mech/driver.py cleanup --name pim-pi-mech
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
import uuid
from pathlib import Path

from agent_os import openshell_runner, pi
from agent_os.openshell_runner import DEFAULT_ENV, SERVICES_ENV, SWEEP, compare_effective, deadline_lines, pi_invocation, wait_job_lines

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import checks  # noqa: E402  (the kit's own module, beside this file)

OPENSHELL = os.environ.get("OPENSHELL_BIN", "openshell")
GATEWAY = "https://localhost:17670"
NAME_PREFIX = "pim-"
FLAVOURS = ("pi", "omp")
MODES = ("mech", "control", "statiker", "statiker-link", "tools-off")
# tools-off (omp only): the runner's command plus an explicit --tools list, the tools the omp control run (5) had
# minus `task`, measuring whether the allowlist closes omp's native subagent tool (record finding 4, run 9). From
# 4aa9633 until subagents went back on by default the runner passed that list itself; without comms (as here) it
# passes none again, so omp's control is run 5's command and tools-off appends the list as run 9 did.
OMP_TOOLS_OFF = "--tools=read,bash,edit,eval,glob,grep,wait,todo,write,web_search"
DEFAULT_MODEL = {"pi": "gpt-6-luna", "omp": "openai-codex/gpt-6-luna"}
FIXTURES = HERE / "fixtures"
PLUGIN_DIR = "/sandbox/plugins/pi-probe"
PI_NPM_DIR = "/sandbox/plugins/pi-npm"
GUARDS_DIR = "/sandbox/plugins/pi-dispatch-guards"
STATIKER_DIR = "/sandbox/plugins/statiker"
PI_DISPATCH_GUARDS_SRC = Path("~/code/pi-dispatch-guards").expanduser()
STATIKER_FORK_SRC = Path("~/plugins/statiker").expanduser()
PI_NPM_SRC = Path("~/.pi/agent/npm/node_modules").expanduser()
REPO_SKILL = ".agents/skills/repo-skill/SKILL.md"
FIXTURE_COMMIT = "probe: pi fixtures (repository skill)"
CHILD_MODEL = "openai-codex/gpt-5.6-terra"
# The flags each mech mode appends after the runner's own (explicit -e and --skill paths work under
# --no-extensions and --no-skills per `pi --help`; omp has no --skill, so its mech mode drops
# --no-skills and filters discovery with --skills=).
MECH_FLAGS = {
    "pi": f"-e {PLUGIN_DIR}/index.ts -e {PI_NPM_DIR}/node_modules/pi-subagents -e {GUARDS_DIR} --skill {PLUGIN_DIR}/skills/probe-skill",
    "omp": f"-e {PLUGIN_DIR}/index.ts --config {PLUGIN_DIR}/overlay-isolation.yml --skills=probe-*,repo-*",
}
STATIKER_FLAGS = f"-e {STATIKER_DIR}/omp/extension.ts"
# statiker-link: the package registered with `omp plugin link` under omp's HOME before the harness
# (the extension alone, by -e, brought none of the package's agents or skill in run 7), and the
# runner's --no-extensions dropped so that omp discovers the linked plugin.
STATIKER_LINK = (f"export HOME=/tmp/pi-home PI_CODING_AGENT_DIR={pi.AGENT_DIR}; cd /sandbox/repo; echo '@@ plugin-link'; "
                 f"timeout 120 omp plugin link {STATIKER_DIR}/omp </dev/null 2>&1 | tail -n 15; echo \"rc=$?\"; "
                 f"echo '@@ plugin-list'; timeout 60 omp plugin list </dev/null 2>&1 | tail -n 20; "
                 f"echo '@@ omp-plugins-dir'; ls -laR /tmp/pi-home/.omp/plugins 2>&1 | head -n 30; "
                 f"echo '@@ config'; cat /tmp/pi-home/.omp/agent/config.yml 2>&1 | head -n 20")
# Where the fixtures land per flavour. Both flavours honour PI_CODING_AGENT_DIR=/tmp/pi-agent (the
# runner's agent dir); omp also runs with HOME=/tmp/pi-home, so a second copy goes under its
# ~/.omp/agent to measure which location it reads. The main script creates both /tmp dirs first.
INSTALL = {
    # pi-dispatch-guards resolves pi-subagents' child-registration API at the fixed path
    # ~/.pi/agent/npm/node_modules/pi-subagents/... (its index.ts), so the uploaded packages tree is
    # linked there too (HOME=/sandbox).
    "pi": (f"mkdir -p {pi.AGENT_DIR}/agents /sandbox/.pi/agent/npm && cp {PLUGIN_DIR}/agents/pi/probe-agent.md {pi.AGENT_DIR}/agents/ && "
           f"cp {PLUGIN_DIR}/dispatch-guards.json /sandbox/.pi/agent/dispatch-guards.json && "
           f"ln -sfn {PI_NPM_DIR}/node_modules /sandbox/.pi/agent/npm/node_modules && "
           f"ls -laR {pi.AGENT_DIR} /sandbox/.pi | head -n 40"),
    # omp (measured 2026-10-15, omp 18.8.3): agents are read from $HOME/.omp/agent/agents only ("Searched:
    # ~/.omp/agent/agents"; a file under $PI_CODING_AGENT_DIR/agents is not offered), skills from
    # $PI_CODING_AGENT_DIR/skills and the repo's .agents/skills (not from $HOME/.omp/agent/skills). So
    # probe-agent lives in both agent dirs (the task tool offers it from HOME) and probe-agent-home in HOME.
    "omp": (f"mkdir -p {pi.AGENT_DIR}/agents {pi.AGENT_DIR}/skills /tmp/pi-home/.omp/agent/agents /tmp/pi-home/.omp/agent/skills && "
            f"cp {PLUGIN_DIR}/agents/omp/probe-agent.md {pi.AGENT_DIR}/agents/ && "
            f"cp {PLUGIN_DIR}/agents/omp/probe-agent.md /tmp/pi-home/.omp/agent/agents/ && "
            f"cp {PLUGIN_DIR}/agents/omp/probe-agent-home.md /tmp/pi-home/.omp/agent/agents/ && "
            f"cp -r {PLUGIN_DIR}/skills/probe-skill {pi.AGENT_DIR}/skills/ && "
            f"cp -r {PLUGIN_DIR}/skills/probe-skill-home /tmp/pi-home/.omp/agent/skills/ && "
            f"ls -laR {pi.AGENT_DIR} /tmp/pi-home | head -n 60"),
}


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


def upload_parts(name: str, local: Path, dest: str) -> int:
    """A file above one upload part (the runner's UPLOAD_PART_BYTES) goes up in parts under
    /sandbox/incoming and is joined inside, size- and digest-checked (the runner's upload_job form,
    for a destination other than the job). Returns the number of parts."""
    size = local.stat().st_size
    digest = hashlib.sha256(local.read_bytes()).hexdigest()
    base = os.path.basename(dest)
    index = 0
    with open(local, "rb") as source, tempfile.TemporaryDirectory(prefix="pim-upload-") as scratch:
        while True:
            chunk = source.read(openshell_runner.UPLOAD_PART_BYTES)
            if not chunk:
                break
            part = Path(scratch) / f"{base}.{index:04d}"
            part.write_bytes(chunk)
            openshell("sandbox", "upload", name, str(part), f"/sandbox/incoming/{base}.{index:04d}")
            part.unlink()
            index += 1
    sandbox_exec(name, f"cd /sandbox/incoming && cat {base}.[0-9][0-9][0-9][0-9] > {dest}.part && rm -f {base}.[0-9][0-9][0-9][0-9] "
                       f"&& test \"$(stat -c %s {dest}.part)\" = {size} && echo '{digest}  {dest}.part' | sha256sum -c --status "
                       f"&& mv {dest}.part {dest}", check=True, timeout=300)
    return index


def listed(listing: str, name: str) -> bool:
    return any(line.split() and line.split()[0] == name for line in listing.splitlines())


def gateway_check() -> str:
    out = openshell("status", check=False, timeout=60).stdout
    if f"Server: {GATEWAY}" not in out or "Status: Connected" not in out:
        raise RuntimeError(f"openshell status does not show a connected {GATEWAY}:\n{out}")
    return out


# -- what the sandbox receives --------------------------------------------------------------

def harness_cmd(flavour: str, mode: str, model: str, session_id: str | None, harness_timeout: int) -> str:
    """The runner's command for the flavour (pi_invocation), then the mode's flags appended."""
    base = pi_invocation(flavour, flavour, None, model, session_id, harness_timeout).command
    if mode == "tools-off" and flavour != "omp":
        raise RuntimeError("tools-off is omp only (pi has no native subagent tool)")
    assert "--tools" not in base, "the runner passes a --tools list again; tools-off and the mech modes assume none"
    if mode == "control":
        return base
    if mode == "tools-off":
        return f"{base} {OMP_TOOLS_OFF}"
    command = base
    if flavour == "omp":
        assert " --no-skills" in command
        command = command.replace(" --no-skills", "", 1)   # --skills= needs discovery on
    command += " " + MECH_FLAGS[flavour]
    if mode in ("statiker", "statiker-link"):
        if flavour != "omp":
            raise RuntimeError("statiker modes are omp only (the operator's statiker-omp extension)")
        if mode == "statiker":
            command += " " + STATIKER_FLAGS
        else:
            assert " --no-extensions" in command
            command = command.replace(" --no-extensions", "", 1)   # the linked plugin needs discovery on
    return command


def probe_main_script(command: str, harness_timeout: int, linger: int) -> str:
    """openshell_runner.main_script line for line, with the probe's command in the harness slot
    (a Pi run reads its prompt from /sandbox/prompt.txt on stdin)."""
    *definition, invocation = deadline_lines(command, "/sandbox/prompt.txt", harness_timeout)
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


def assert_runner_shape(flavour: str, model: str, session_id: str | None, harness_timeout: int, linger: int) -> None:
    ours = probe_main_script(harness_cmd(flavour, "control", model, session_id, harness_timeout), harness_timeout, linger)
    theirs = openshell_runner.main_script("pi", model, session_id, flavour=flavour, bin=flavour, provider=None,
                                          harness_timeout=harness_timeout, linger=linger)
    if ours != theirs:
        raise RuntimeError("the runner's main_script changed shape; update probe_main_script")


def prompt_for(flavour: str, mode: str, codeword: str) -> str:
    """control and tools-off: control.md; every other mode: mech.md with the flavour's subagent step spliced in."""
    if mode in ("control", "tools-off"):
        text = (HERE / "prompts" / "control.md").read_text()
    else:
        text = (HERE / "prompts" / "mech.md").read_text()
        text = text.replace("@SUBAGENT_STEP@", (HERE / "prompts" / f"subagent-{flavour}.md").read_text().strip())
    return text.replace("@CODEWORD@", codeword)


# -- in-sandbox reads -----------------------------------------------------------------------

def blocks_script(blocks: list[tuple[str, str]]) -> str:
    return "\n".join(f"echo '== {label} =='; ( {body} ) 2>&1" for label, body in blocks)


def pre_reads(flavour: str) -> list[tuple[str, str]]:
    return [
        ("pre-env", 'echo "uid=$(id -u) HOME=$HOME PI_CODING_AGENT_DIR=${PI_CODING_AGENT_DIR-unset}"; '
                    'echo "exec env CLAUDE_CODE_OAUTH_TOKEN entries: $(env | grep -c "^CLAUDE_CODE_OAUTH_TOKEN=")"'),
        ("pre-version", f"{flavour} --version 2>&1 | tail -n 1"),
        ("pre-help-flags", f"{flavour} --help 2>&1 | grep -E -- '--extension|--skill|--no-extensions|--no-skills|--config|--hook|--skills' | head -n 12"),
        ("pre-fixtures", f"ls -laR {pi.AGENT_DIR} /tmp/pi-home /sandbox/plugins 2>&1 | head -n 80"),
        ("pre-auth", f"stat -c '%a %U %n' {pi.AGENT_DIR}/auth.json {pi.AGENT_DIR}/agent.db 2>&1"),
    ]


EARLY_READS = [
    ("early-repo", "cd /sandbox/repo && { test -d .git && echo 'dot-git is a directory (full clone)'; git rev-parse HEAD; "
                   "git log --oneline -3; git ls-files .agents; ls -la .git/worktrees; }"),
    ("early-procs", "for p in $(pgrep -u \"$(id -u)\" -f 'pi |omp |node|^timeout'); do [ \"$p\" = \"$$\" ] && continue; "
                    "echo \"pid $p: $(tr '\\0' ' ' < /proc/$p/cmdline | cut -c1-160)\"; done 2>&1 | head -n 20"),
]
SUB_FILES = " ".join(f"/sandbox/sub-{m}.txt /sandbox/sub-{m}-curl.txt /sandbox/sub-{m}-env.txt /sandbox/sub-{m}-cwd.txt"
                     for m in ("probe", "second"))
# Every session file the run wrote (the runner's --session-dir, Pi's and omp's own dirs): the
# models of its assistant messages, so a child's model is read from the harness's own record.
SESSIONS = r"""python3 - <<'PY'
import glob, json, os
roots = ["/sandbox/pi-sessions", "/tmp/pi-agent", "/tmp/pi-home", "/sandbox/.pi", "/sandbox/.omp", "/sandbox/.local"]
files = []
for root in roots:
    files += glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
files = sorted(set(files))
print(f"jsonl files under {roots}: {len(files)}")
for path in files:
    models, kinds, first, cwd = [], {}, None, None
    try:
        with open(path, errors="replace") as fh:
            for raw in fh:
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                t = rec.get("type"); kinds[t] = kinds.get(t, 0) + 1
                if first is None:
                    first = {k: rec.get(k) for k in ("type", "id", "cwd", "parentId", "parentSessionId", "agent", "agentName") if k in rec}
                msg = rec.get("message") if isinstance(rec.get("message"), dict) else None
                if msg and msg.get("role") == "assistant" and msg.get("model"):
                    m = f"{msg.get('provider')}/{msg.get('model')}"
                    if not models or models[-1] != m: models.append(m)
                for k in ("cwd",):
                    if rec.get(k) and cwd is None: cwd = rec.get(k)
    except OSError as exc:
        print(f"-- {path}: {exc}"); continue
    print(f"-- {path} size={os.path.getsize(path)}")
    print(f"   first={json.dumps(first)} cwd={cwd}")
    print(f"   assistant models={models} kinds={json.dumps(dict(sorted(kinds.items(), key=lambda kv: str(kv[0]))))}")
PY"""
POST_READS = [
    ("subagent-files", f'for f in {SUB_FILES}; do echo "-- $f"; cat "$f"; done'),
    ("firelog", 'for f in /sandbox/.local/share/pi-probe/fires.jsonl /tmp/pi-home/.local/share/pi-probe/fires.jsonl '
                '/sandbox/.local/share/pi/dispatch-guards-fires.jsonl /tmp/pi-home/.local/share/pi/dispatch-guards-fires.jsonl; do '
                'if [ -f "$f" ]; then echo "-- $f lines=$(wc -l < "$f")"; cat "$f"; else echo "-- $f absent"; fi; done'),
    ("state", 'for d in /sandbox/.local/state /tmp/pi-home/.local/state; do echo "-- $d"; ls -laR "$d" 2>&1 | head -n 30; done'),
    ("worktrees", "cd /sandbox/repo && ls -la .git/worktrees; git worktree list"),
    ("usr-paths", "ls -ld /usr/local/pi-wt /usr/pi-mark /usr/pi-probe-mark"),
    ("agent-dir", f"find {pi.AGENT_DIR} -maxdepth 3 | sort | head -n 60; stat -c '%a %n' {pi.AGENT_DIR}/auth.json {pi.AGENT_DIR}/agent.db 2>&1"),
    ("pi-home", "find /tmp/pi-home -maxdepth 4 -not -path '*/node_modules/*' | sort | head -n 80"),
    ("sessions-dir", f"find {pi.SESSION_DIR} -maxdepth 4 | sort | head -n 40"),
    ("auth-sha", f"for f in {pi.AGENT_DIR}/auth.json {pi.AGENT_DIR}/agent.db; do [ -f \"$f\" ] && echo \"$(sha256sum \"$f\" | cut -c1-64) $f\"; done"),
    ("repo-after", "cd /sandbox/repo && git log --oneline -5 && git status --porcelain | head -n 20"),
    ("tmp", "ls -la /tmp"),
]
# Counts only: the worker's access token and account id are read from the uploaded snapshot inside
# the sandbox (auth.json or omp's agent.db) and never leave it; each count has a control string.
IN_SANDBOX_SCAN = r"""python3 - <<'PY'
import json, os, sqlite3, subprocess
AGENT = "/tmp/pi-agent"
values = {}
if os.path.exists(f"{AGENT}/auth.json"):
    entry = json.load(open(f"{AGENT}/auth.json")).get("openai-codex") or {}
    values = {k: str(entry[k]).encode() for k in ("access", "accountId") if entry.get(k)}
elif os.path.exists(f"{AGENT}/agent.db"):
    conn = sqlite3.connect(f"{AGENT}/agent.db")
    for (data,) in conn.execute("SELECT data FROM auth_credentials WHERE provider='openai-codex'"):
        entry = json.loads(data)
        values = {k: str(entry[k]).encode() for k in ("access", "accountId") if entry.get(k)}
    conn.close()
skip = {f"{AGENT}/auth.json", f"{AGENT}/agent.db"}
def count(root, needle):
    n = 0
    for d, dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            if p in skip or os.path.islink(p):
                continue
            try:
                with open(p, "rb") as fh:
                    n += needle in fh.read()
            except OSError:
                pass
    return n
print(f"secret labels read inside the sandbox: {sorted(values)}")
for root, control in (("/sandbox", b"aos-policy"), ("/tmp", b"pi-agent"), ("/home/sandbox", b"PS1")):
    found = {k: count(root, v) for k, v in values.items()}
    print(f"files under {root} holding a pi secret: {found}; control {control.decode()!r}: {count(root, control)}")
objs = subprocess.run(["git", "-C", "/sandbox/repo", "cat-file", "--batch-all-objects", "--batch"], capture_output=True).stdout
print(f"pi secrets in the decompressed git objects: {{k: objs.count(v) for k, v in values.items()}}; control 'pi fixtures': {objs.count(b'pi fixtures')}")
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


def agent_text(name: str, where: str) -> str:
    return (FIXTURES / "agents" / "omp" / "probe-agent.md").read_text().replace("@NAME@", name).replace("@WHERE@", where)


def prepare(args: argparse.Namespace) -> int:
    """Rebuild the provisioned job tarball with the repository skill committed; build the plugins
    tarball (the probe extension, the skills with fresh phrases, the agent files, the omp overlay,
    pi-dispatch-guards from its source checkout, the operator's statiker fork for the omp statiker mode)
    and the Pi packages tarball (the worker's own ~/.pi/agent/npm/node_modules, which holds
    pi-subagents and its dependencies, built for pi 0.85.1 on this host)."""
    phrases = {name: f"PHRASE-{name.split('-')[0].upper()}-{secrets.token_hex(3)}" for name in ("probe-skill", "probe-skill-home", "repo-skill")}
    phrases["probe-skill-home"] = f"PHRASE-HOME-{secrets.token_hex(3)}"
    summary: dict[str, object] = {"phrases": phrases}
    with tempfile.TemporaryDirectory(prefix="pim-prepare-") as scratch:
        root = Path(scratch) / "job"
        root.mkdir()
        with tarfile.open(args.base_tarball, "r:gz") as tar:
            tar.extractall(root, filter="tar")
        summary["base_commit"] = git("rev-parse", "HEAD", cwd=root).strip()
        skill = root / REPO_SKILL
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(skill_text("repo-skill", "the repository's .agents/skills", phrases["repo-skill"]))
        git("add", "--", REPO_SKILL, cwd=root)
        git("-c", "user.name=pi-probe", "-c", "user.email=probe@localhost.invalid", "commit", "-q", "-m", FIXTURE_COMMIT, cwd=root)
        summary["fixture_commit"] = git("rev-parse", "HEAD", cwd=root).strip()
        with tarfile.open(args.out, "w:gz") as tar:
            for child in sorted(root.iterdir()):
                tar.add(child, arcname=child.name)
        plug = Path(scratch) / "pi-probe"
        for sub in ("skills/probe-skill", "skills/probe-skill-home", "agents/pi", "agents/omp"):
            (plug / sub).mkdir(parents=True)
        shutil.copyfile(FIXTURES / "probe-ext" / "index.ts", plug / "index.ts")
        shutil.copyfile(FIXTURES / "overlay-isolation.yml", plug / "overlay-isolation.yml")
        (plug / "skills" / "probe-skill" / "SKILL.md").write_text(skill_text("probe-skill", "an explicit --skill path (pi) or $PI_CODING_AGENT_DIR/skills (omp)", phrases["probe-skill"]))
        (plug / "skills" / "probe-skill-home" / "SKILL.md").write_text(skill_text("probe-skill-home", "$HOME/.omp/agent/skills (omp)", phrases["probe-skill-home"]))
        shutil.copyfile(FIXTURES / "agents" / "pi" / "probe-agent.md", plug / "agents" / "pi" / "probe-agent.md")
        (plug / "agents" / "omp" / "probe-agent.md").write_text(agent_text("probe-agent", "$PI_CODING_AGENT_DIR/agents"))
        (plug / "agents" / "omp" / "probe-agent-home.md").write_text(agent_text("probe-agent-home", "$HOME/.omp/agent/agents"))
        (plug / "dispatch-guards.json").write_text(json.dumps({"defaultModel": CHILD_MODEL, "maxMessageChars": 3000}) + "\n")

        def skip(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            parts = Path(info.name).parts
            return None if any(p in ("__pycache__", "node_modules", ".git", "tests", "probes", "docs", ".clippy") for p in parts) else info

        with tarfile.open(args.plugins_out, "w:gz") as tar:
            tar.add(plug, arcname="pi-probe")
            tar.add(PI_DISPATCH_GUARDS_SRC, arcname="pi-dispatch-guards", filter=skip)
            tar.add(STATIKER_FORK_SRC, arcname="statiker", filter=skip)
        with tarfile.open(args.plugins_out, "r:gz") as tar:
            names = tar.getnames()
        summary["plugins"] = {"path": args.plugins_out, "bytes": os.path.getsize(args.plugins_out), "sha256": sha256(Path(args.plugins_out)),
                              "members": len(names), "pi_probe_members": [n for n in names if n.startswith("pi-probe/")],
                              "pi_dispatch_guards_commit": git("rev-parse", "--short", "HEAD", cwd=PI_DISPATCH_GUARDS_SRC).strip(),
                              "statiker_fork_commit": git("rev-parse", "--short", "HEAD", cwd=STATIKER_FORK_SRC).strip()}
        if args.pi_npm_out:
            npm_src = Path(args.pi_npm_src).expanduser() if args.pi_npm_src else PI_NPM_SRC

            def npm_skip(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
                parts = Path(info.name).parts
                return None if any(p in ("fsevents", ".bin") for p in parts) else info
            with tarfile.open(args.pi_npm_out, "w:gz") as tar:
                tar.add(npm_src, arcname="node_modules", filter=npm_skip)
            versions = {}
            for pkg in ("pi-subagents", "pi-intercom"):
                manifest = npm_src / pkg / "package.json"
                if manifest.exists():
                    versions[pkg] = json.loads(manifest.read_text()).get("version")
            summary["pi_npm"] = {"path": args.pi_npm_out, "bytes": os.path.getsize(args.pi_npm_out), "sha256": sha256(Path(args.pi_npm_out)),
                                 "versions": versions, "source": str(npm_src)}
    summary["tarball"] = {"path": args.out, "bytes": os.path.getsize(args.out), "sha256": sha256(Path(args.out))}
    print(json.dumps(summary, indent=2))
    return 0


# -- the run --------------------------------------------------------------------------------

def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def redact(text: str, values: dict[str, str]) -> str:
    for label, value in sorted(values.items(), key=lambda kv: -len(kv[1])):
        if value:
            text = text.replace(value, f"<{label}>")
    return text


def run(args: argparse.Namespace) -> int:
    if not args.name.startswith(NAME_PREFIX):
        raise RuntimeError(f"sandbox names start with {NAME_PREFIX}")
    flavour, mode = args.flavour, args.mode
    model = args.model or DEFAULT_MODEL[flavour]
    evidence = Path(args.evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    downloads = Path(args.downloads) / args.name
    downloads.mkdir(parents=True, exist_ok=True)
    phrases = json.loads(Path(args.prepare_summary).read_text())["phrases"] if args.prepare_summary else {}
    # The worker auth exactly as the runner selects and snapshots it (nonrenewable refresh value).
    try:
        auth = pi.selected_auth(os.path.expanduser(args.pi_auth), args.harness_timeout + 300)
    except pi.AuthError as exc:
        raise RuntimeError(f"Pi auth: {exc}") from None
    spec = pi.FLAVOURS[flavour]
    values = {f"pi_secret_{i}": s for i, s in enumerate(auth.secrets)}
    timeline: list[dict] = []
    summary: dict[str, object] = {"name": args.name, "flavour": flavour, "mode": mode, "model": model, "phrases": phrases,
                                  "auth": repr(auth)}

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
    session_id = str(uuid.uuid4()) if spec.session_id_flag else None
    prompt = prompt_for(flavour, mode, codeword)
    command = harness_cmd(flavour, mode, model, session_id, args.harness_timeout)
    linger = args.harness_timeout + 900 + openshell_runner.LINGER_MARGIN
    assert_runner_shape(flavour, model, session_id, args.harness_timeout, linger)
    script = probe_main_script(command, args.harness_timeout, linger)
    (evidence / "main-script.sh").write_text(script)
    (evidence / "prompt.txt").write_text(prompt)
    (evidence / "harness-command.txt").write_text(command + "\n")
    summary.update({"codeword": codeword, "session_id": session_id, "command": command})
    exec_text: list[str] = []

    # The runner's create call for a Pi run: no provider (the worker snapshot is the credential).
    openshell("sandbox", "create", "--name", args.name, "--from", args.image, "--policy", args.policy,
              *sum((["--env", e] for e in DEFAULT_ENV), []),
              "--cpu", args.cpu, "--memory", args.memory, "--detach", "--no-tty", "--", "bash", "-lc", script)
    mark("sandbox.created", name=args.name, image=args.image)
    try:
        wait_for_file(args.name, "/sandbox/incoming", 180)
        effective = openshell("policy", "get", args.name, "--full", timeout=60).stdout
        (evidence / "effective-policy.yaml").write_text(effective)
        problems = compare_effective(Path(args.policy).read_text(), effective, strict=True)
        summary["effective_policy_problems"] = problems
        mark("policy.effective", problems=problems)

        upload(args.name, Path(args.plugins_tarball), "/sandbox/plugins.tgz")
        sandbox_exec(args.name, "mkdir -p /sandbox/plugins && tar xzf /sandbox/plugins.tgz -C /sandbox/plugins "
                                "&& rm -f /sandbox/plugins.tgz", check=True, timeout=120)
        if flavour == "pi" and mode != "control":
            if not args.pi_npm_tarball:
                raise RuntimeError("pi mech needs --pi-npm-tarball (prepare --pi-npm-out)")
            size = os.path.getsize(args.pi_npm_tarball)
            if size > openshell_runner.UPLOAD_PART_BYTES:
                parts = upload_parts(args.name, Path(args.pi_npm_tarball), "/sandbox/pi-npm.tgz")
                mark("pi_npm.uploaded", bytes=size, parts=parts)
            else:
                upload(args.name, Path(args.pi_npm_tarball), "/sandbox/pi-npm.tgz")
            sandbox_exec(args.name, f"mkdir -p {PI_NPM_DIR} && tar xzf /sandbox/pi-npm.tgz -C {PI_NPM_DIR} && rm -f /sandbox/pi-npm.tgz "
                                    f"&& ls {PI_NPM_DIR}/node_modules | head -n 20", check=True, timeout=300)
        installed = sandbox_exec(args.name, INSTALL[flavour], check=True, timeout=60)
        exec_text.append("== install ==\n" + installed.stdout + installed.stderr)
        if mode == "statiker-link":
            linked = sandbox_exec(args.name, STATIKER_LINK, timeout=240)
            exec_text.append("== statiker-link ==\n" + linked.stdout + linked.stderr)
        mark("fixtures.installed")

        # The auth snapshot as the runner uploads it: private 0600 file, unlinked at once, chmod inside.
        snapshot = state / "pi-auth.json"
        _write_private(snapshot, spec.auth_payload(auth))
        try:
            upload(args.name, snapshot, spec.auth_path)
        finally:
            snapshot.unlink(missing_ok=True)
        sandbox_exec(args.name, f"chmod 600 {spec.auth_path}", check=True)
        try:
            pi.ensure_validity(auth, args.harness_timeout + 300)
        except pi.AuthError as exc:
            raise RuntimeError(f"Pi auth: {exc}") from None
        mark("pi_auth.uploaded", path=spec.auth_path)

        local = state / "prompt.txt"
        local.write_text(prompt)
        upload(args.name, local, "/sandbox/prompt.txt")
        local.unlink()
        mark("prompt.uploaded")

        pre = sandbox_exec(args.name, blocks_script(pre_reads(flavour)), timeout=90)
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

        post = sandbox_exec(args.name, blocks_script(POST_READS), timeout=180)
        exec_text.append(post.stdout + post.stderr)
        sessions = sandbox_exec(args.name, SESSIONS, timeout=120)       # its own exec: a heredoc inside blocks_script never ends
        exec_text.append("== sessions ==\n" + sessions.stdout + sessions.stderr)
        scan_in = sandbox_exec(args.name, IN_SANDBOX_SCAN, timeout=300)
        exec_text.append("== in-sandbox-scan ==\n" + scan_in.stdout + scan_in.stderr)
        (downloads / "exec-reads.txt").write_text(redact("\n".join(exec_text), values))
        mark("exec.reads_done")

        for remote in ("/sandbox/harness.out", "/sandbox/harness.err", "/sandbox/harness.rc", "/sandbox/repo/probe-report.md"):
            local = downloads / os.path.basename(remote)
            fetched = openshell("sandbox", "download", args.name, remote, str(local), check=False, timeout=300)
            if fetched.returncode != 0 or not local.exists():
                mark("download.failed", path=remote, rc=fetched.returncode, stderr=redact(fetched.stderr[-300:], values))
        if not (downloads / "probe-report.md").exists() or not (downloads / "probe-report.md").stat().st_size:
            got = sandbox_exec(args.name, "cat /sandbox/repo/probe-report.md", timeout=60)
            if got.returncode == 0:
                (downloads / "probe-report.md").write_text(got.stdout)
        # The downloads are scrubbed of the worker secrets before anything reads them (the runner
        # retains session.jsonl scrubbed the same way).
        for name in ("harness.out", "harness.err", "probe-report.md"):
            path = downloads / name
            if path.exists():
                path.write_bytes(pi.redact_bytes(path.read_bytes(), auth))
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

    _, text = checks.analyze(flavour, mode, downloads, evidence, codeword, values, phrases, session_id)
    for name in ("probe-report.md", "exec-reads.txt"):
        if (downloads / name).exists():
            shutil.copyfile(downloads / name, evidence / name)
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
    parser = argparse.ArgumentParser(description="Pi and omp mechanisms inside an OpenShell sandbox.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="rebuild the job tarball with the repository skill; build the plugins and Pi packages tarballs")
    p.add_argument("--base-tarball", required=True, help="run_job.py provision output")
    p.add_argument("--out", required=True, help="the rebuilt job tarball")
    p.add_argument("--plugins-out", required=True)
    p.add_argument("--pi-npm-out", help="a node_modules tree holding pi-subagents and its dependencies, as a tarball")
    p.add_argument("--pi-npm-src", help="that tree (default: the worker's ~/.pi/agent/npm/node_modules, pi-subagents 0.68.0 for pi 0.85.1; "
                                        "a scratch `npm install --legacy-peer-deps pi-subagents@0.78.0 pi-intercom` tree for pi 1.1.0)")
    p.set_defaults(func=prepare)
    p = sub.add_parser("run")
    p.add_argument("--name", required=True, help="sandbox name, pim-*")
    p.add_argument("--flavour", choices=FLAVOURS, required=True)
    p.add_argument("--mode", choices=MODES, required=True)
    p.add_argument("--model", help="default gpt-6-luna (pi) / openai-codex/gpt-6-luna (omp), the qualification models")
    p.add_argument("--image", required=True)
    p.add_argument("--policy", required=True, help="rendered OpenShell policy YAML (harness host chatgpt.com:443)")
    p.add_argument("--tarball", required=True, help="job tarball from `prepare`")
    p.add_argument("--plugins-tarball", required=True)
    p.add_argument("--pi-npm-tarball", help="prepare --pi-npm-out (pi mech only)")
    p.add_argument("--prepare-summary", help="prepare's JSON output (the code phrases checks.py looks for)")
    p.add_argument("--pi-auth", default="~/.aos/worker/pi-auth.json", help="the worker's Pi-schema auth.json (snapshotted, never printed)")
    p.add_argument("--state", required=True, help="private scratch dir for files staged for upload")
    p.add_argument("--downloads", required=True)
    p.add_argument("--evidence", required=True)
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
