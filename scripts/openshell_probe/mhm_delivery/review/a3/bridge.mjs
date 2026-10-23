import {copyFileSync, readFileSync, writeFileSync, chmodSync, mkdirSync, mkdtempSync} from 'node:fs';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {dirname, join} from 'node:path';
import {tmpdir} from 'node:os';
const here = dirname(fileURLToPath(import.meta.url));
const scratchParent = process.env.MHM_SCRATCH || tmpdir();
mkdirSync(scratchParent, {recursive:true});
const base = mkdtempSync(join(scratchParent, 'mhm-a3-bridge-'));
const scratchBase = base + '/';
copyFileSync(fileURLToPath(new URL('../../aos_pi.ts', import.meta.url)), scratchBase+'aos_pi.ts');
// Relocate only the fixture's probe log; bridge operations are unchanged.
let source = readFileSync(scratchBase+'aos_pi.ts', 'utf8');
source = source.replace('const LOG = join(homedir(), ".local", "share", "aos-pi-probe", "fires.jsonl");',
                        `const LOG = ${JSON.stringify(scratchBase+'fires.jsonl')};`);
writeFileSync(scratchBase+'aos_pi.ts',source);
const stub = scratchBase+'hook-stub.py';
writeFileSync(stub,`#!/usr/bin/env python3
import json,os,sys
if sys.argv[1]=='inbox':
 print(json.dumps({'hookSpecificOutput':{'additionalContext':'<operator-message>question</operator-message>'}}))
else:
 mode=os.environ.get('A3_MODE')
 if mode=='bad-shape': print(json.dumps({'decision':'block','reason':None}))
 elif mode=='failed-stop':
  print('aos-agent: stop hook: OSError: output write failed',file=sys.stderr)
 elif mode=='nonzero-stop': sys.exit(7)
 else: print(json.dumps({'decision':'block','reason':'answer the acknowledged question'}))
`);
chmodSync(stub,0o755);
process.env.AOS_AGENT_BIN=stub;
const {default:extension}=await import(pathToFileURL(scratchBase+'aos_pi.ts'));
for(const mode of ['normal','failed-stop','nonzero-stop','bad-shape']){
 process.env.A3_MODE=mode;
 const callbacks=new Map(), queued=[];
 extension({on:(e,f)=>callbacks.set(e,f),sendUserMessage:(text,opts)=>queued.push({text,opts}),sendMessage:()=>{}});
 const ctx={sessionManager:{getSessionId:()=> 'parent',getSessionFile:()=> '/parent.jsonl'}};
 await callbacks.get('session_start')({},ctx);
 const result=await callbacks.get('tool_result')({toolName:'bash',content:[{type:'text',text:'ordinary'}]},ctx);
 const returned=await callbacks.get('turn_end')({message:{role:'assistant',stopReason:'stop'},toolResults:[]},ctx);
 console.log(JSON.stringify({mode,inboundDelivered:result?.content?.length===2,finalTurnReturn:returned??null,
   continuationCount:queued.length,stopFailureVisibleToModel:JSON.stringify([returned,queued]).includes('failed')}));
}
