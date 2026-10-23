#!/usr/bin/env python3
"""The probe's Codex hook: one command for every event in hooks.json.

Appends one JSON line per fire to $HOME/.local/share/codex-probe/fires.jsonl (HOME inside an
OpenShell sandbox is /sandbox, measured 2026-10-14). On SessionStart it also measures the sandbox
boundary from the hook's own process (a curl to example.com, a /usr write, the token entries in
its environment, a state write under ~/.local/state) and records the results in the same line.
On PreToolUse it denies any tool call whose input mentions HOOK-DENY-ME, in the documented JSON
form (hookSpecificOutput.permissionDecision = deny). On Stop it blocks exactly once (marker file),
so the record can show the block reaching the model without an endless loop. Everything else is
silent: exit 0, no stdout.
"""
import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
LOG_DIR = os.path.join(HOME, ".local", "share", "codex-probe")
LOG = os.path.join(LOG_DIR, "fires.jsonl")
STOP_MARK = os.path.join(LOG_DIR, "stop-blocked-once")
DENY_MARK = "HOOK-DENY-ME"


def boundary() -> dict:
    out: dict = {"home": HOME, "cwd": os.getcwd(), "uid": os.getuid()}
    out["token_entries"] = sum(1 for k in os.environ if k == "CLAUDE_CODE_OAUTH_TOKEN")
    out["codex_env_keys"] = sorted(k for k in os.environ if k.startswith("CODEX"))
    try:
        got = subprocess.run(["curl", "-sS", "--max-time", "8", "https://example.com"], capture_output=True, text=True, timeout=20)
        out["curl_rc"] = got.returncode
        out["curl_err"] = got.stderr.strip()[:160]
    except Exception as exc:  # noqa: BLE001
        out["curl_rc"] = f"exception {type(exc).__name__}"
    try:
        with open("/usr/codex-probe-mark", "w") as fh:
            fh.write("x")
        out["usr_write"] = "ok (unexpected)"
    except OSError as exc:
        out["usr_write"] = f"errno {exc.errno} {exc.strerror}"
    state = os.path.join(HOME, ".local", "state", "codex-probe")
    try:
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "mark"), "w") as fh:
            fh.write("ok\n")
        out["state_write"] = "ok " + state
    except OSError as exc:
        out["state_write"] = f"errno {exc.errno} {exc.strerror}"
    return out


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {"unparsed": raw[:200]}
    event = payload.get("hook_event_name") or "?"
    line = {"t": round(time.time(), 3), "event": event, "tool_name": payload.get("tool_name"), "model": payload.get("model"),
            "session_id": payload.get("session_id"), "agent_id": payload.get("agent_id"), "agent_type": payload.get("agent_type"),
            "stop_hook_active": payload.get("stop_hook_active"), "argv0": sys.argv[0],
            "input_head": json.dumps(payload.get("tool_input"))[:200] if "tool_input" in payload else None,
            "keys": sorted(payload)}
    decision = None
    if event == "SessionStart":
        line["boundary"] = boundary()
    elif event == "PreToolUse" and DENY_MARK in json.dumps(payload.get("tool_input")):
        decision = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "[codex-probe] denied by the probe hook (HOOK-DENY-ME)"}}
        line["decision"] = "deny"
    elif event == "Stop" and not os.path.exists(STOP_MARK):
        os.makedirs(LOG_DIR, exist_ok=True)
        open(STOP_MARK, "w").close()
        decision = {"decision": "block", "reason": "[codex-probe] stop blocked once by the probe hook: reply with the single line "
                                                  "STOP-HOOK-SEEN plus your final message, then stop"}
        line["decision"] = "block-once"
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError:
        pass
    if decision is not None:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
