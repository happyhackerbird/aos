import contextlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
sys.dont_write_bytecode=True
root=Path(__file__).resolve().parents[5]
scratch_parent = os.environ.get('MHM_SCRATCH') or None
if scratch_parent: Path(scratch_parent).mkdir(parents=True, exist_ok=True)
base = Path(tempfile.mkdtemp(prefix='mhm-a2-boundaries-', dir=scratch_parent))
spec=importlib.util.spec_from_file_location('a2bound',root/'docker/harness/aos_agent.py')
a=importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
class Clock:
    value=0.0
    def monotonic(self):return self.value
    def sleep(self,n):self.value+=n
    def time(self):return self.value
for scenario in ['late-unanswered','late-answer-and-acks']:
    clock=Clock(); a.time=clock
    class DeadlineBus:
        calls=0
        def wait(self,ident,server_timeout):
            self.calls+=1
            remaining=900-clock.value
            self.last_http_timeout=min(a.WAIT_CALL_TIMEOUT,math.ceil(remaining)+15)
            if self.calls<15:
                clock.value+=server_timeout
                return {'intent':{'state':'stored'}}
            if scenario=='late-unanswered':
                clock.value+=self.last_http_timeout
                return {'intent':{'state':'stored'}}
            clock.value+=59
            return {'intent':{'state':'answered'},'replies':[{'id':'operator-reply','body':'answer'}]}
        def ack(self,ident):
            clock.value+=a.TOOL_TIMEOUT
            raise a.BusError('ack connection timed out')
    bus=DeadlineBus()
    answer=a.tool_wait(bus,None,{'id':'operator-i1','timeout_s':900})
    print(json.dumps(dict(probe='D21-D24',scenario=scenario,elapsed=clock.value,last_http_timeout=bus.last_http_timeout,wait_calls=bus.calls,ack_failures=answer.count('ack failed'))))

directory=base/'ledger-cap'; directory.mkdir(exist_ok=True)
os.environ['AOS_AGENT_STATE_DIR']=str(directory)
ledger=a.Ledger('AO-7-r1'); Path(ledger.path).unlink(missing_ok=True)
for i in range(400):
    ledger.append({'event':'hook','at':i*5})
    ledger.acked({'id':f'operator-i{i}','task_id':'operator','kind':'request','reply_to':None,'body':'x'*200})
ledger.append({'event':'stop_blocked'})
for i in range(400):ledger.append({'event':'replied','id':f'operator-i{i}'})
raw=Path(ledger.path).read_bytes()
def counts(data):
    events=[]
    for line in data.splitlines():
        try:events.append(json.loads(line))
        except ValueError:pass
    return {kind:sum(e.get('event')==kind for e in events) for kind in ['hook','acked','replied','stop_blocked']}
print(json.dumps(dict(probe='D18',ledger_bytes=len(raw),full=counts(raw),first64KiB=counts(raw[:65536]),last64KiB=counts(raw[-65536:]),body_in_capped_bytes=b'"body"' in raw[:65536])))

codeword='INJECTED-MID-RUN'
case=Path(os.environ.get('MHM_EVIDENCE', str(root/'docs/probes/2026-10-17-multi-harness-delivery')))/'smoke-codex'
if (case/'harness.out').is_file() and (case/'fake.log').is_file():
    stdout=(case/'harness.out').read_text(); requests=(case/'fake.log').read_text()
    print(json.dumps(dict(probe='D12-D19',case='smoke-codex',stdout_lines=len(stdout.splitlines()),stdout_has_frame='operator-message' in stdout,stdout_has_codeword=codeword in stdout,provider_requests_have_codeword=codeword in requests)))
else:
    print("D12-D19: skipped (raw smoke-codex evidence absent; the fact is the probe record's F124; regenerate with sh /probe/run-codex.sh smoke-codex in the kit)")

noop=base/'noop.ts'; noop.write_text('export default function adapter(pi: any) {}\n')
ready=subprocess.run(['node','-e',f"import({json.dumps(str(noop))}).then(m => process.exit(typeof m.default === 'function' ? 0 : 3))"],capture_output=True,text=True)
listing=subprocess.run([sys.executable,str(root/'docker/harness/aos_agent.py'),'mcp'],input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',capture_output=True,text=True)
names=sorted(t['name'] for t in json.loads(listing.stdout)['result']['tools'])
print(json.dumps(dict(probe='D25',noop_extension_readiness_exit=ready.returncode,noop_registered_handlers=0,pre_SU1_agent_tool_names=names)))
