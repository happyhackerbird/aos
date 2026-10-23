from pathlib import Path
import ast
import json
import sys
root=Path(__file__).resolve().parents[5]
sources=Path(__file__).resolve().parents[2]
matches=[]
for name in ['README.md','fake_bus.py','run-omp.sh','run-pi.sh','run-codex.sh','summarize.py']:
 for number,line in enumerate((sources/name).read_text().splitlines(),1):
  if 'probe-claim-token-9f3e2c0b-never-printed' in line:matches.append(f'{name}:{number}')
print(json.dumps({'D28_R7_scan_would_reject_preserved_fixture_files':matches}))
# All three run scripts silently use the 0-second probe throttle, as opposed to D24's 5 seconds.
for name in ['run-omp.sh','run-pi.sh','run-codex.sh']:
 lines=[(n,l) for n,l in enumerate((sources/name).read_text().splitlines(),1) if 'HOOK_INTERVAL=' in l]
 print(json.dumps({'runner':name,'throttle_assignment':lines}))
# Verify the phase ordering from the actual runner's AST, independently of prose line numbers.
tree=ast.parse((root/'agent_os/openshell_runner.py').read_text())
run=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run_task')
hits=[]
for n in ast.walk(run):
 if isinstance(n,ast.Call):
  text=ast.get_source_segment((root/'agent_os/openshell_runner.py').read_text(),n) or ''
  if ('/start' in text and text.startswith('client.post')) or (isinstance(n.func,ast.Name) and n.func.id in ('check_effective','upload_job')):
   hits.append((n.lineno,text.splitlines()[0]))
print(json.dumps({'D19_running_before_harness_release':sorted(hits)}))
