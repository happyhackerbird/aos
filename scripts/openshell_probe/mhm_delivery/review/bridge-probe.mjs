import { mkdirSync, writeFileSync, readFileSync, chmodSync, rmSync, mkdtempSync } from 'node:fs';
import { dirname, join } from 'node:path';
import {fileURLToPath} from 'node:url';
import {tmpdir} from 'node:os';
const here = dirname(fileURLToPath(import.meta.url));
const scratchParent = process.env.MHM_SCRATCH || tmpdir();
mkdirSync(scratchParent, {recursive:true});
const base = mkdtempSync(join(scratchParent, 'mhm-a1-bridge-'));
const mode = process.argv[2] || 'small';
if (!['small','large','stops'].includes(mode)) throw new Error('unknown probe mode');
const dir = `${base}/case-${mode}`;
rmSync(dir, { recursive: true, force: true });
mkdirSync(dir, { recursive: true });
// All probe-owned writes, including the spike's HOME-relative log, stay under scratch.
process.env.HOME = dir;
process.env.AOS_AGENT_BIN = join(here, 'hook-stub.py');
chmodSync(process.env.AOS_AGENT_BIN, 0o755);
process.env.AOS_AGENT_RUN_FILE = `${dir}/run.json`;
process.env.AOS_AGENT_STATE_DIR = `${dir}/ledger`;
process.env.AOS_AGENT_HOOK_INTERVAL = '0';
process.env.REVIEW_MODE = mode;
process.env.REVIEW_TOKEN = 'review-fake-claim-token';
writeFileSync(process.env.AOS_AGENT_RUN_FILE, JSON.stringify({run_id:'AO-7-r1',task_id:'AO-7',claim_token:process.env.REVIEW_TOKEN,bus_url:'http://unused.invalid'}));
const {default:aosPi} = await import(new URL(`../aos_pi.ts?mode=${mode}`, import.meta.url));
const handlers = new Map();
const followups = [];
aosPi({on:(e,fn)=>handlers.set(e,fn),sendUserMessage:(text,opts)=>followups.push({text,opts}),sendMessage:()=>{}});
const original = [{type:'text',text:'ordinary-tool-output'}];
const result = await handlers.get('tool_result')({toolName:'bash',content:original,isError:false});
console.log(JSON.stringify({mode,contentBlocks:result?.content?.length??0,tokenInToolResult:JSON.stringify(result??null).includes(process.env.REVIEW_TOKEN)}));
const ledger = () => readFileSync(`${dir}/ledger/AO-7-r1.jsonl`,'utf8').trim().split('\n').map(JSON.parse);
console.log(JSON.stringify({acked:ledger().filter(e=>e.event==='acked').length}));
if(mode === 'stops') {
  for (const stopReason of ['error','aborted','stop']) {
    const before=followups.length;
    await handlers.get('turn_end')({type:'turn_end',message:{role:'assistant',content:[],stopReason},toolResults:[]});
    console.log(JSON.stringify({stopReason,newFollowups:followups.length-before}));
  }
} else {
  await handlers.get('turn_end')({message:{role:'assistant',stopReason:'stop'},toolResults:[]});
  console.log(JSON.stringify({followups:followups.length,tokenInFollowup:JSON.stringify(followups).includes(process.env.REVIEW_TOKEN)}));
}
const fires=readFileSync(`${dir}/.local/share/aos-pi-probe/fires.jsonl`,'utf8').trim().split('\n').map(JSON.parse);
for(const e of fires.filter(e=>e.event==='spawn'||e.event==='spawn_unparsed'))
 console.log(JSON.stringify({event:e.event,args:e.args,status:e.status,signal:e.signal,stdoutChars:e.stdout_head?.length,stderr:e.stderr_head}));
