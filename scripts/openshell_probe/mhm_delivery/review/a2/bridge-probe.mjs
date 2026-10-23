import {mkdirSync, writeFileSync, readFileSync, chmodSync, rmSync, existsSync, mkdtempSync} from 'node:fs';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {tmpdir} from 'node:os';
const mode=process.argv[2]??'normal';
const here = dirname(fileURLToPath(import.meta.url));
const scratchParent = process.env.MHM_SCRATCH || tmpdir();
mkdirSync(scratchParent, {recursive:true});
const base = mkdtempSync(join(scratchParent, 'mhm-a2-bridge-'));
const dir=`${base}/case-${mode}`;
rmSync(dir,{recursive:true,force:true}); mkdirSync(dir,{recursive:true});
process.env.HOME=dir;
process.env.AOS_AGENT_BIN=join(here, 'hook-stub.py'); chmodSync(process.env.AOS_AGENT_BIN,0o755);
process.env.AOS_AGENT_STATE_DIR=`${dir}/ledger`;
process.env.AOS_AGENT_RUN_FILE=`${dir}/run.json`;
process.env.AOS_AGENT_HOOK_INTERVAL='0'; process.env.A2_MODE=mode;
writeFileSync(process.env.AOS_AGENT_RUN_FILE,JSON.stringify({run_id:'AO-7-r1',task_id:'AO-7',claim_token:'a2-fake-token',bus_url:'http://unused.invalid'}));
const {default:extension}=await import(new URL('../../aos_pi.ts', import.meta.url));
const handlers=new Map(), followups=[];
extension({on:(e,f)=>handlers.set(e,f),sendUserMessage:(t,o)=>followups.push({t,o}),sendMessage:()=>{}});
const ctx={sessionManager:{getSessionId:()=> 'parent',getSessionFile:()=> '/parent.jsonl'}};
await handlers.get('session_start')({},ctx);
const result=await handlers.get('tool_result')({toolName:'bash',content:[{type:'text',text:'ordinary tool output'}]},ctx);
const stop=handlers.get('turn_end');
const counts=[];
for(const stopReason of ['error','aborted','stop','stop']){
 const before=followups.length;
 await stop({message:{role:'assistant',stopReason},toolResults:[]},ctx);
 counts.push({stopReason,newFollowups:followups.length-before});
}
const ledger=existsSync(`${dir}/ledger/AO-7-r1.jsonl`)?readFileSync(`${dir}/ledger/AO-7-r1.jsonl`,'utf8').trim().split('\n').map(JSON.parse):[];
const fires=readFileSync(`${dir}/.local/share/aos-pi-probe/fires.jsonl`,'utf8').trim().split('\n').map(JSON.parse);
console.log(JSON.stringify({mode,contentBlocks:result?.content?.length??0,visibleFailureBlock:JSON.stringify(result??null).includes('inbound message delivery failed here'),acked:ledger.filter(e=>e.event==='acked').length,
 stopBlocked:ledger.filter(e=>e.event==='stop_blocked').length,followups:followups.length,stops:counts,
 spawns:fires.filter(e=>e.event==='spawn').map(e=>({args:e.args,status:e.status,signal:e.signal,stdoutChars:e.stdout_chars,stderr:e.stderr_head}))}));
