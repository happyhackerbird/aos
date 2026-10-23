from __future__ import annotations
import ast
import dataclasses
import importlib.util
import json
import os
import tempfile
from pathlib import Path
import sys
import time
import types

root=Path(__file__).resolve().parents[4]
scratch_parent = os.environ.get('MHM_SCRATCH') or None
if scratch_parent: Path(scratch_parent).mkdir(parents=True, exist_ok=True)
base = Path(tempfile.mkdtemp(prefix='mhm-retention-', dir=scratch_parent))
# Execute the checked-in functions. Omit pi.py's unrelated policy import to avoid installing dependencies.
tree=ast.parse((root/'agent_os/pi.py').read_text())
tree.body=[node for node in tree.body if not(isinstance(node,ast.ImportFrom) and node.module=='agent_os')]
p=types.ModuleType('review_pi'); sys.modules[p.__name__]=p
exec(compile(tree,'agent_os/pi.py','exec'),p.__dict__)
sid='00000000-0000-4000-8000-000000000001'
claim='review-retained-claim-token'; worker='review-worker-secret'
raw='\n'.join(map(json.dumps,[{'type':'session','id':sid},
 {'type':'agent_end','messages':[{'role':'assistant','content':[{'type':'text','text':f'ordinary output {claim} {worker}'}],'stopReason':'stop'}]},
 {'type':'agent_settled','aborted':False}])).encode()+b'\n'
tree=ast.parse((root/'agent_os/openshell_runner.py').read_text())
functions=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in ('collect_pi','_write_private')]
module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*functions],type_ignores=[])
ast.fix_missing_locations(module)
namespace=dict(pi=p,Path=Path,json=json,os=__import__('os'),sandbox_exec_bytes=lambda *a,**k:types.SimpleNamespace(returncode=0,stdout=raw))
exec(compile(module,'agent_os/openshell_runner.py','exec'),namespace)
auth=p.PiAuth(worker,'account',0,'declared',(worker,))
result,events,diagnostics=namespace['collect_pi']('unused',types.SimpleNamespace(flavour='pi',auth=auth),types.SimpleNamespace(expected_session_id=sid),0,base,'AO-7-r1',sid)
retained=events.read_text()
print(f'collect_pi: parser_error={result.error} claim_token_retained={claim in retained} worker_token_retained={worker in retained} mode={oct(events.stat().st_mode & 0o777)}')

spec=importlib.util.spec_from_file_location('review_wait_agent',root/'docker/harness/aos_agent.py')
a=importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
class DelayedBus:
    def wait(self,ident,requested):
        print(f'tool_wait: server_timeout_requested={requested} local_HTTP_timeout={a.WAIT_CALL_TIMEOUT}')
        time.sleep(1.25)
        return {'intent':{'state':'stored'}}
started=time.monotonic()
text=a.tool_wait(DelayedBus(),None,{'id':'operator-i1','timeout_s':1})
print(f'tool_wait: requested_budget=1 elapsed={time.monotonic()-started:.2f} result={text!r}')
class Clock:
    value=0
    def monotonic(self):return self.value
    def sleep(self,n):self.value+=n
clock=Clock(); a.time=clock
class DelayedFinalCall:
    calls=0
    def wait(self,ident,requested):
        self.calls+=1
        clock.value+=requested+(15 if self.calls==15 else 0)
        return {'intent':{'state':'stored'}}
bus=DelayedFinalCall()
text=a.tool_wait(bus,None,{'id':'operator-i1','timeout_s':900})
print(f'virtual_budget=900 elapsed={clock.value} calls={bus.calls} final_request_HTTP_limit={a.WAIT_CALL_TIMEOUT} result={text!r}')
