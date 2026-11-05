"""Pi-family harness adapter: invocation flavours, the settled event contract, worker auth, redaction, bundle checks.

The OpenShell runner (``agent_os.openshell_runner``) runs a Pi-family coding agent inside the sandbox in
print/JSON mode and hands stdout lines to :func:`parse_output`. Success needs the whole settled
event contract (measured on Pi 1.1.0): one session header with a canonical UUID, the latest ``agent_end``
whose final assistant message stopped with ``stop`` and carries nonblank text, then a final
``agent_settled`` that was not aborted, and a zero process exit. Anything else fails with a fixed,
bounded diagnostic; an observed session id is preserved and never manufactured.

Flavours (``pi``, ``omp``) map the options AOS needs onto each binary's real flags. Every flag the
runner passes is checked against the binary's ``--help`` inside the sandbox before the job is
released; the version is recorded, and a version outside the tested list is a warning, not a refusal.

Worker auth is the ``openai-codex`` OAuth entry of a Pi-schema ``auth.json`` that the operator exports
for the worker, or of a persistent omp worker login on the host that :func:`login_auth` renews through omp's
own refresh before admission. Only that entry is selected, its refresh token is replaced by a public placeholder so
no renewable credential enters a job, and the effective expiry is the earlier of the declared expiry
and the access token's JWT ``exp``.
"""
from __future__ import annotations

import base64
import dataclasses
import fcntl
import io
import json
import math
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
from typing import Any, Callable, Iterable

from agent_os import policy

RAW_CAP = 8 * 1024 * 1024          # legacy bytes-input parser cap; streaming callers impose their disk cap
TEXT_TAIL = 20000                  # characters of final assistant text kept for the report
AUTH_FILE_CAP = 1024 * 1024
OBJECT_CAP = 64 * 1024 * 1024      # largest git object a Pi result bundle may carry
PLACEHOLDER_REFRESH = "aos-nonrenewable-worker-snapshot"  # public value; never treated as a secret
AUTH_PROVIDER = "openai-codex"     # the only provider with a supported worker-auth path
# Harness grants each provider needs in the sandbox policy (operator AOS_OPENSHELL_HARNESS_HOSTS_PI).
# Measured 2026-10-12 (F52): with the nonrenewable snapshot Pi only talks to chatgpt.com; a run granted
# chatgpt.com:443 alone completed and auth.openai.com was never contacted (it serves refresh, which the
# placeholder refresh value cannot do anyway).
REQUIRED_GRANTS: dict[str, tuple[str, ...]] = {"openai-codex": ("chatgpt.com:443",)}
SESSION_DIR = "/sandbox/pi-sessions"
AGENT_DIR = "/tmp/pi-agent"
CONTRACT = "output did not match the Pi event contract"

# Events that mean the agent started working again after an agent_end: that end is no longer final.
RENEWED = frozenset({"agent_start", "turn_start", "message_start", "message_update", "message_end",
                     "tool_execution_start", "auto_retry_start", "auto_compaction_start"})
KNOWN_STOPS = frozenset({"stop", "length", "toolUse", "error", "aborted"})


class AuthError(ValueError):
    """Selected worker auth is missing, malformed, of the wrong kind or too close to expiry."""


class BundleError(ValueError):
    """A result bundle carries a selected secret or an oversized object."""


# -- result parsing ---------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class PiResult:
    text: str | None
    session_id: str | None
    error: str | None
    stop_reason: str | None
    settled: bool


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _assistant_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(block["text"] for block in content
                   if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))


def parse_omp_output(raw: bytes | Iterable[bytes], returncode: int | None,
                     expected_session_id: str | None = None) -> PiResult:
    """omp print-mode JSONL. Measured on omp 18.8.3 (2026-10-12): the same session header and agent events
    as Pi but no ``agent_settled``; print mode returns only after the prompt, its retries and the advisor
    drain finished, and exits 1 when the last assistant message stopped with error or aborted. So the
    settlement signal is the zero exit, and the latest ``agent_end`` must not be followed by renewed
    activity. Every other rule of :func:`parse_output` applies unchanged."""
    return parse_output(raw, returncode, expected_session_id, settle_event=False)


def parse_output(raw: bytes | Iterable[bytes], returncode: int | None, expected_session_id: str | None = None, *,
                 settle_event: bool = True) -> PiResult:
    """Interpret Pi print-mode JSONL with fixed bounded failure diagnostics.

    Iterables yield byte lines (with or without their newline); memory depends on the largest line and
    final text, not stream length. Bytes retain the legacy RAW_CAP failure contract. Iterator I/O errors
    propagate to the collecting caller. Full final text is returned for redaction before report clipping.
    ``settle_event=False`` is the omp contract (no ``agent_settled``; see :func:`parse_omp_output`)."""
    session: str | None = None
    problem: str | None = None
    ended = False
    final_assistant = False
    reason: object = None
    text = ""
    settled = False
    aborted: object = None
    last_type: str | None = None
    if isinstance(raw, bytes):
        if len(raw) > RAW_CAP:
            problem = f"output exceeds the {RAW_CAP // (1024 * 1024)} MiB cap"
            # Preserve the legacy oversized outcome: inspect only an intact first line inside the cap.
            newline = raw.find(b"\n", 0, RAW_CAP)
            raw = raw[:newline + 1] if newline >= 0 else b""
        raw = io.BytesIO(raw)
    for line in raw:
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except UnicodeDecodeError:
            problem = problem or f"{CONTRACT} (invalid UTF-8)"
            break
        except ValueError:
            problem = problem or f"{CONTRACT} (malformed or truncated JSON line)"
            break
        kind = record.get("type") if isinstance(record, dict) else None
        if not isinstance(kind, str) or not kind:
            problem = problem or f"{CONTRACT} (record without a type)"
            break
        if session is None:
            if kind != "session":
                problem = problem or f"{CONTRACT} (no session header)"
                break
            if not _canonical_uuid(record.get("id")):
                problem = problem or f"{CONTRACT} (session id is not a canonical UUID)"
                break
            session = record["id"]
            if expected_session_id is not None and session != expected_session_id:
                problem = problem or "session id differs from the requested one"
                break
        elif kind == "session":
            problem = problem or f"{CONTRACT} (repeated session header)"
            break
        elif kind == "agent_end":
            ended = True
            final_assistant = False
            reason, text = None, ""
            messages = record.get("messages")
            if isinstance(messages, list) and messages:
                final = messages[-1]
                if isinstance(final, dict) and final.get("role") == "assistant":
                    final_assistant = True
                    reason = final.get("stopReason")
                    text = _assistant_text(final)
                del final
            del messages
            settled = False
        elif kind == "agent_settled":
            settled = ended
            aborted = record.get("aborted")
        elif kind in RENEWED:
            ended, settled, final_assistant = False, False, False
            reason, text = None, ""
        last_type = kind
        del record
    stop_reason = reason if reason in KNOWN_STOPS else None
    if not settle_event:
        settled = ended and returncode == 0
    if problem is None:
        if session is None:
            problem = f"{CONTRACT} (no session header)"
        elif not settle_event and not ended:
            problem = f"{CONTRACT} (not ended by a final agent_end)"
        elif settle_event and (last_type != "agent_settled" or not settled):
            problem = f"{CONTRACT} (not settled after a final agent_end)"
        elif settle_event and aborted is not False:
            problem = "agent run was aborted"
        elif not final_assistant:
            problem = f"{CONTRACT} (no final assistant message)"
        elif stop_reason is None:
            problem = "assistant stop reason is missing or unknown"
        elif stop_reason != "stop":
            problem = f"assistant stopped with {stop_reason}"
        elif not text.strip():
            problem = "assistant final message has no text"
    if returncode is None:
        problem = "harness did not exit (timeout, cancellation or collection failure)"
    elif returncode != 0:
        problem = f"harness exited {int(returncode)}"
    if problem is not None:
        return PiResult(None, session, "pi: " + problem, stop_reason, settled and (aborted is False or not settle_event))
    # Callers use safe_tail(text, TEXT_TAIL, auth): redact the full final text before cutting it.
    return PiResult(text, session, None, stop_reason, True)


# -- flavours ---------------------------------------------------------------------------

BIN_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
BIN_PATH = re.compile(r"(/[A-Za-z0-9._+-]+)+")
MODEL_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*")


@dataclasses.dataclass(frozen=True)
class Invocation:
    command: str                 # shell text run with stdin from the prompt file
    required_flags: tuple[str, ...]
    expected_session_id: str | None


@dataclasses.dataclass(frozen=True)
class Flavour:
    name: str
    tested_versions: tuple[str, ...]
    session_id_flag: bool
    env: tuple[tuple[str, str], ...]
    build: Callable[..., list[str]]
    parser: Callable[[bytes | Iterable[bytes], int | None, str | None], PiResult]
    auth_path: str                     # where the nonrenewable snapshot goes in the sandbox
    auth_payload: Callable[[Any], bytes]  # PiAuth -> the file written there
    unmeasured: tuple[str, ...] = ()   # live-probe items reported on every run


def _pi_flags(*, provider: str | None, model: str, session_id: str | None, harness_timeout: int,
              extensions: tuple[str, ...] = (), skills: tuple[str, ...] = (), comms: bool = False,
              automatic: bool = True, interactive: bool = False) -> list[str]:
    """Explicit -e paths and --skill directories load under --no-extensions --no-skills (Pi mechanisms probe,
    findings 1, 2). Pi has no subagent mechanism on 1.1.0 (finding 5). With comms, --no-mcp is dropped and `-e builtin:mcp`
    loads Pi's built-in MCP support under --no-extensions; the image-owned aos extension is
    the final -e, after the operator's, in both automatic and pull modes."""
    if interactive:
        raise ValueError("interactive execution needs the omp flavour")
    return ["--print", "--mode", "json", "--provider", provider or AUTH_PROVIDER, "--model", model,
            "--session-id", session_id or "", "--session-dir", SESSION_DIR, "--no-extensions", "--no-skills",
            "--no-prompt-templates", *([] if comms else ["--no-mcp"]),
            "--no-approve", "--no-context-files", "--no-themes", "--offline",
            *(["-e", "builtin:mcp"] if comms else []),
            *(flag for path in extensions for flag in ("-e", path)),
            *(flag for path in skills for flag in ("--skill", path)),
            *(["-e", "/usr/local/lib/aos-agent/aos_pi.ts"] if comms else [])]


# omp's default tool set under the runner's flags, its native `task` tool (subagents) included, as on Claude
# (docs/probes/2026-10-16-omp-mcp.md, finding 3). An allowlist without `task` closes it
# (docs/probes/2026-10-14-pi-mechanisms.md, finding 4, run 9), so a list the runner passes always names it.
OMP_TOOLS = ("read", "bash", "edit", "eval", "glob", "grep", "wait", "todo", "write", "web_search", "task")
# The aos server's tools as omp 18.8.3 registers them: mcp__<server>_<tool>, the tool's repeated server prefix dropped
# (agents that talk U6, docs/probes/2026-10-16-omp-mcp.md, finding 2). A registered MCP tool reaches the model only
# when --tools names it (finding 3), so a comms run always passes the allowlist.
OMP_AOS_TOOLS = tuple(f"mcp__aos_{verb}" for verb in ("ask", "reply", "inbox", "wait", "declare", "propose"))

OMP_AOS_TOOLS_INTERACTIVE = OMP_AOS_TOOLS + (
    "mcp__aos_submit", "mcp__aos_inbox_wait", "mcp__aos_family", "mcp__aos_import", "mcp__aos_export_candidate")
OMP_INTERACTIVE_CONFIG = "/tmp/pi-agent/interactive.yml"


def _omp_flags(*, provider: str | None, model: str, session_id: str | None, harness_timeout: int,
               extensions: tuple[str, ...] = (), skills: tuple[str, ...] = (), comms: bool = False,
               automatic: bool = True, interactive: bool = False) -> list[str]:
    """omp has no --skill: skills (names here) are read from $PI_CODING_AGENT_DIR/skills with discovery on and
    --skills= as the filter; agents in $HOME/.omp/agent/agents run through `task`, on by default; explicit -e paths
    load under --no-extensions (Pi mechanisms probe, findings 1, 2, 3, 4). Only `comms` passes a --tools allowlist:
    omp's default set and the aos tools; the lifecycle extension follows in both comms modes."""
    flags = [] if interactive else ["--print", "--mode", "json"]
    if provider:
        flags += ["--provider", provider]
    return flags + ["--model", model, "--session-dir", SESSION_DIR, "--no-extensions",
                    *([] if skills else ["--no-skills"]), "--no-rules", "--no-title", "--max-time", str(harness_timeout),
                    "--approval-mode=yolo", *(["--config", OMP_INTERACTIVE_CONFIG] if interactive else []),
                    *(["--tools=" + ",".join(OMP_TOOLS + (OMP_AOS_TOOLS_INTERACTIVE if interactive else OMP_AOS_TOOLS))]
                      if comms else []),
                    *([f"--skills={','.join(skills)}"] if skills else []),
                    *(flag for path in extensions for flag in ("-e", path)),
                    *(["-e", "/usr/local/lib/aos-agent/aos_pi.ts"] if comms else [])]


FLAVOURS: dict[str, Flavour] = {
    "pi": Flavour("pi", ("1.1.0",), True, (("PI_CODING_AGENT_DIR", AGENT_DIR),), _pi_flags, parse_output,
                  f"{AGENT_DIR}/auth.json", lambda auth: auth.snapshot()),
    # omp 18.8.3, measured in OpenShell 2026-10-12: prompt read from stdin with --print; PI_CODING_AGENT_DIR
    # is its agent directory and credentials come from agent.db there (auth.json is ignored); HOME must be
    # writable (natives, logs); events as Pi without agent_settled (a live repair ended at agent_end, exit 0).
    "omp": Flavour("omp", ("18.8.3",), False,
                   (("PI_CODING_AGENT_DIR", AGENT_DIR), ("HOME", "/tmp/pi-home")), _omp_flags, parse_omp_output,
                   f"{AGENT_DIR}/agent.db", lambda auth: auth.omp_store_snapshot()),
}


def check_bin(value: str) -> str:
    if not (BIN_TOKEN.fullmatch(value) or (BIN_PATH.fullmatch(value) and "/../" not in value + "/"
                                         and "/./" not in value + "/")):
        raise ValueError(f"AOS_OPENSHELL_PI_BIN must be a plain command name or an absolute path (got {value!r})")
    return value


def check_model(value: str) -> str:
    if not MODEL_TOKEN.fullmatch(value):
        raise ValueError("AOS_OPENSHELL_PI_MODEL must be a model name without spaces or a leading dash")
    return value


def invocation(flavour: str, *, bin: str, provider: str | None, model: str, session_id: str | None,
               harness_timeout: int, extensions: tuple[str, ...] = (), skills: tuple[str, ...] = (),
               comms: bool = False, automatic: bool = True, interactive: bool = False) -> Invocation:
    """The harness command for one run. Headless prompts use stdin; interactive omp takes one argument. The plugin
    arguments (AOS_OPENSHELL_PI_PLUGINS) are sandbox paths of extensions and skills as the flavour takes them (pi:
    directories, omp: names); `comms` adds the aos tools and lifecycle extension."""
    spec = FLAVOURS[flavour]
    if spec.session_id_flag and not _canonical_uuid(session_id):
        raise ValueError(f"a {flavour} run needs a canonical UUID session id")
    check_bin(bin)
    check_model(model)
    flags = spec.build(provider=provider, model=model, session_id=session_id if spec.session_id_flag else None,
                       harness_timeout=harness_timeout, extensions=extensions, skills=skills, comms=comms,
                       automatic=automatic, interactive=interactive)
    env = " ".join(f"{key}={value}" for key, value in spec.env)
    if comms:
        env += f" AOS_AGENT_AUTOMATIC={int(automatic)}"
    command = f"{env} {shlex.quote(bin)} " + " ".join(shlex.quote(flag) for flag in flags)
    if interactive:
        command += ' "$(cat /sandbox/prompt.txt)"'
    required = tuple(dict.fromkeys(flag.split("=", 1)[0] for flag in flags if flag.startswith("--") or flag == "-e"))
    return Invocation(command, required, session_id if spec.session_id_flag else None)


VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")


def capabilities(flavour: str, version_text: str, help_text: str, required: tuple[str, ...]) -> tuple[str | None, list[str], list[str]]:
    """Return (detected version, missing required flags, warnings) from ``--version`` and ``--help`` output."""
    match = VERSION_RE.search(version_text or "")
    version = match.group(0) if match else None
    missing = [flag for flag in required if not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text or "")]
    warnings = []
    tested = FLAVOURS[flavour].tested_versions
    if version is None:
        warnings.append(f"{flavour} version not detected; tested with {', '.join(tested)}")
    elif version not in tested:
        warnings.append(f"{flavour} {version} is untested; tested with {', '.join(tested)}")
    return version, missing, warnings


# -- worker auth ------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class PiAuth:
    access: str
    account_id: str
    expires_ms: int
    expiry_source: str           # "jwt" (min of declared and token exp) or "declared"
    secrets: tuple[str, ...] = dataclasses.field(repr=False)
    origin: str = "export"       # "export" (AOS_OPENSHELL_PI_AUTH) or "login" (AOS_OPENSHELL_PI_LOGIN, renewed by omp)

    def __repr__(self) -> str:   # never print token values
        return f"PiAuth(expires_ms={self.expires_ms}, expiry_source={self.expiry_source!r})"

    def _entry(self) -> dict[str, Any]:
        return {"access": self.access, "refresh": PLACEHOLDER_REFRESH, "expires": self.expires_ms,
                "accountId": self.account_id}

    def snapshot(self) -> bytes:
        """The Pi-schema auth.json uploaded into the sandbox: one provider, nonrenewable refresh value."""
        return json.dumps({AUTH_PROVIDER: {"type": "oauth", **self._entry()}}, separators=(",", ":")).encode()

    def omp_store_snapshot(self) -> bytes:
        """omp's credential store with the same single nonrenewable entry.

        omp ignores auth.json; it reads ``auth_credentials`` rows from the SQLite ``agent.db`` in its
        agent directory, ``data`` being the OAuth object without ``type`` (measured on omp 18.8.3). Only
        that table is created; omp adds its schema version, indexes and other tables on first open."""
        with tempfile.TemporaryDirectory(prefix="aos-omp-store-") as scratch:
            path = os.path.join(scratch, "agent.db")
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE auth_credentials (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, "
                             "credential_type TEXT NOT NULL, data TEXT NOT NULL, disabled_cause TEXT DEFAULT NULL, "
                             "identity_key TEXT DEFAULT NULL, "
                             "created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)), "
                             "updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)))")
                conn.execute("INSERT INTO auth_credentials (provider, credential_type, data) VALUES (?, ?, ?)",
                             (AUTH_PROVIDER, "oauth", json.dumps(self._entry(), separators=(",", ":"))))
                conn.commit()
            finally:
                conn.close()
            with open(path, "rb") as stream:
                return stream.read()


def _jwt_exp_ms(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeDecodeError):
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
        return None
    return int(exp * 1000)


def ensure_validity(auth: PiAuth, seconds: int) -> None:
    remaining = auth.expires_ms / 1000 - time.time()
    if remaining < seconds:
        note = "" if auth.expiry_source == "jwt" else " (access token is not a decodable JWT; declared expiry used)"
        raise AuthError(f"selected Pi auth is valid for {max(0, int(remaining))} s, less than the required {seconds} s"
                        f"{note}; " + ("export a freshly renewed worker access token" if auth.origin == "export" else
                                       "the renewed worker login does not cover the run; shorten "
                                       "AOS_HARNESS_TIMEOUT_SECONDS or retry the task"))


def selected_auth(path: str, min_validity_seconds: int = 300) -> PiAuth:
    """Select the openai-codex OAuth entry from a bounded regular Pi auth.json; never prints values."""
    try:
        info = os.stat(path)
    except OSError:
        raise AuthError("AOS_OPENSHELL_PI_AUTH is not readable") from None
    if not stat.S_ISREG(info.st_mode):
        raise AuthError("AOS_OPENSHELL_PI_AUTH is not a regular file")
    if info.st_size > AUTH_FILE_CAP:
        raise AuthError("AOS_OPENSHELL_PI_AUTH is larger than 1 MiB")
    try:
        with open(path, "rb") as stream:
            data = json.loads(stream.read(AUTH_FILE_CAP + 1).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise AuthError("AOS_OPENSHELL_PI_AUTH is not valid JSON") from None
    entry = data.get(AUTH_PROVIDER) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        raise AuthError(f"AOS_OPENSHELL_PI_AUTH has no {AUTH_PROVIDER} entry")
    if entry.get("type") != "oauth":
        raise AuthError(f"the {AUTH_PROVIDER} entry is not OAuth; API keys and commands are not accepted")
    return _entry_auth(entry, min_validity_seconds)


def _entry_auth(entry: dict, min_validity_seconds: int | None, origin: str = "export") -> PiAuth:
    """Validate one OAuth object (access, refresh, expires, accountId) and check its horizon unless None."""
    fields = {key: entry.get(key) for key in ("access", "refresh", "accountId")}
    for key, value in fields.items():
        if not isinstance(value, str) or not value.strip() or any(c.isspace() for c in value):
            raise AuthError(f"the {AUTH_PROVIDER} entry has no valid {key}")
    declared = entry.get("expires")
    if isinstance(declared, bool) or not isinstance(declared, (int, float)) or not math.isfinite(declared):
        raise AuthError(f"the {AUTH_PROVIDER} entry has no numeric millisecond expires")
    token_exp = _jwt_exp_ms(fields["access"])
    expires = int(min(declared, token_exp)) if token_exp is not None else int(declared)
    secrets = tuple(sorted({v for v in fields.values() if v != PLACEHOLDER_REFRESH}, key=len, reverse=True))
    auth = PiAuth(fields["access"], fields["accountId"], expires, "jwt" if token_exp is not None else "declared", secrets,
                  origin)
    if min_validity_seconds is not None:
        ensure_validity(auth, min_validity_seconds)
    return auth


# -- persistent worker login (omp's own credential store and refresh) ----------------------

LOGIN_DB = "agent.db"
LOGIN_LOCK = "aos-renew.lock"
LOGIN_LOCK_TIMEOUT = 300           # seconds an admission waits for another admission's renewal
LOGIN_REFRESH_TIMEOUT = 120        # seconds `omp token --force-refresh` may take
LOGIN_HINT = "log the worker in again: PI_CODING_AGENT_DIR=<AOS_OPENSHELL_PI_LOGIN> omp login " + AUTH_PROVIDER
TOKENISH = re.compile(r"[A-Za-z0-9_+/=.-]{32,}")


def _sanitize(text: str, secrets: tuple[str, ...], limit: int = 300) -> str:
    """Bounded one-line diagnostic: known secrets and every long token-shaped run replaced."""
    for secret in secrets:
        text = text.replace(secret, MARKER)
    text = " ".join(TOKENISH.sub(MARKER, text).split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _private_dir(directory: str) -> None:
    try:
        info = os.lstat(directory)
    except OSError:
        raise AuthError("AOS_OPENSHELL_PI_LOGIN is not a readable directory") from None
    if not stat.S_ISDIR(info.st_mode):
        raise AuthError("AOS_OPENSHELL_PI_LOGIN is not a directory (a symlink is not accepted)")
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise AuthError("AOS_OPENSHELL_PI_LOGIN must be owned by the runner user and private (mode 700)")
    db = os.path.join(directory, LOGIN_DB)
    try:
        info = os.lstat(db)
    except OSError:
        raise AuthError(f"AOS_OPENSHELL_PI_LOGIN holds no {LOGIN_DB}; {LOGIN_HINT}") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise AuthError(f"AOS_OPENSHELL_PI_LOGIN/{LOGIN_DB} must be a regular file owned by the runner user")


def _store_rows(db: str) -> list[tuple[int, Any, Any]]:
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        try:
            return conn.execute("SELECT id, data, disabled_cause FROM auth_credentials "
                                "WHERE provider = ? AND credential_type = 'oauth' ORDER BY id", (AUTH_PROVIDER,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        raise AuthError(f"AOS_OPENSHELL_PI_LOGIN/{LOGIN_DB} is not a readable omp credential store") from None


def _blocked_until(db: str, credential_id: int) -> int | None:
    """omp's own back-off after a failed refresh (``auth_credential_blocks``), in ms, if any."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        try:
            row = conn.execute("SELECT MAX(blocked_until_ms) FROM auth_credential_blocks WHERE credential_id = ?",
                               (credential_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row and isinstance(row[0], int) and row[0] > time.time() * 1000 else None


def read_login(directory: str) -> tuple[int, PiAuth]:
    """The single active openai-codex OAuth account of an omp store, without a horizon check."""
    db = os.path.join(directory, LOGIN_DB)
    rows = _store_rows(db)
    active = [row for row in rows if row[2] is None]
    if not active:
        if rows:
            causes = {_sanitize(str(row[2]), (), 80) for row in rows}
            raise AuthError(f"omp disabled the worker's {AUTH_PROVIDER} login ({'; '.join(sorted(causes))}); {LOGIN_HINT}")
        raise AuthError(f"AOS_OPENSHELL_PI_LOGIN has no {AUTH_PROVIDER} OAuth login; {LOGIN_HINT}")
    if len(active) > 1:
        raise AuthError(f"AOS_OPENSHELL_PI_LOGIN holds {len(active)} active {AUTH_PROVIDER} accounts; a dedicated "
                        "worker login holds exactly one")
    credential_id, data, _ = active[0]
    try:
        entry = json.loads(data)
    except (TypeError, ValueError):
        raise AuthError(f"the stored {AUTH_PROVIDER} login is not valid JSON") from None
    if not isinstance(entry, dict):
        raise AuthError(f"the stored {AUTH_PROVIDER} login is not an OAuth object")
    return credential_id, _entry_auth(entry, None, "login")


def login_auth(directory: str, min_validity_seconds: int, *, bin: str = "omp",
               lock_timeout: float = LOGIN_LOCK_TIMEOUT) -> PiAuth:
    """Select the worker's persistent omp login, renewing it first when it does not cover the horizon.

    The renewal is omp's own (``omp token openai-codex --force-refresh`` with ``PI_CODING_AGENT_DIR`` set to the
    login directory): omp refreshes, writes the new access token and any rotated refresh token into its
    ``agent.db`` and coordinates with its own refresh leases. AOS never writes the store. An exclusive lock on
    ``aos-renew.lock`` in the directory serializes admissions, so a waiting admission re-reads the store and reuses
    the credential the first one renewed. The token omp prints goes to /dev/null; the store is read back instead."""
    _private_dir(directory)
    db = os.path.join(directory, LOGIN_DB)
    try:
        lock = os.open(os.path.join(directory, LOGIN_LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise AuthError("cannot open the renewal lock in AOS_OPENSHELL_PI_LOGIN") from None
    try:
        deadline = time.monotonic() + lock_timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AuthError(f"another admission held the worker login's renewal lock for {int(lock_timeout)} s; "
                                    "retry later") from None
                time.sleep(0.2)
        credential_id, auth = read_login(directory)
        if auth.expires_ms / 1000 - time.time() >= min_validity_seconds:
            return auth
        env = {key: value for key, value in os.environ.items() if key not in ("OMP_PROFILE", "PI_CODING_AGENT_DIR")}
        env.update(PI_CODING_AGENT_DIR=directory, NO_COLOR="1")
        try:
            result = subprocess.run([bin, "token", AUTH_PROVIDER, "--force-refresh"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
                                    timeout=LOGIN_REFRESH_TIMEOUT)
            failure = None if result.returncode == 0 else (
                f"omp token exited {result.returncode}: "
                + (_sanitize(result.stderr[-4096:].decode("utf-8", "replace"), auth.secrets) or "no diagnostic"))
        except FileNotFoundError:
            failure = f"{bin} is not on this host"
        except subprocess.TimeoutExpired:
            failure = f"omp token did not finish within {LOGIN_REFRESH_TIMEOUT} s"
        except OSError:
            failure = f"{bin} could not be started"
        if failure is not None:
            blocked = _blocked_until(db, credential_id)
            hold = (f"; omp holds the credential back until "
                    f"{time.strftime('%H:%M:%S UTC', time.gmtime(blocked / 1000))}") if blocked else ""
            raise AuthError(f"renewing the worker login failed ({failure}){hold}. If the login was revoked or its "
                            f"refresh token expired, {LOGIN_HINT}; otherwise check the host's access to "
                            "auth.openai.com:443 and retry")
        _, renewed = read_login(directory)
        if renewed.account_id != auth.account_id:
            raise AuthError("the renewed worker login carries a different accountId than before the renewal")
        ensure_validity(renewed, min_validity_seconds)
        return renewed
    finally:
        os.close(lock)  # closing the descriptor releases the lock


def redact(text: str | None, auth: PiAuth | None) -> str | None:
    if text is None or auth is None:
        return text
    for secret in auth.secrets:  # longest first
        text = text.replace(secret, "[REDACTED]")
    return text


def redact_bytes(data: bytes, auth: PiAuth | None) -> bytes:
    if auth is None:
        return data
    for secret in auth.secrets:
        data = data.replace(secret.encode(), b"[REDACTED]")
    return data


MIN_FRAGMENT = 8   # shortest secret fragment at a cut edge that is treated as sensitive
MARKER = "[REDACTED]"


def _scrub_edges(text: str, auth: PiAuth | None) -> str:
    """Replace a leading suffix or trailing prefix of a secret (at least MIN_FRAGMENT characters): what a
    cut through a secret leaves behind when the text was truncated before it reached us."""
    if auth is None:
        return text
    for secret in auth.secrets:
        for size in range(min(len(secret) - 1, len(text)), MIN_FRAGMENT - 1, -1):
            if text.startswith(secret[-size:]):
                text = MARKER + text[size:]
                break
        for size in range(min(len(secret) - 1, len(text)), MIN_FRAGMENT - 1, -1):
            if text.endswith(secret[:size]):
                text = text[:-size] + MARKER
                break
    return text


def safe_text(text: str | None, auth: PiAuth | None) -> str | None:
    """Redact whole secrets and secret fragments at both edges (for text cut upstream)."""
    return None if text is None else _scrub_edges(redact(text, auth) or "", auth)


def safe_tail(text: str | None, limit: int, auth: PiAuth | None) -> str | None:
    """Redact, then keep the last ``limit`` characters, then scrub fragments at the edges."""
    if text is None:
        return None
    cut = (redact(text, auth) or "")[-limit:] if limit > 0 else ""
    cut = _scrub_edges(cut, auth)
    return cut[-limit:] if limit > 0 else ""


def safe_head(text: str | None, limit: int, auth: PiAuth | None) -> str | None:
    """Redact, then keep the first ``limit`` characters, then scrub fragments at the edges."""
    if text is None:
        return None
    cut = _scrub_edges((redact(text, auth) or "")[:limit], auth)
    return cut[:limit]


# -- result bundle check ----------------------------------------------------------------

def _git(repo: str, *args: str, input: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "-C", repo, *args], input=input, capture_output=True,
                            env=policy.git_env(os.environ), timeout=600)
    if result.returncode != 0:
        raise BundleError(f"git {args[0]} failed while checking the result bundle")
    return result.stdout


def validate_bundle(source: str, bundle: str, base_sha: str, auth: PiAuth | None) -> None:
    """Inspect every object the bundle's pack carries, reachable or not, before anything is imported.

    The bundle is unbundled (``index-pack --fix-thin``) into a temporary bare clone of the trusted source;
    every object in the resulting pack that the source does not already have gets the 64 MiB cap and the
    literal secret scan, because ``git fetch`` of the bundle keeps all of them. No worktree checkout and no
    repository code runs. The scan is an accident guard, not a defence against encoded exfiltration.
    """
    secrets = [s.encode() for s in (auth.secrets if auth else ())]
    with tempfile.TemporaryDirectory(prefix="aos-pi-bundle-") as scratch:
        check = os.path.join(scratch, "check.git")
        _git(scratch, "clone", "-q", "--bare", "--shared", "--no-tags", source, check)
        _git(check, "bundle", "unbundle", bundle)
        pack_dir = os.path.join(check, "objects", "pack")
        shas: list[bytes] = []
        for index in sorted(os.listdir(pack_dir)) if os.path.isdir(pack_dir) else []:
            if index.endswith(".idx"):
                with open(os.path.join(pack_dir, index), "rb") as stream:
                    listing = _git(check, "show-index", input=stream.read())
                shas += [line.split()[1] for line in listing.splitlines() if line.strip()]
        shas = list(dict.fromkeys(shas))
        if not shas:
            return
        # Drop objects the trusted source already holds (thin-pack bases added by --fix-thin).
        known = _git(source, "cat-file", "--batch-check=%(objectname) %(objecttype)", input=b"\n".join(shas) + b"\n")
        new = [line.split()[0] for line in known.splitlines() if line.endswith(b" missing")]
        if not new:
            return
        sizes = _git(check, "cat-file", "--batch-check=%(objectname) %(objectsize)", input=b"\n".join(new) + b"\n")
        for line in sizes.splitlines():
            sha, size = line.split()
            if int(size) > OBJECT_CAP:
                raise BundleError(f"result object {sha.decode()[:12]} exceeds the {OBJECT_CAP} byte object cap")
        if secrets:
            scan_objects(check, new, secrets)


def scan_objects(repo: str, shas: list[bytes], secrets: list[bytes]) -> None:
    """Stream `git cat-file --batch` one object at a time (each already bounded by OBJECT_CAP); BundleError names
    the first object carrying one of the secrets. Public: the OpenShell runner's result scan calls it as well
    (endpoint lane E7)."""
    proc = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=policy.git_env(os.environ))
    try:
        assert proc.stdin is not None and proc.stdout is not None
        for sha in shas:
            proc.stdin.write(sha + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            if len(header) != 3:
                raise BundleError("git cat-file failed while checking the result bundle")
            content = proc.stdout.read(int(header[2]))
            proc.stdout.read(1)
            if any(secret in content for secret in secrets):
                raise BundleError(f"result object {sha.decode()[:12]} contains a selected credential value")
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.kill()
        proc.wait()


_scan_objects = scan_objects  # the private name, kept for callers that still use it
