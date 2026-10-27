# Super Productivity sync-file fixtures

Synthetic sync files for `tests/test_sp_sync.py`, generated with the fork's own
format code by `generate.mjs`:

    node tests/fixtures/sp/generate.mjs /path/to/super-productivity

Generated on 2026-10-14 from the fork at commit `896f7c52ed`
(`<fork checkout>`, `packages/sync-core/dist` built with
`npm run sync-core:build`). The fork is read-only for this script.

| File | Layout | Prefix |
|---|---|---|
| `v2-plain.txt` | legacy single file, version 2, JSON | `pf_4.5__` |
| `v2-gzip.txt` | gzip, base64 | `pf_C4.5__` |
| `v2-encrypted.txt` | Argon2id + AES-GCM (the fork's `encrypt()`) | `pf_E4.5__` |
| `v2-gzip-encrypted.txt` | gzip, then encrypted | `pf_CE4.5__` |
| `v2-legacy.txt` | PBKDF2 (password as salt, 1000 iterations, SHA-256) + AES-GCM | `pf_E4.5__` |
| `v3/sync-data.json` | the migration tombstone (`version: 3, format: split`) | `pf_4.5__` |
| `v3/sync-ops.json`, `v3/sync-state*.json` | the split layout the reader refuses | `pf_4.5__` |

The password of every encrypted file is in `expected.json` (`fixture-password`).
The fork has no legacy *encrypt* path (`encryption/legacy.ts` decrypts only), so
`v2-legacy.txt` is produced with WebCrypto at exactly those parameters and verified
through the fork's own `decrypt()` before it is written. Every task, project and tag
is invented; none is the operator's data.
