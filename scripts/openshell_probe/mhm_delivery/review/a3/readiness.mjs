// Execute exactly D44(iii)'s predicate, without substituting a future runner implementation.
import {writeFileSync, mkdirSync, mkdtempSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {pathToFileURL} from 'node:url';
const scratchParent = process.env.MHM_SCRATCH || tmpdir();
mkdirSync(scratchParent, {recursive:true});
const path = pathToFileURL(join(mkdtempSync(join(scratchParent, 'mhm-readiness-')), 'registered-noop.ts'));
writeFileSync(path,`export const protocol=2;
export default function adapter(pi:any){
 pi.on('session_start',()=>{}); pi.on('tool_result',()=>{}); pi.on('turn_end',()=>{});
}
`);
const m=await import(path.href);
const callbacks=new Map(),messages=[];
m.default({on:(e,f)=>callbacks.set(e,f),sendUserMessage:(...args)=>messages.push(args),sendMessage:()=>{}});
const admitted=m.protocol>=2 && ['session_start','tool_result','turn_end'].every(e=>callbacks.has(e));
await callbacks.get('session_start')({},{});
const result=await callbacks.get('tool_result')({content:[{type:'text',text:'ordinary'}]},{});
await callbacks.get('turn_end')({message:{role:'assistant',stopReason:'stop'},toolResults:[]},{});
console.log(JSON.stringify({probe:'D44 registered hollow extension',readinessExit:admitted?0:1,
 injected:result??null,continuations:messages.length}));
console.log(JSON.stringify({probe:'D44 version predicate',runnerProtocol:2,helperProtocol:3,
 admitted:3>=2,retainedOldSubcommandCompatibility:'no requirement in D44'}));
