"""Prototype job runner for OpenShell (probe tooling, not the production adapter).

Drives the ``openshell`` CLI by subprocess to provision a sanitized job copy, run a
harness as the sandbox's main process with stdin closed, run acceptance as fresh
execs, extract results as a git bundle, cancel, and compare the effective policy
with the rendered one. Every subprocess argv is logged to stderr; environment
values are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

OPENSHELL = os.environ.get("OPENSHELL_BIN", "openshell")
CLAUDE_SETTINGS = {
    "permissions": {"defaultMode": "dontAsk",
                    "allow": ["Bash(*)", "Read(*)", "Edit(*)", "Write(*)", "Agent(*)"],
                    "deny": ["WebFetch", "WebSearch", "mcp__*"]},
    "sandbox": {"enabled": False},
    "disableAllHooks": True,
}
DEFAULT_ENV = ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", "DISABLE_AUTOUPDATER=1")
HARNESSES = ("claude", "codex", "pi")
RULE_NAME = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*$")


def run_cli(argv: list[str], *, check: bool = True, timeout: int | None = None,
            stdin=subprocess.DEVNULL) -> subprocess.CompletedProcess:
    """The single seam for subprocesses; tests monkeypatch this."""
    print("+ " + shlex.join(argv), file=sys.stderr)
    result = subprocess.run(argv, stdin=stdin, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} exited {result.returncode}: "
                           f"{result.stderr.strip()[-400:]}")
    return result


def openshell(*args: str, **kw) -> subprocess.CompletedProcess:
    return run_cli([OPENSHELL, *args], **kw)


def sandbox_exec(name: str, script: str, *, timeout: int = 60, check: bool = False) -> subprocess.CompletedProcess:
    return openshell("sandbox", "exec", "-n", name, "--no-tty", "--timeout", str(timeout), "--", "bash", "-lc", script,
                     check=check, timeout=timeout + 30)


# -- provisioning -----------------------------------------------------------

def provision(repo: str, excludes: list[str], out: str, ref: str | None = None) -> dict:
    """Clone without hardlinks, drop excluded paths, tar the copy with .git intact.

    `ref` checks out a branch or commit of the source (for example a fix branch fetched
    from an extracted bundle) so a fresh acceptance sandbox can verify exactly that tree.
    """
    with tempfile.TemporaryDirectory(prefix="aos-provision-") as scratch:
        clone = Path(scratch) / "job"
        run_cli(["git", "clone", "-q", "--no-hardlinks", repo, str(clone)])
        if ref:
            # A branch of the source is `origin/<ref>` in the clone; a commit id resolves as is.
            probe = run_cli(["git", "-C", str(clone), "rev-parse", "-q", "--verify", f"origin/{ref}^{{commit}}"], check=False)
            target = f"origin/{ref}" if probe.returncode == 0 else ref
            run_cli(["git", "-C", str(clone), "checkout", "-q", target, "--"])
        tracked = run_cli(["git", "-C", str(clone), "ls-files", "-z"]).stdout.split("\0")
        tracked_set = {entry for entry in tracked if entry}
        removed: list[str] = []
        for pattern in excludes:
            for path in sorted(clone.glob(pattern), reverse=True):
                if path == clone or ".git" in path.relative_to(clone).parts:
                    continue
                if not path.exists() and not path.is_symlink():
                    continue
                rel = path.relative_to(clone).as_posix()
                hit = next((entry for entry in sorted(tracked_set) if entry == rel or entry.startswith(rel + "/")), None)
                if hit is not None:
                    # A tracked file stays readable through the object store (git show), and a
                    # sanitized copy makes `git add -A` record its deletion; refuse at admission.
                    raise SystemExit(f"policy: exclude {pattern!r} matches tracked path {hit}; "
                                     "move it out of the repository history or drop the entry")
                removed.append(rel)
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        files = sum(1 for p in clone.rglob("*") if p.is_file() or p.is_symlink())
        with tarfile.open(out, "w:gz") as tar:
            for child in sorted(clone.iterdir()):
                tar.add(child, arcname=child.name)
    return {"tarball": out, "files": files, "excluded": sorted(set(removed))}


# -- the main script ---------------------------------------------------------

def harness_command(harness: str, model: str | None) -> str:
    if harness == "claude":
        return ('claude -p "$(cat /sandbox/prompt.txt)" --output-format json --setting-sources "" '
                '--settings /sandbox/claude-settings.json')
    if harness == "codex":
        return ('CODEX_HOME=/sandbox/codex-home codex exec --json --skip-git-repo-check --sandbox danger-full-access '
                '-c approval_policy="never" "$(cat /sandbox/prompt.txt)"')
    if harness == "pi":
        if not model:
            raise ValueError("pi needs --model provider/id")
        return (f'pi -p "$(cat /sandbox/prompt.txt)" --mode json --model {shlex.quote(model)} '
                '--no-session --no-extensions --no-skills')
    raise ValueError(f"unknown harness {harness!r}")


def build_main_script(harness: str, model: str | None = None) -> str:
    return "\n".join([
        "set -u",
        "mkdir -p /tmp/claude-home /tmp/pi-agent /sandbox/codex-home /sandbox/incoming",
        "export CLAUDE_CONFIG_DIR=/tmp/claude-home PI_CODING_AGENT_DIR=/tmp/pi-agent CODEX_HOME=/sandbox/codex-home",
        "while [ ! -f /sandbox/job.tgz ]; do sleep 1; done",
        "mkdir -p /sandbox/repo && tar xzf /sandbox/job.tgz -C /sandbox/repo",
        "cd /sandbox/repo",
        harness_command(harness, model) + " </dev/null >/sandbox/harness.out 2>/sandbox/harness.err",
        # Stop every process the harness left behind (detached servers, databases, browsers)
        # before signalling completion, so a fresh acceptance exec starts from a clean process table.
        'rc=$?; ' + 'for p in $(pgrep -u "$(id -u)"); do case "$p" in 1|$$|$PPID) ;; *) kill "$p" 2>/dev/null || true;; esac; done; sleep 1; for p in $(pgrep -u "$(id -u)"); do case "$p" in 1|$$|$PPID) ;; *) kill -9 "$p" 2>/dev/null || true;; esac; done' + '; echo $rc > /sandbox/harness.rc',
        "sleep 86400",
    ]) + "\n"


def build_accept_script(commands: list[str]) -> str:
    """Main script of a fresh acceptance sandbox: extract the job tree, run each command, record results."""
    steps = []
    for index, command in enumerate(commands):
        steps.append(f"( cd /sandbox/repo && {command} ) </dev/null >/sandbox/accept-{index}.out 2>&1; "
                     f"echo $? > /sandbox/accept-{index}.rc")
    return "\n".join([
        "set -u",
        "mkdir -p /sandbox/incoming",
        "while [ ! -f /sandbox/job.tgz ]; do sleep 1; done",
        "mkdir -p /sandbox/repo && tar xzf /sandbox/job.tgz -C /sandbox/repo",
        *steps,
        "touch /sandbox/accept.done",
        "sleep 86400",
    ]) + "\n"


def accept_fresh(args: argparse.Namespace) -> int:
    """Independent acceptance: a new sandbox from the image and policy, the tree from the tarball, no harness."""
    script = build_accept_script(args.command)
    create(args.name, args.image, args.policy, providers=[], env=list(DEFAULT_ENV), cpu=args.cpu,
           memory=args.memory, script=script)
    wait_for_file(args.name, "/sandbox/incoming", 120, interval=2)
    upload(args.name, args.tarball, "/sandbox/incoming/job.tgz")
    sandbox_exec(args.name, "mv /sandbox/incoming/job.tgz /sandbox/job.tgz", check=True)
    wait_for_file(args.name, "/sandbox/accept.done", args.timeout)
    failed = 0
    for index, command in enumerate(args.command):
        result = sandbox_exec(args.name, f"cat /sandbox/accept-{index}.rc; echo; tail -n 20 /sandbox/accept-{index}.out",
                              timeout=60)
        rc_line, _, tail = result.stdout.partition("\n")
        rc = int(rc_line.strip() or "1")
        print(json.dumps({"command": command, "rc": rc, "tail": tail.strip()}, indent=2))
        failed += rc != 0
    return 1 if failed else 0


# -- lifecycle ------------------------------------------------------------------

def create(name: str, image: str, policy: str, *, providers: list[str], env: list[str], cpu: str, memory: str,
           script: str) -> None:
    argv = ["sandbox", "create", "--name", name, "--from", image, "--policy", policy]
    for provider in providers:
        argv += ["--provider", provider]
    for entry in env:
        argv += ["--env", entry]
    argv += ["--cpu", cpu, "--memory", memory, "--detach", "--no-tty", "--", "bash", "-lc", script]
    openshell(*argv, timeout=600)


def upload(name: str, local: str, dest: str) -> None:
    openshell("sandbox", "upload", name, local, dest, timeout=600)


def wait_for_file(name: str, path: str, timeout: int, interval: int = 5) -> float:
    started = time.monotonic()
    while True:
        result = sandbox_exec(name, f"test -e {shlex.quote(path)}", timeout=20)
        if result.returncode == 0:
            return time.monotonic() - started
        if time.monotonic() - started > timeout:
            raise TimeoutError(f"{path} did not appear within {timeout} s")
        time.sleep(interval)


def run(args: argparse.Namespace) -> int:
    script = build_main_script(args.harness, args.model)
    env = list(DEFAULT_ENV) + list(args.env or [])
    create(args.name, args.image, args.policy, providers=args.provider or [], env=env, cpu=args.cpu,
           memory=args.memory, script=script)
    with tempfile.TemporaryDirectory(prefix="aos-run-") as scratch:
        settings = Path(scratch) / "claude-settings.json"
        settings.write_text(json.dumps(CLAUDE_SETTINGS, indent=2))
        # `openshell sandbox upload` treats a file inside a git work tree as a filtered tree upload and
        # would turn /sandbox/prompt.txt into a directory; stage uploads from the scratch dir instead.
        prompt = Path(scratch) / "prompt.txt"
        prompt.write_bytes(Path(args.prompt).read_bytes())
        wait_for_file(args.name, "/sandbox/incoming", 120, interval=2)
        upload(args.name, str(prompt), "/sandbox/prompt.txt")
        upload(args.name, str(settings), "/sandbox/claude-settings.json")
        if args.codex_auth:
            auth = Path(scratch) / "auth.json"
            auth.write_bytes(Path(args.codex_auth).read_bytes())
            os.chmod(auth, 0o600)
            upload(args.name, str(auth), "/sandbox/codex-home/auth.json")
            sandbox_exec(args.name, "chmod 600 /sandbox/codex-home/auth.json", check=True)
        upload(args.name, args.tarball, "/sandbox/incoming/job.tgz")
        sandbox_exec(args.name, "mv /sandbox/incoming/job.tgz /sandbox/job.tgz", check=True)
    elapsed = wait_for_file(args.name, "/sandbox/harness.rc", args.timeout)
    result = sandbox_exec(args.name, "cat /sandbox/harness.rc; echo; tail -c 2000 /sandbox/harness.err", timeout=30)
    rc_line, _, err_tail = result.stdout.partition("\n")
    print(json.dumps({"sandbox": args.name, "harness": args.harness, "harness_rc": rc_line.strip(),
                      "seconds": round(elapsed, 1), "stderr_tail": err_tail[-800:]}, indent=2))
    return 0 if rc_line.strip() == "0" else 1


def accept(args: argparse.Namespace) -> int:
    failed = 0
    for command in args.command:
        result = sandbox_exec(args.name, f"cd /sandbox/repo && {command}", timeout=args.timeout)
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-20:])
        print(json.dumps({"command": command, "rc": result.returncode, "tail": tail}, indent=2))
        failed += result.returncode != 0
    return 1 if failed else 0


def extract(args: argparse.Namespace) -> int:
    sandbox_exec(args.name, f"cd /sandbox/repo && git bundle create /sandbox/out.bundle {shlex.quote(args.base)}..HEAD",
                 timeout=120, check=True)
    openshell("sandbox", "download", args.name, "/sandbox/out.bundle", args.out, timeout=300)
    print(json.dumps({"bundle": args.out, "bytes": os.path.getsize(args.out)}))
    return 0


def cancel(args: argparse.Namespace) -> int:
    started = time.monotonic()
    openshell("sandbox", "delete", args.name, check=False, timeout=120)
    while time.monotonic() - started < args.timeout:
        listing = openshell("sandbox", "list", check=False, timeout=60).stdout
        if not any(line.split() and line.split()[0] == args.name for line in listing.splitlines()):
            print(json.dumps({"sandbox": args.name, "gone_after_seconds": round(time.monotonic() - started, 1)}))
            return 0
        time.sleep(1)
    print(json.dumps({"sandbox": args.name, "error": "still listed"}))
    return 1


# -- effective policy -----------------------------------------------------------

def parse_policy_text(text: str) -> dict:
    """Minimal reader for the YAML shapes the renderer emits and `policy get --full` prints."""
    body = text.split("\n---\n", 1)[1] if "\n---\n" in text else text
    rules: dict[str, set[tuple[str, int]]] = {}
    current: str | None = None
    host: str | None = None
    landlock = user = None
    section = None
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if not line.startswith(" "):
            section = line.split(":")[0]
            current = None
            continue
        if section == "network_policies":
            match = RULE_NAME.match(line)
            if match:
                current = match.group(1)
                rules.setdefault(current, set())
                host = None
                continue
            stripped = line.strip()
            if stripped.startswith("- host:") or stripped.startswith("host:"):
                host = stripped.split(":", 1)[1].strip().strip('"\'')
            elif stripped.startswith("port:") and current and host:
                rules[current].add((host, int(stripped.split(":", 1)[1].strip())))
                host = None
        elif section == "landlock" and line.strip().startswith("compatibility:"):
            landlock = line.split(":", 1)[1].strip()
        elif section == "process" and line.strip().startswith("run_as_user:"):
            user = line.split(":", 1)[1].strip().strip('"')
    return {"rules": rules, "landlock": landlock, "run_as_user": user}


def compare_effective(expected_text: str, effective_text: str) -> list[str]:
    expected, effective = parse_policy_text(expected_text), parse_policy_text(effective_text)
    problems = []
    for key in ("landlock", "run_as_user"):
        if expected[key] != effective[key]:
            problems.append(f"{key}: expected {expected[key]!r}, effective {effective[key]!r}")
    for rule, endpoints in expected["rules"].items():
        if rule not in effective["rules"]:
            problems.append(f"rule {rule} missing from the effective policy")
        elif effective["rules"][rule] != endpoints:
            problems.append(f"rule {rule} endpoints differ: expected {sorted(endpoints)}, effective {sorted(effective['rules'][rule])}")
    for rule in effective["rules"]:
        if rule not in expected["rules"] and not rule.startswith("_provider_"):
            problems.append(f"unexpected rule {rule} in the effective policy")
    return problems


def effective(args: argparse.Namespace) -> int:
    text = openshell("policy", "get", args.name, "--full", timeout=60).stdout
    problems = compare_effective(Path(args.expect).read_text(), text)
    for line in problems:
        print(line)
    print(json.dumps({"sandbox": args.name, "match": not problems}))
    return 3 if problems else 0


# -- CLI ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prototype OpenShell job runner (probe tooling).")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("provision"); p.add_argument("--repo", required=True); p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--out", required=True); p.add_argument("--ref", help="branch or commit to check out in the job copy")
    p.set_defaults(func=lambda a: print(json.dumps(provision(a.repo, a.exclude, a.out, a.ref), indent=2)) or 0)
    p = sub.add_parser("accept-fresh", help="independent acceptance in a new sandbox built from the tarball")
    for flag in ("--name", "--image", "--policy", "--tarball"):
        p.add_argument(flag, required=True)
    p.add_argument("--command", action="append", required=True); p.add_argument("--cpu", default="2")
    p.add_argument("--memory", default="2Gi"); p.add_argument("--timeout", type=int, default=900)
    p.set_defaults(func=accept_fresh)
    p = sub.add_parser("run")
    for flag in ("--name", "--image", "--policy", "--tarball", "--prompt"):
        p.add_argument(flag, required=True)
    p.add_argument("--harness", choices=HARNESSES, required=True); p.add_argument("--provider", action="append")
    p.add_argument("--env", action="append"); p.add_argument("--model"); p.add_argument("--codex-auth")
    p.add_argument("--cpu", default="2"); p.add_argument("--memory", default="2Gi"); p.add_argument("--timeout", type=int, default=900)
    p.set_defaults(func=run)
    p = sub.add_parser("accept"); p.add_argument("--name", required=True); p.add_argument("--command", action="append", required=True)
    p.add_argument("--timeout", type=int, default=600); p.set_defaults(func=accept)
    p = sub.add_parser("extract"); p.add_argument("--name", required=True); p.add_argument("--base", required=True)
    p.add_argument("--out", required=True); p.set_defaults(func=extract)
    p = sub.add_parser("cancel"); p.add_argument("--name", required=True); p.add_argument("--timeout", type=int, default=120)
    p.set_defaults(func=cancel)
    p = sub.add_parser("effective"); p.add_argument("--name", required=True); p.add_argument("--expect", required=True)
    p.set_defaults(func=effective)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, TimeoutError, ValueError, OSError) as exc:
        print(f"run_job: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
