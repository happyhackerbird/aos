import { copyFileSync, mkdtempSync, readFileSync, writeFileSync, chmodSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = fileURLToPath(new URL('.', import.meta.url));
const scratchParent = process.env.MHM_SCRATCH || tmpdir();
mkdirSync(scratchParent, {recursive:true});
const scratch = mkdtempSync(join(scratchParent, 'mhm-a4-bridge-'));
const fixture = fileURLToPath(new URL('../../aos_pi.ts', import.meta.url));
const copied = join(scratch, 'aos_pi.ts');
copyFileSync(fixture, copied);
writeFileSync(copied, readFileSync(copied, 'utf8').replace(
  'const LOG = join(homedir(), ".local", "share", "aos-pi-probe", "fires.jsonl");',
  `const LOG = ${JSON.stringify(join(scratch, 'fires.jsonl'))};`));
const helper = join(scratch, 'helper.py');
writeFileSync(helper, `#!/usr/bin/env python3
import json,sys,os
if sys.argv[1]=='inbox':
 print(json.dumps({'hookSpecificOutput':{'hookEventName':'PostToolUse','additionalContext':'<operator-message id="operator-i1">new word</operator-message>'}}))
elif os.environ.get('A4_STOP')=='1':
 print(json.dumps({'decision':'block','reason':'answer then restate the report'}))
`);
chmodSync(helper, 0o755);
process.env.AOS_AGENT_BIN = helper;
const { default: extension } = await import(pathToFileURL(copied));
const handlers = new Map(), queued = [];
extension({ on: (name, handler) => handlers.set(name, handler),
  sendUserMessage: (text, opts) => queued.push({ text, opts }), sendMessage: () => {} });
const ctx = { sessionManager: { getSessionId: () => 'parent', getSessionFile: () => '/parent.jsonl' } };
await handlers.get('session_start')({}, ctx);
const prior = [{type:'text', text:'operator plugin replacement content'}];
const result = await handlers.get('tool_result')({ toolName:'bash', content:prior }, ctx);
console.log(JSON.stringify({ probe:'D49 append after prior replacement',
  priorContentPreserved:result.content[0].text===prior[0].text,
  operatorFrameLast:result.content.at(-1).text.includes('<operator-message') }));
process.env.A4_STOP = '1';
for (const stopReason of ['error','aborted','length','stop']) {
  const before=queued.length;
  await handlers.get('turn_end')({message:{role:'assistant',stopReason},toolResults:[]},ctx);
  console.log(JSON.stringify({probe:'D49 spike final predicate',stopReason,continuations:queued.length-before}));
}
