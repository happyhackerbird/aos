"""A model may execute the same public helper command that an adapter executes."""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
from pathlib import Path
import sys

sys.dont_write_bytecode=True
root=Path(__file__).resolve().parents[5]
spec=importlib.util.spec_from_file_location('a3origin',root/'docker/harness/aos_agent.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
scratch_parent = os.environ.get('MHM_SCRATCH') or None
if scratch_parent: Path(scratch_parent).mkdir(parents=True, exist_ok=True)
directory = Path(tempfile.mkdtemp(prefix='mhm-a3-origin-', dir=scratch_parent))
os.environ['AOS_AGENT_STATE_DIR']=str(directory)
a.load_run=lambda:{'run_id':'AO-7-r1'}
ledger=a.Ledger('AO-7-r1');Path(ledger.path).unlink(missing_ok=True)
class FakeBus:
    def __init__(self,*args):pass
    def inbox(self):return {'pending_requests':[{'id':'operator-i1','task_id':'operator',
      'kind':'request','reply_to':None,'body':'fresh word','framed':'<operator-message>fresh word</operator-message>'}]}
    def ack(self,ident):pass
a.Bus=FakeBus
a.sys.stdin=io.StringIO('{}')
output=io.StringIO()
with contextlib.redirect_stdout(output):
    rc=a.main(['inbox','--hook'])
events=ledger.events()
print(json.dumps({'probe':'model shell invokes aos-agent inbox --hook','exit':rc,
 'frame_read_by_caller':'fresh word' in output.getvalue(),
 'events':[e['event'] for e in events],
 'MCP_tool_event_count':sum(e['event']=='tool' for e in events),
 'reach':'actual helper hook path; D32(d) adds events only in the separate MCP tools/call path'}))
