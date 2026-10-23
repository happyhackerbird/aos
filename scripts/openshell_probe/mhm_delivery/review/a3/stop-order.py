"""Run the existing stop helper with ONLY D32(e)'s output/state reorder applied."""
import ast
import contextlib
import importlib.util
import io
import json
import os
import tempfile
from pathlib import Path
import sys

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parents[5]
path = root / 'docker/harness/aos_agent.py'
spec = importlib.util.spec_from_file_location('a3stop', path)
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
tree = ast.parse(path.read_text())
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_hook_stop')
record = next(n for n in fn.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
              and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == 'append')
fn.body.remove(record)
fn.body.insert(-1, record)
exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), 'exec'), a.__dict__)
scratch_parent = os.environ.get('MHM_SCRATCH') or None
if scratch_parent: Path(scratch_parent).mkdir(parents=True, exist_ok=True)
directory = Path(tempfile.mkdtemp(prefix='mhm-stop-order-', dir=scratch_parent))
os.environ['AOS_AGENT_STATE_DIR'] = str(directory)
a.load_run = lambda: {'run_id': 'AO-7-r1'}
ledger = a.Ledger('AO-7-r1')
Path(ledger.path).unlink(missing_ok=True)
ledger.acked({'id':'operator-i1','kind':'request','task_id':'operator','body':'unanswered question'})
def failed_send(message):
    raise OSError('probe output write failed')
a.send = failed_send
a.sys.stdin = io.StringIO('{"stop_hook_active":false}')
output, errors = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
    status = a.hook_stop()
print(json.dumps({'probe': 'D32(e) reordered stop output failure', 'exit': status,
                  'stdout': output.getvalue(), 'stderr': errors.getvalue().strip(),
                  'stopBlocked': sum(e['event']=='stop_blocked' for e in ledger.events())}))
