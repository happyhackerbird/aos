"""Count the run's claim token (read from the bus DB, never printed) and a control word in every artifact."""
import json, sqlite3, subprocess, sys
from pathlib import Path
L = Path(sys.argv[1]); control = json.loads((L / "words.json").read_text())["note"].encode()
db = sqlite3.connect(next((L / "bus-data").glob("*.db")))
db.row_factory = sqlite3.Row
token = db.execute("SELECT claim_token FROM task_runs WHERE id='AO-1-r1'").fetchone()["claim_token"].encode()
print("token length", len(token))
def count(name, data):
    print(f"{name}: token {data.count(token)}, control {data.count(control)}")
run = dict(db.execute("SELECT * FROM task_runs WHERE id='AO-1-r1'").fetchone()); run.pop("claim_token")
count("bus runs row (claim_token column removed)", json.dumps(run).encode())
count("bus ledger rows", json.dumps([dict(r) for r in db.execute("SELECT * FROM task_intents")]).encode())
count("bus runs.jsonl", b"".join(p.read_bytes() for p in (L / "bus-data").glob("*.jsonl")))
repo = L / "repos" / "aos-example"
count("git log -p main..ao/AO-1", subprocess.run(["git", "-C", str(repo), "log", "-p", "main..ao/AO-1"], capture_output=True).stdout)
objs = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch-all-objects", "--batch"], capture_output=True).stdout
count(f"object store ({len(objs)} bytes)", objs)
for root in ("openshell-state", "logs"):
    files = [p for p in (L / root).rglob("*") if p.is_file()]
    count(f"{root}/ ({len(files)} files)", b"".join(p.read_bytes() for p in files))
