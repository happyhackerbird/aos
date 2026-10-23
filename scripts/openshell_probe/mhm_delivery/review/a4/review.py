"""A4 read-only source review probes; generated state stays under MHM_SCRATCH or a temporary directory."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[5]
scratch_parent = os.environ.get('MHM_SCRATCH') or None
if scratch_parent: Path(scratch_parent).mkdir(parents=True, exist_ok=True)
BASE = Path(tempfile.mkdtemp(prefix='mhm-a4-review-', dir=scratch_parent))

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

a = module("a4helper", ROOT / "docker/harness/aos_agent.py")
s = module("a4store", ROOT / "agent_os/store.py")
handshake = '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
for form in ("mcp", "serve"):
    result = subprocess.run([sys.executable, str(ROOT / "docker/harness/aos_agent.py"), form],
                            input=handshake, text=True, capture_output=True)
    names = [] if not result.stdout else [x["name"] for x in json.loads(result.stdout)["result"]["tools"]]
    print(json.dumps(dict(probe="D50 command", form=form, exit=result.returncode, names=names,
                          stderr=result.stderr.strip())))

store = s.Store(BASE / "bus")
task = store.create_task(repo="a4-disposable", title="ownership probe", brief="probe", acceptance=["true"])
store.ready_task(task["id"])
run = store.claim_task(task["id"], worker="a4", worker_host="mac", lease_seconds=3600,
                       expected_spec_hash=task["spec_hash"], effective_acceptance=task["acceptance"],
                       acceptance_source="task")
store.start_run(run["id"], run["claim_token"], base_commit="a" * 40, branch="probe", worktree="probe")
row = store.create_operator_intent(to_task_id=task["id"], body="Instruction intended for the report-producing agent")
credential = dict(run_id=run["id"], task_id=task["id"], claim_token=run["claim_token"], bus_url="http://unused.invalid")
os.environ["AOS_AGENT_STATE_DIR"] = str(BASE / "ledger")
a.load_run = lambda: credential
class StoreBus:
    def __init__(self, *args): pass
    def inbox(self):
        return {"pending_requests": store.requests_for_run(run["id"], run["claim_token"])}
    def ack(self, ident):
        return store.ack_intent(ident, run["id"], run["claim_token"])
    def reply(self, ident, text):
        return store.create_intent(task["id"], run["id"], run["claim_token"], kind="request", reply_to=ident, body=text)
a.Bus = StoreBus
def mcp_call(name, arguments):
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"jsonrpc":"2.0", "id":1, "method":"tools/call",
                                       "params":{"name":name, "arguments":arguments}}) + "\n")
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            a.serve()
    finally:
        sys.stdin = old_stdin
    return json.loads(output.getvalue())["result"]
child_result = mcp_call("aos_inbox", {})
main_result = a.run_tool("aos_inbox", {})
print(json.dumps(dict(probe="D52 shared MCP consumer", child_received_frame="operator-message" in str(child_result),
                      parent_result=main_result["content"][0]["text"], bus_state=store.get_intent(row["id"])["state"],
                      reach="actual run_tool and real Store, shared run credential; no pinned-harness child executed")))

def stop(payload):
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            a.hook_stop()
    finally:
        sys.stdin = old_stdin
    return output.getvalue()

first = stop({"stop_hook_active": False})
answered_first = a.run_tool("aos_reply", {"id":row["id"], "text":"First question answered"})
late = store.create_operator_intent(to_task_id=task["id"], body="A new question received during the continuation")
received_late = a.run_tool("aos_inbox", {})
second = stop({"stop_hook_active": True})
second_without_flag = stop({"stop_hook_active": False})
print(json.dumps(dict(probe="D49 new request after first stop", first_block=bool(first),
                      first_question_answered=store.get_intent(row["id"])["state"] == "answered",
                      late_question_received="new question" in str(received_late),
                      late_bus_state=store.get_intent(late["id"])["state"],
                      second_block_with_D49_flag=bool(second), second_block_without_flag=bool(second_without_flag),
                      reach="real Store, real helper stop path, and the literal D49 stop_hook_active payload; no failure injected")))

for shape in ("hooks", "config", "directory_alias", "leaf_alias", "plain_codex", "clean"):
    directory = BASE / shape
    directory.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=directory, text=True, capture_output=True, check=True).stdout.strip()
    git("init", "-q")
    if shape == "directory_alias":
        (directory / "settings").mkdir()
        (directory / "settings/hooks.json").write_text("{}")
        (directory / ".codex").symlink_to("settings")
    elif shape == "plain_codex":
        (directory / ".codex").write_text("plain")
    else:
        (directory / ".codex").mkdir()
        if shape == "hooks": (directory / ".codex/hooks.json").write_text("{}")
        elif shape == "config": (directory / ".codex/config.toml").write_text("x=1")
        elif shape == "leaf_alias":
            (directory / "outside.json").write_text("{}")
            (directory / ".codex/hooks.json").symlink_to("../outside.json")
        else: (directory / ".codex/ordinary").write_text("clean")
    git("add", ".")
    tree = git("write-tree")
    listing = git("ls-tree", "--full-tree", tree, "--", ".codex", ".codex/config.toml", ".codex/hooks.json")
    parsed = [(line.split()[0], line.split("\t", 1)[1]) for line in listing.splitlines()]
    refused = any(path in (".codex/config.toml", ".codex/hooks.json") or
                  (path == ".codex" and mode != "040000") for mode, path in parsed)
    print(json.dumps(dict(probe="D23 new guard", shape=shape, refused=refused)))

for candidate in ("1.9", "2.0", "2.1", "3.0"):
    major, minor = map(int, candidate.split("."))
    print(json.dumps(dict(probe="D46 literal compatibility predicate", image=candidate,
                          runner="2.0", admitted=major == 2 and minor >= 0)))
print(json.dumps(dict(probe="D46 lower minor", image="2.0", runner="2.1", admitted=(2 == 2 and 0 >= 1))))

evidence = Path(os.environ.get('MHM_EVIDENCE', str(ROOT/'docs/probes/2026-10-17-multi-harness-delivery')))
if (evidence/'smoke-codex/harness.out').is_file() and (evidence/'smoke-codex/fake.log').is_file():
    codex_stdout = (evidence / "smoke-codex/harness.out").read_text()
    codex_requests = (evidence / "smoke-codex/fake.log").read_text()
    print(json.dumps(dict(probe="D47 recorded stream", stdout_has_frame="operator-message" in codex_stdout,
                          stdout_has_word="INJECTED-MID-RUN" in codex_stdout,
                          provider_requests_have_word="INJECTED-MID-RUN" in codex_requests)))
else:
    print("D47: skipped (raw smoke-codex evidence absent; the fact is the probe record's F124)")
if (evidence/'a2-omp-instance/fires.jsonl').is_file():
    owner_events = [json.loads(line) for line in (evidence / "a2-omp-instance/fires.jsonl").read_text().splitlines()]
    starts = [e for e in owner_events if e.get("event") == "session_start"]
    print(json.dumps(dict(probe="D52 recorded omp instance", sessions=len({e.get("session_id") for e in starts}),
                          instances=len({e.get("instance") for e in starts}))))
else:
    print("D52: skipped (raw a2-omp-instance evidence absent; the fact is the probe record's F129)")
for case in ("co-omp-aoslast", "co-omp-aosfirst", "co-pi-aoslast", "co-pi-aosfirst"):
    if (evidence/case/'fake.log').is_file():
        rows = [json.loads(line) for line in (evidence / case / "fake.log").read_text().splitlines()]
        request = next(r for r in rows if r.get("n") == 1)
        content = request["messages"][3]["content"]
        text = content if isinstance(content, str) else json.dumps(content)
        note, frame = text.index("[pi-probe] note"), text.index("<operator-message")
        print(json.dumps(dict(probe="F160 recorded provider input", case=case,
                              both_blocks=True, frame_last=frame > note)))
    else:
        print(f"F160: skipped (raw {case} evidence absent; the fact is the probe record's F160)")
if (evidence/'claude-payloads/hooks.jsonl').is_file():
    payloads = [json.loads(line) for line in (evidence / "claude-payloads/hooks.jsonl").read_text().splitlines()]
    for payload in payloads:
        print(json.dumps(dict(probe="D52 predicate on F166 payload", event=payload.get("hook_event_name"),
                              tool=payload.get("tool_name"), skip=payload.get("agent_id") is not None)))
else:
    print("D52: skipped (raw claude-payloads evidence absent; the fact is the probe record's F166)")
