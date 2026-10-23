import contextlib
import importlib.util
import io
import json
import os
import tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[4]
scratch_parent = os.environ.get('MHM_SCRATCH') or None
if scratch_parent: Path(scratch_parent).mkdir(parents=True, exist_ok=True)
base = Path(tempfile.mkdtemp(prefix='mhm-shared-', dir=scratch_parent))
base.mkdir(parents=True,exist_ok=True)
os.environ['AOS_AGENT_STATE_DIR']=str(base/'state')
os.environ['AOS_AGENT_RUN_FILE']=str(base/'run.json')
os.environ['AOS_AGENT_HOOK_INTERVAL']='5'
(base/'state/AO-7-r1.jsonl').unlink(missing_ok=True)
(base/'run.json').write_text(json.dumps(dict(run_id='AO-7-r1',task_id='AO-7',claim_token='fake-shared-token',bus_url='http://unused.invalid')))
spec=importlib.util.spec_from_file_location('review_agent',root/'docker/harness/aos_agent.py')
a=importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
pending=[dict(id='operator-i1',task_id='operator',kind='request',body='Instruction for main session',reply_to=None,framed='Instruction for main session')]
class Bus:
    def __init__(self,*args):pass
    def inbox(self):return {'pending_requests':list(pending)}
    def ack(self,ident):pending.clear()
a.Bus=Bus
def hook(fn,payload):
    output=io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stdin(io.StringIO(json.dumps(payload))) if hasattr(contextlib,'redirect_stdin') else contextlib.nullcontext():
        import sys
        old=sys.stdin; sys.stdin=io.StringIO(json.dumps(payload))
        try: fn()
        finally: sys.stdin=old
    return output.getvalue()
for name,fn,payload in [('child inbox',a.hook_inbox,{'session_id':'child'}),('main inbox',a.hook_inbox,{'session_id':'main'}),('child stop',a.hook_stop,{'session_id':'child','stop_hook_active':False}),('main stop',a.hook_stop,{'session_id':'main','stop_hook_active':False})]:
    print(f'{name}: output={bool(hook(fn,payload))}')
print('ledger',a.Ledger('AO-7-r1').events())
