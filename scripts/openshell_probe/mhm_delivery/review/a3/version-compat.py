"""An allowed protocol bump is not a backward compatibility assertion."""
import importlib.util
import json
import os
import tempfile
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode=True
root=Path(__file__).resolve().parents[5]
scratch_parent = os.environ.get('MHM_SCRATCH') or None
if scratch_parent: Path(scratch_parent).mkdir(parents=True, exist_ok=True)
base = Path(tempfile.mkdtemp(prefix='mhm-version-compat-', dir=scratch_parent))
spec=importlib.util.spec_from_file_location('a3protocol',root/'docker/harness/aos_agent.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
candidate=base/'protocol3.py'
candidate.write_text('import json,sys\n'
 'if sys.argv[1:]==["version"]: print("aos-agent protocol 3")\n'
 'elif sys.argv[1:]==["mcp"]:\n'
 f' print(json.dumps({{"jsonrpc":"2.0","id":1,"result":{{"tools":{a.TOOLS!r}}}}}))\n'
 'elif sys.argv[1:]==["inbox-v3","--hook"]: print("{}")\n'
 'else:\n print("usage: version | mcp | inbox-v3 --hook",file=sys.stderr)\n sys.exit(2)\n')
def call(*args,input=''):
    return subprocess.run([sys.executable,str(candidate),*args],input=input,text=True,capture_output=True)
version=call('version')
listing=call('mcp',input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
names=sorted(t['name'] for t in json.loads(listing.stdout)['result']['tools'])
accepted=version.stdout.strip().startswith('aos-agent protocol ') and int(version.stdout.split()[-1])>=2 and names==sorted(t['name'] for t in a.TOOLS)
hook=call('inbox','--hook',input='{}')
print(json.dumps({'probe':'D44 permitted newer subcommand change','version':version.stdout.strip(),
 'six_names':len(names),'readiness_admitted':accepted,'runner_hook_exit':hook.returncode,
 'hook_stdout':hook.stdout,'reach':'executable compatibility counterexample, not an existing production protocol 3'}))
