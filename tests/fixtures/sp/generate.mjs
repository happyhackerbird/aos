#!/usr/bin/env node
// Generates the Super Productivity sync-file fixtures under this directory with
// the fork's own format code (packages/sync-core: prefix helper, gzip, Argon2id
// + AES-GCM). Usage, from the agent-os checkout:
//
//     node tests/fixtures/sp/generate.mjs /path/to/super-productivity
//
// The fork must have `packages/sync-core/dist` built (`npm run sync-core:build`
// there writes gitignored output only). Every task, project and tag below is
// synthetic. The password of the encrypted fixtures is PASSWORD below.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execSync } from 'node:child_process';
import { webcrypto } from 'node:crypto';

const fork = process.argv[2];
if (!fork) {
  console.error('usage: node generate.mjs <path to the super-productivity fork>');
  process.exit(2);
}
const here = dirname(fileURLToPath(import.meta.url));
const core = await import(
  pathToFileURL(resolve(fork, 'packages/sync-core/dist/index.mjs')).href
);
const { createSyncFilePrefixHelpers, compressWithGzipToString, encrypt, decrypt } = core;

// provider.const.ts: REMOTE_FILE_CONTENT_PREFIX = 'pf_'; model-config.ts: CROSS_MODEL_VERSION = 4.5
const PREFIX = 'pf_';
const MODEL_VERSION = 4.5;
const PASSWORD = 'fixture-password';
const { getSyncFilePrefix } = createSyncFilePrefixHelpers({ prefix: PREFIX });

// ── synthetic data ─────────────────────────────────────────────────────────
const task = (id, title, extra = {}) => ({
  id,
  title,
  subTaskIds: [],
  timeSpentOnDay: {},
  timeSpent: 0,
  timeEstimate: 0,
  isDone: false,
  tagIds: [],
  created: 1760000000000,
  attachments: [],
  projectId: 'p-aos',
  ...extra,
});
const entityState = (items, extra = {}) => ({
  ids: items.map((item) => item.id),
  entities: Object.fromEntries(items.map((item) => [item.id, item])),
  ...extra,
});

const NOTES_1 = [
  'Make the poller retry once before it reports an error.',
  '',
  '## Acceptance',
  '- `uv run pytest -q tests/test_poller.py`',
  '* make lint',
  '',
  '## AOS',
  'harness: codex',
  'tier: 2',
  'host: server',
].join('\n');

const tasks = [
  task('task-1', 'Add retry to the poller', {
    notes: NOTES_1,
    tagIds: ['t-agent-os', 't-go'],
    subTaskIds: ['sub-1', 'sub-2'],
  }),
  task('sub-1', 'write the failing test', { parentId: 'task-1' }),
  task('sub-2', 'wire the retry', { parentId: 'task-1' }),
  task('task-2', 'Draft the README', { notes: 'Just the README, nothing else.', tagIds: ['t-agent-os'] }),
  task('task-3', 'Already done', { tagIds: ['t-agent-os'], isDone: true, doneOn: 1760000001000 }),
  task('task-4', 'Elsewhere', { projectId: 'p-other', tagIds: ['t-misc'] }),
  task('task-5', 'No repo tag', { tagIds: ['t-go'] }),
  task('task-6', 'Two repo tags', { tagIds: ['t-agent-os', 't-freelance'] }),
  task('task-7', 'Tier three', { notes: '## AOS\ntier: 3\n', tagIds: ['t-agent-os'] }),
];
const projects = [
  { id: 'p-aos', title: 'AOS', taskIds: ['task-1', 'task-2', 'task-3', 'task-5', 'task-6', 'task-7'], backlogTaskIds: [], noteIds: [], theme: {}, advancedCfg: {} },
  { id: 'p-other', title: 'Other', taskIds: ['task-4'], backlogTaskIds: [], noteIds: [], theme: {}, advancedCfg: {} },
];
const tags = [
  { id: 't-go', title: 'go', taskIds: ['task-1', 'task-5'], created: 1, theme: {}, advancedCfg: {} },
  { id: 't-agent-os', title: 'agent-os', taskIds: ['task-1', 'task-2', 'task-3', 'task-6', 'task-7'], created: 1, theme: {}, advancedCfg: {} },
  { id: 't-freelance', title: 'freelance_bot', taskIds: ['task-6'], created: 1, theme: {}, advancedCfg: {} },
  { id: 't-misc', title: 'misc', taskIds: ['task-4'], created: 1, theme: {}, advancedCfg: {} },
];
const state = {
  task: entityState(tasks, { currentTaskId: null, selectedTaskId: null, lastCurrentTaskId: null, isDataLoaded: true }),
  project: entityState(projects),
  tag: entityState(tags),
  globalConfig: { sync: { isEnabled: true } },
};
const clock = { 'client-a': 12 };

// v2: FileBasedSyncData (packages/sync-providers/src/file-based-sync-data.ts)
const v2 = {
  version: 2,
  syncVersion: 12,
  schemaVersion: 1,
  vectorClock: clock,
  lastModified: 1760000002000,
  clientId: 'client-a',
  state,
  recentOps: [],
};

// v3 (SPAP-11 split files, opt-in in the app): the reader refuses this layout
// (coordinator ruling 2026-10-14: no action-replay of recentOps). The fixture
// exists to prove the refusal is precise: the legacy file is the migration
// tombstone, the ops file points at its immutable snapshot through snapshotRef.
const snapshotClock = { 'client-a': 12 };
const v3State = {
  version: 3,
  syncVersion: 5,
  schemaVersion: 1,
  vectorClock: snapshotClock,
  lastModified: 1760000002000,
  clientId: 'client-a',
  state,
};
const v3Ops = {
  version: 3,
  syncVersion: 6,
  schemaVersion: 1,
  vectorClock: { 'client-a': 12, 'client-b': 6 },
  lastModified: 1760000004000,
  clientId: 'client-b',
  snapshotRef: { syncVersion: 5, vectorClock: snapshotClock, file: 'sync-state__5__0123456789abcdef.json' },
  recentOps: [
    {
      id: 'op-1', a: 'HU', o: 'UPD', e: 'TASK', d: 'task-2',
      p: { task: { id: 'task-2', changes: { title: 'Draft the README (revised)' } } },
      c: 'client-b', v: { 'client-a': 12, 'client-b': 6 }, t: 1760000003006, s: 1, sv: 6,
    },
  ],
};
const tombstone = { version: 3, format: 'split', migratedAt: 1760000005000, note: 'migrated to split sync files' };

// ── encoders, mirroring EncryptAndCompressHandlerService.compressAndEncrypt ──
const encode = async (data, { isCompress = false, isEncrypt = false } = {}) => {
  let body = JSON.stringify(data);
  if (isCompress) body = await compressWithGzipToString(body);
  if (isEncrypt) body = await encrypt(body, PASSWORD);
  return getSyncFilePrefix({ isCompress, isEncrypt, modelVersion: MODEL_VERSION }) + body;
};

// Legacy PBKDF2 ciphertext: the fork can only DECRYPT this form (encryption/legacy.ts),
// so it is produced here with WebCrypto at exactly legacy.ts's parameters and then
// verified below through the fork's own decrypt().
const legacyEncrypt = async (text, password) => {
  const subtle = webcrypto.subtle;
  const enc = new TextEncoder();
  const keyMaterial = await subtle.importKey('raw', enc.encode(password), { name: 'PBKDF2' }, false, ['deriveKey']);
  const key = await subtle.deriveKey(
    { name: 'PBKDF2', salt: enc.encode(password), iterations: 1000, hash: 'SHA-256' },
    keyMaterial, { name: 'AES-GCM', length: 256 }, true, ['encrypt', 'decrypt']);
  const iv = webcrypto.getRandomValues(new Uint8Array(12));
  const cipher = new Uint8Array(await subtle.encrypt({ name: 'AES-GCM', iv }, key, enc.encode(text)));
  const out = new Uint8Array(12 + cipher.length);
  out.set(iv, 0);
  out.set(cipher, 12);
  return Buffer.from(out).toString('base64');
};

const files = {
  'v2-plain.txt': await encode(v2),
  'v2-gzip.txt': await encode(v2, { isCompress: true }),
  'v2-encrypted.txt': await encode(v2, { isEncrypt: true }),
  'v2-gzip-encrypted.txt': await encode(v2, { isCompress: true, isEncrypt: true }),
  'v2-legacy.txt': getSyncFilePrefix({ isCompress: false, isEncrypt: true, modelVersion: MODEL_VERSION }) + (await legacyEncrypt(JSON.stringify(v2), PASSWORD)),
  'v3/sync-data.json': await encode(tombstone),
  'v3/sync-ops.json': await encode(v3Ops),
  'v3/sync-state__5__0123456789abcdef.json': await encode(v3State),
  'v3/sync-state.json': await encode(v3State),
};

// Round-trip check through the fork's own decoder before anything is written.
const { extractSyncFileStateFromPrefix } = createSyncFilePrefixHelpers({ prefix: PREFIX });
for (const [name, text] of Object.entries(files)) {
  const { isEncrypted, cleanDataStr } = extractSyncFileStateFromPrefix(text);
  if (isEncrypted) {
    const plain = await decrypt(cleanDataStr, PASSWORD);
    if (!plain.startsWith('{') && !plain.match(/^[A-Za-z0-9+/=]+$/)) throw new Error(`round trip failed for ${name}`);
  }
}

mkdirSync(join(here, 'v3'), { recursive: true });
for (const [name, text] of Object.entries(files)) {
  writeFileSync(join(here, name), text);
}
const expected = {
  password: PASSWORD,
  model_version: MODEL_VERSION,
  v2_task_ids: tasks.filter((t) => !t.parentId).map((t) => t.id),
  v2_aos_task_ids: tasks.filter((t) => !t.parentId && t.projectId === 'p-aos').map((t) => t.id),
};
writeFileSync(join(here, 'expected.json'), JSON.stringify(expected, null, 2) + '\n');

let commit = 'unknown';
try {
  commit = execSync('git rev-parse --short HEAD', { cwd: fork, encoding: 'utf8' }).trim();
} catch {
  // leave 'unknown'
}
console.log(`fixtures written (fork ${commit}): ${Object.keys(files).join(', ')}`);
