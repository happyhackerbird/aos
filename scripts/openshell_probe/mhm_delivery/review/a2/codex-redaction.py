import ast
import json
from pathlib import Path
root=Path(__file__).resolve().parents[5]
tree=ast.parse((root/'agent_os/openshell_runner.py').read_text())
fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='hide')
namespace={}
exec(compile(ast.Module(body=[fn],type_ignores=[]),'agent_os/openshell_runner.py','exec'),namespace)
claim='a2-synthetic-run-token'; worker='a2-synthetic-worker-access-token'
event={'type':'item.completed','item':{'type':'command_execution','aggregated_output':json.dumps({'tokens':{'access_token':worker}})+' '+claim}}
raw=json.dumps(event)
scrubbed=namespace['hide'](raw,claim)
print(json.dumps({'claim_token_retained':claim in scrubbed,'codex_worker_token_retained':worker in scrubbed,'uses_real_credentials':False}))
