"""Super Productivity sync files: the Dropbox download and the file format.

No bus knowledge lives here. The format follows the fork's own source
(packages/sync-core: sync-file-prefix.ts, compression.ts, encryption.ts with
encryption/argon2.ts, encryption/legacy.ts, encryption/web-crypto.ts;
packages/sync-providers/src/file-based-sync-data.ts; the app's
encrypt-and-compress-handler.service.ts for the decode order):

    <prefix><body>      prefix = "pf_" + ("C" if gzip) + ("E" if encrypted) + <model version> + "__"
    body, encrypted     base64([salt 16][iv 12][AES-GCM ciphertext + tag])   Argon2id key
                        base64([iv 12][AES-GCM ciphertext + tag])            legacy PBKDF2 key
    body, compressed    base64(gzip(json))
    body, plain         json

Decode order: prefix, decrypt, gunzip, JSON. The legacy single file
(``sync-data.json``, ``version: 2``) carries the complete state snapshot and is
authoritative as read. The split layout (``version: 3``: ``sync-ops.json`` plus a
``sync-state`` snapshot, an opt-in in the app) is detected precisely and refused:
replaying its ``recentOps`` needs the app's reducer semantics, not entity records
(coordinator ruling, 2026-10-14).

Dropbox: ``files/download`` on the content host with the path in the
``Dropbox-API-Arg`` header and the metadata in the ``dropbox-api-result`` response
header; a short-lived access token minted from the refresh token at
``/oauth2/token`` (``grant_type=refresh_token`` with ``client_id`` and
``client_secret`` as form fields). ``python -m agent_os.sp_sync auth`` mints the
refresh token once (``token_access_type=offline``, no redirect: Dropbox shows the
code to the user).
"""
from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import gzip
import json
import os
import re
import sys
import time
import zlib
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import httpx
from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# file-based-sync-data.ts FILE_BASED_SYNC_CONSTANTS
SYNC_FILE = "sync-data.json"
OPS_FILE = "sync-ops.json"
STATE_FILE = "sync-state.json"

# provider.const.ts REMOTE_FILE_CONTENT_PREFIX, sync-file-prefix.ts prefixRegex
PREFIX = "pf_"
_PREFIX_RE = re.compile(r"^pf_(C)?(E)?(\d+(?:\.\d+)?)__")
_BASE64_ONLY = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")

# encryption/web-crypto.ts and encryption/argon2.ts
SALT_LENGTH = 16
IV_LENGTH = 12
KEY_LENGTH = 32
_MIN_ARGON2_SIZE = SALT_LENGTH + IV_LENGTH + 16
_MIN_LEGACY_SIZE = IV_LENGTH + 16
ARGON2_PARAMS = {"time_cost": 3, "memory_cost": 65536, "parallelism": 1}
# encryption/legacy.ts: PBKDF2-SHA256, 1000 iterations, the password as its own salt
_LEGACY_ITERATIONS = 1000

DROPBOX_API_BASE = "https://api.dropboxapi.com"
DROPBOX_CONTENT_BASE = "https://content.dropboxapi.com"
DROPBOX_AUTHORIZE_URL = "https://www.dropbox.com/oauth2/authorize"
_TOKEN_TIMEOUT = 30.0
_DOWNLOAD_TIMEOUT = 120.0

SPLIT_SETTING_HINT = ("Turn off the split-sync-files setting in Super Productivity's sync settings and "
                      "let the app sync once; nothing changed on the bus.")


class SyncFileError(ValueError):
    """The sync file cannot be read; the message says why."""


class PasswordRequired(SyncFileError):
    pass


class WrongPassword(SyncFileError):
    pass


class SplitFormatError(SyncFileError):
    """The folder holds the split layout (version 3), which this reader refuses."""


class DropboxError(RuntimeError):
    """A Dropbox call failed; the message never carries a token or secret."""


# -- format ------------------------------------------------------------------

def _head_shape(text: str) -> str:
    """sync-file-prefix.ts classifyHead: a coarse shape of the body's first bytes, never the bytes."""
    head = text[:64].lstrip()
    if head.startswith("<"):
        return "markup"
    if head.startswith("{") or head.startswith("["):
        return "json"
    return "base64" if len(head) >= 16 and _BASE64_ONLY.match(head) else "other"


def split_prefix(text: str) -> tuple[bool, bool, float, str]:
    """(compressed, encrypted, model version, body) from the prefixed text."""
    match = _PREFIX_RE.match(text)
    if match is None:
        raise SyncFileError(
            f"not a Super Productivity sync file: expected the {PREFIX} prefix "
            f"(head shape {_head_shape(text)}, length {len(text)})")
    return bool(match.group(1)), bool(match.group(2)), float(match.group(3)), text[match.end():]


def _sanitize_base64(text: str) -> str:
    """compression.ts sanitizeBase64: drop foreign characters, repair the padding."""
    cleaned = re.sub(r"[^A-Za-z0-9+/=]", "", text)
    remainder = len(cleaned) % 4
    if remainder == 0:
        return cleaned
    if remainder == 1:
        return cleaned[:-1]
    return cleaned + "=" * (4 - remainder)


def _argon2_key(password: str, salt: bytes) -> bytes:
    return hash_secret_raw(password.encode("utf-8"), salt, hash_len=KEY_LENGTH, type=Type.ID, **ARGON2_PARAMS)


def _legacy_key(password: str) -> bytes:
    secret = password.encode("utf-8")
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=KEY_LENGTH, salt=secret,
                      iterations=_LEGACY_ITERATIONS).derive(secret)


def _decrypt_legacy(buffer: bytes, password: str) -> bytes:
    return AESGCM(_legacy_key(password)).decrypt(buffer[:IV_LENGTH], buffer[IV_LENGTH:], None)


def _decrypt_argon2(buffer: bytes, password: str) -> bytes:
    key = _argon2_key(password, buffer[:SALT_LENGTH])
    return AESGCM(key).decrypt(buffer[SALT_LENGTH:SALT_LENGTH + IV_LENGTH], buffer[SALT_LENGTH + IV_LENGTH:], None)


def decrypt(data: str, password: str) -> str:
    """encryption.ts decrypt: Argon2id for 44+ bytes with the legacy fallback, legacy below 44."""
    try:
        buffer = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SyncFileError(f"the encrypted body is not valid base64 (truncated or damaged): {exc}") from exc
    if len(buffer) < _MIN_LEGACY_SIZE:
        raise SyncFileError("the encrypted body is too short to be valid")
    attempts = [_decrypt_legacy] if len(buffer) < _MIN_ARGON2_SIZE else [_decrypt_argon2, _decrypt_legacy]
    for attempt in attempts:
        try:
            return attempt(buffer, password).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError):
            continue
    raise WrongPassword("cannot decrypt the sync file with AOS_SP_SYNC_PASSWORD: wrong password or corrupt data")


def decompress(data: str) -> str:
    try:
        return gzip.decompress(base64.b64decode(_sanitize_base64(data))).decode("utf-8")
    except (OSError, EOFError, zlib.error, binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise SyncFileError(f"the gzip body is corrupt or truncated: {exc}") from exc


def decode(raw: bytes | str, password: str | None) -> dict[str, Any]:
    """The JSON object inside a sync file: prefix, decrypt, gunzip, JSON (the fork's order)."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SyncFileError(f"the sync file is not UTF-8 text: {exc}") from exc
    else:
        text = raw
    compressed, encrypted, _model_version, body = split_prefix(text)
    if encrypted:
        if not password:
            raise PasswordRequired(
                "the sync file is encrypted (pf_E prefix) and AOS_SP_SYNC_PASSWORD is not set")
        body = decrypt(body, password)
    if compressed:
        body = decompress(body)
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise SyncFileError(f"the decoded body is not JSON (truncated or damaged): {exc}") from exc
    if not isinstance(data, dict):
        raise SyncFileError("the decoded body is not a JSON object")
    return data


def is_split_tombstone(data: Mapping[str, Any]) -> bool:
    """file-based-sync-data.ts FileBasedSplitTombstone: written over sync-data.json by the migration."""
    return data.get("version") == 3 and data.get("format") == "split"


def _split_error(cause: str) -> SplitFormatError:
    return SplitFormatError(
        "Super Productivity uses the split sync files (version 3: sync-ops.json plus a sync-state "
        f"snapshot), which this reader does not support: {cause}. {SPLIT_SETTING_HINT}")


def _ops_cause(ops: Mapping[str, Any]) -> str:
    ref = ops.get("snapshotRef") if isinstance(ops.get("snapshotRef"), dict) else {}
    snapshot = ref.get("file") or STATE_FILE
    return f"{OPS_FILE} (syncVersion {ops.get('syncVersion')}, snapshot {snapshot}) is present"


def load_state(files: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The state snapshot from decoded files keyed by name; version 2 only."""
    data = files.get(SYNC_FILE)
    if data is not None:
        if data.get("version") == 2:
            state = data.get("state")
            if not isinstance(state, dict):
                raise SyncFileError(f"{SYNC_FILE} version 2 carries no state snapshot")
            _entities(state, "task")
            return state
        if is_split_tombstone(data):
            raise _split_error(f"{SYNC_FILE} is the migration tombstone of the split format")
        raise SyncFileError(f"{SYNC_FILE} has unsupported version {data.get('version')!r}")
    ops = files.get(OPS_FILE)
    if ops is not None:
        raise _split_error(_ops_cause(ops))
    if STATE_FILE in files:
        raise _split_error(f"{STATE_FILE} is present")
    raise SyncFileError(f"no {SYNC_FILE} among the downloaded files")


def _entities(state: Mapping[str, Any], key: str) -> tuple[list[str], dict[str, dict[str, Any]]]:
    if not isinstance(state, Mapping):
        raise SyncFileError("the state snapshot is not an object")
    model = state.get(key)
    if key == "task":
        if not isinstance(model, dict):
            raise SyncFileError("the state snapshot has no task object")
        entities = model.get("entities")
        if not isinstance(entities, dict):
            raise SyncFileError("the task collection has no entities object")
        if any(not isinstance(value, dict) for value in entities.values()):
            raise SyncFileError("the task collection contains a non-object entity")
        if "ids" in model and (not isinstance(model["ids"], list)
                               or any(not isinstance(task_id, str) for task_id in model["ids"])):
            raise SyncFileError("the task collection ids must be a list of strings")
        if any(task_id not in entities for task_id in model.get("ids", [])):
            raise SyncFileError("the task collection ids reference a missing entity")
    else:
        model = model if isinstance(model, dict) else {}
        raw_entities = model.get("entities")
        entities = {k: v for k, v in raw_entities.items() if isinstance(v, dict)} if isinstance(raw_entities, dict) else {}
    ordered = model.get("ids")
    ids = [i for i in ordered if isinstance(i, str) and i in entities] if isinstance(ordered, list) else []
    ordered_ids = set(ids)
    ids += [i for i in entities if i not in ordered_ids]
    return ids, entities


def task_ids(state: Mapping[str, Any]) -> list[str]:
    """Every task id the app still holds, top-level and nested, in the app's order."""
    return _entities(state, "task")[0]


def extract_tasks(state: Mapping[str, Any], *, project: str | None = None,
                  tag: str | None = None) -> list[dict[str, Any]]:
    """Top-level tasks (no parent) as plain records, in the app's order.

    With ``project`` or ``tag`` (titles), only tasks in that project or carrying that
    tag; both given means either. Without either, every top-level task."""
    task_ids, tasks = _entities(state, "task")
    _, projects = _entities(state, "project")
    _, tags = _entities(state, "tag")
    project_title = {pid: str(p.get("title") or "") for pid, p in projects.items()}
    tag_title = {tid: str(t.get("title") or "") for tid, t in tags.items()}
    records = []
    for task_id in task_ids:
        task = tasks[task_id]
        if task.get("parentId"):
            continue
        tag_ids = [t for t in (task.get("tagIds") or []) if isinstance(t, str)]
        titles = [tag_title.get(t, t) for t in tag_ids]
        project_name = project_title.get(task.get("projectId") or "", "")
        if project is not None or tag is not None:
            if not ((project is not None and project_name == project) or (tag is not None and tag in titles)):
                continue
        subtasks = [str(tasks[s].get("title") or "") for s in (task.get("subTaskIds") or []) if s in tasks]
        records.append({
            "id": task_id, "title": str(task.get("title") or ""), "notes": str(task.get("notes") or ""),
            "tags": titles, "project": project_name, "is_done": bool(task.get("isDone")), "subtasks": subtasks,
        })
    return records


# -- Dropbox ------------------------------------------------------------------

def _error_summary(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict):
        return str(payload.get("error_summary") or payload.get("error_description") or payload.get("error")
                   or response.text[:200])
    return response.text[:200]


class Dropbox:
    """files/download with a refresh-token client. One access token per lifetime,
    minted lazily, refreshed on expiry and once on a 401."""

    def __init__(self, app_key: str, app_secret: str, refresh_token: str, *,
                 client: httpx.Client | None = None, api_base: str = DROPBOX_API_BASE,
                 content_base: str = DROPBOX_CONTENT_BASE) -> None:
        self.app_key, self.app_secret, self.refresh_token = app_key, app_secret, refresh_token
        self.api_base, self.content_base = api_base.rstrip("/"), content_base.rstrip("/")
        self.client = client or httpx.Client(timeout=_DOWNLOAD_TIMEOUT)
        self._access_token: str | None = None
        self._expires_at = 0.0

    def _refresh(self) -> str:
        response = self.client.post(
            f"{self.api_base}/oauth2/token",
            data={"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                  "client_id": self.app_key, "client_secret": self.app_secret},
            timeout=_TOKEN_TIMEOUT)
        if response.status_code != 200:
            raise DropboxError(f"Dropbox token refresh returned {response.status_code}: {_error_summary(response)}")
        payload = response.json()
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise DropboxError("Dropbox token refresh returned no access_token")
        self._access_token = token
        try:
            self._expires_at = time.monotonic() + max(float(payload.get("expires_in", 0)) - 60, 0)
        except (TypeError, ValueError):
            self._expires_at = 0.0
        return token

    def _token(self) -> str:
        if self._access_token is None or time.monotonic() >= self._expires_at:
            return self._refresh()
        return self._access_token

    def _download(self, path: str, token: str) -> httpx.Response:
        return self.client.post(
            f"{self.content_base}/2/files/download",
            headers={"Authorization": f"Bearer {token}", "Dropbox-API-Arg": json.dumps({"path": path}),
                     "Content-Type": "application/octet-stream"},
            timeout=_DOWNLOAD_TIMEOUT)

    def download_file(self, path: str) -> str | None:
        """The file's text, or None when Dropbox reports path/not_found."""
        response = self._download(path, self._token())
        if response.status_code == 401:
            response = self._download(path, self._refresh())
        if response.status_code == 200:
            return response.text
        summary = _error_summary(response)
        if response.status_code == 409 and "path/not_found" in summary:
            return None
        raise DropboxError(f"Dropbox files/download returned {response.status_code} for {path}: {summary}")


def folder_path(folder: str) -> str:
    """Dropbox folder as a path prefix: '' for the root (or app folder), '/a/b' otherwise."""
    stripped = folder.strip().strip("/")
    return f"/{stripped}" if stripped else ""


def fetch_state(dropbox: Dropbox, folder: str, password: str | None) -> dict[str, Any]:
    """Download and decode the sync file set in ``folder``; the state snapshot."""
    base = folder_path(folder)
    files: dict[str, dict[str, Any]] = {}
    raw = dropbox.download_file(f"{base}/{SYNC_FILE}")
    if raw is not None:
        files[SYNC_FILE] = decode(raw, password)
    else:
        raw_ops = dropbox.download_file(f"{base}/{OPS_FILE}")
        if raw_ops is None:
            raise SyncFileError(f"no sync files in Dropbox folder {base or '/'}: neither {SYNC_FILE} nor {OPS_FILE} exists")
        files[OPS_FILE] = decode(raw_ops, password)
    return load_state(files)


# -- one-time authorization ----------------------------------------------------

def authorize_url(app_key: str) -> str:
    return DROPBOX_AUTHORIZE_URL + "?" + urlencode(
        {"client_id": app_key, "response_type": "code", "token_access_type": "offline"})


def exchange_code(app_key: str, app_secret: str, code: str, *, client: httpx.Client | None = None,
                  api_base: str = DROPBOX_API_BASE) -> dict[str, Any]:
    client = client or httpx.Client(timeout=_TOKEN_TIMEOUT)
    response = client.post(f"{api_base.rstrip('/')}/oauth2/token",
                           data={"grant_type": "authorization_code", "code": code,
                                 "client_id": app_key, "client_secret": app_secret})
    if response.status_code != 200:
        raise DropboxError(f"Dropbox code exchange returned {response.status_code}: {_error_summary(response)}")
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("refresh_token"), str):
        raise DropboxError("Dropbox code exchange returned no refresh_token (was token_access_type=offline sent?)")
    return payload


def main(argv: list[str] | None = None, *, client: httpx.Client | None = None,
         read: Callable[[str], str] = input, secret: Callable[[str], str] = getpass.getpass) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_os.sp_sync")
    sub = parser.add_subparsers(dest="command", required=True)
    auth_p = sub.add_parser("auth", help="print the Dropbox authorize URL, take the pasted code, print the refresh token once")
    auth_p.add_argument("--app-key", help="Dropbox app key (else AOS_SP_DROPBOX_APP_KEY, else prompted)")
    auth_p.add_argument("--app-secret", help="Dropbox app secret (else AOS_SP_DROPBOX_APP_SECRET, else prompted)")
    args = parser.parse_args(argv)
    app_key = (args.app_key or os.environ.get("AOS_SP_DROPBOX_APP_KEY") or read("Dropbox app key: ")).strip()
    app_secret = (args.app_secret or os.environ.get("AOS_SP_DROPBOX_APP_SECRET") or secret("Dropbox app secret: ")).strip()
    if not app_key or not app_secret:
        print("an app key and an app secret are required", file=sys.stderr)
        return 2
    print("1. Open this URL, allow the app, and copy the code Dropbox shows (no redirect is used):")
    print(authorize_url(app_key))
    code = read("2. Paste the code here: ").strip()
    if not code:
        print("no code given", file=sys.stderr)
        return 2
    try:
        tokens = exchange_code(app_key, app_secret, code, client=client)
    except (DropboxError, httpx.HTTPError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("3. The refresh token, shown once; put these in the bus environment:")
    print(f"AOS_SP_DROPBOX_APP_KEY={app_key}")
    print("AOS_SP_DROPBOX_APP_SECRET=<the app secret>")
    print(f"AOS_SP_DROPBOX_REFRESH_TOKEN={tokens['refresh_token']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
