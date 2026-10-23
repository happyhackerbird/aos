from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from agent_os import sp_sync
from agent_os.sp_sync import (
    OPS_FILE, STATE_FILE, SYNC_FILE, Dropbox, DropboxError, PasswordRequired, SplitFormatError,
    SyncFileError, WrongPassword, decode, extract_tasks, fetch_state, load_state,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sp"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text())
PASSWORD = EXPECTED["password"]
ALL_TASK_IDS = ["task-1", "sub-1", "sub-2", "task-2", "task-3", "task-4", "task-5", "task-6", "task-7"]


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# -- decoding each fixture --------------------------------------------------

@pytest.mark.parametrize("name", ["v2-plain.txt", "v2-gzip.txt", "v2-encrypted.txt", "v2-gzip-encrypted.txt",
                                  "v2-legacy.txt"])
def test_each_v2_fixture_decodes_to_the_synthetic_state(name: str) -> None:
    data = decode(fixture(name), PASSWORD)
    assert data["version"] == 2 and data["syncVersion"] == 12
    assert data["state"]["task"]["ids"] == ALL_TASK_IDS
    assert data["state"]["task"]["entities"]["task-1"]["title"] == "Add retry to the poller"
    assert [p["title"] for p in data["state"]["project"]["entities"].values()] == ["AOS", "Other"]


def test_plain_fixtures_decode_without_a_password() -> None:
    assert decode(fixture("v2-plain.txt"), None)["version"] == 2
    assert decode(fixture("v2-gzip.txt").decode(), "")["version"] == 2


def test_extract_tasks_maps_the_records() -> None:
    state = load_state({SYNC_FILE: decode(fixture("v2-plain.txt"), None)})
    records = extract_tasks(state, project="AOS")
    assert [r["id"] for r in records] == EXPECTED["v2_aos_task_ids"]
    first = records[0]
    assert first == {
        "id": "task-1", "title": "Add retry to the poller",
        "notes": "Make the poller retry once before it reports an error.\n\n## Acceptance\n"
                 "- `uv run pytest -q tests/test_poller.py`\n* make lint\n\n## AOS\nharness: codex\ntier: 2\nhost: server",
        "tags": ["agent-os", "go"], "project": "AOS", "is_done": False,
        "subtasks": ["write the failing test", "wire the retry"],
    }
    by_id = {r["id"]: r for r in records}
    assert by_id["task-3"]["is_done"] is True
    assert by_id["task-2"]["notes"] == "Just the README, nothing else." and by_id["task-2"]["subtasks"] == []
    assert by_id["task-7"]["notes"] == "## AOS\ntier: 3\n"
    assert [r["id"] for r in extract_tasks(state, tag="go")] == ["task-1", "task-5"]
    assert [r["id"] for r in extract_tasks(state)] == EXPECTED["v2_task_ids"]
    assert extract_tasks(state, project="Nope") == []
    assert [r["id"] for r in extract_tasks(state, project="Other", tag="go")] == ["task-1", "task-4", "task-5"]


def test_extract_tasks_tolerates_missing_fields() -> None:
    state = {"task": {"ids": ["a"], "entities": {"a": {"id": "a", "title": "bare", "projectId": "p", "tagIds": ["gone"]}}},
             "project": {"ids": ["p"], "entities": {"p": {"id": "p", "title": "P"}}},
             "tag": {"ids": [], "entities": {}}}
    assert extract_tasks(state, project="P") == [{"id": "a", "title": "bare", "notes": "", "tags": ["gone"],
                                                 "project": "P", "is_done": False, "subtasks": []}]


@pytest.mark.parametrize("state", [
    None, {}, {"task": []},
    {"task": {}}, {"task": {"entities": []}},
    {"task": {"entities": {"a": {}, "b": None}}},
    {"task": {"entities": {}, "ids": "a"}},
    {"task": {"entities": {"a": {}}, "ids": ["a", None]}},
    {"task": {"entities": {}, "ids": ["gone"]}},
])
@pytest.mark.parametrize("reader", ["load_state", "extract_tasks", "task_ids"])
def test_malformed_authoritative_task_collection_is_rejected(state: Any, reader: str) -> None:
    with pytest.raises(SyncFileError):
        if reader == "load_state":
            load_state({SYNC_FILE: {"version": 2, "state": state}})
        else:
            getattr(sp_sync, reader)(state)


@pytest.mark.parametrize("ids", [None, []])
def test_explicit_empty_task_entities_are_authoritative(ids: list[str] | None) -> None:
    state: dict[str, Any] = {"task": {"entities": {}}}
    if ids is not None:
        state["task"]["ids"] = ids
    assert load_state({SYNC_FILE: {"version": 2, "state": state}}) == state
    assert extract_tasks(state) == [] and sp_sync.task_ids(state) == []


@pytest.mark.parametrize("ids", [None, [], ["b", "a"]])
def test_task_order_and_optional_models_remain_tolerant(ids: list[str] | None) -> None:
    state: dict[str, Any] = {
        "task": {"entities": {"a": {"title": "A"}, "b": {"title": "B"},
                              "child": {"parentId": "b"}, "c": {}}},
        "project": None,
        "tag": {"entities": {"valid": {"title": "Tag"}, "ignored": None}},
    }
    if ids is not None:
        state["task"]["ids"] = ids
    expected = ["a", "b", "child", "c"] if not ids else ["b", "a", "child", "c"]
    assert sp_sync.task_ids(state) == expected
    records = extract_tasks(state)
    assert [record["id"] for record in records] == [task_id for task_id in expected if task_id != "child"]
    assert records[-1] == {"id": "c", "title": "", "notes": "", "tags": [],
                           "project": "", "is_done": False, "subtasks": []}


# -- clear errors -------------------------------------------------------------

def test_wrong_password_is_a_clear_error() -> None:
    with pytest.raises(WrongPassword, match="wrong password"):
        decode(fixture("v2-encrypted.txt"), "not-the-password")
    with pytest.raises(WrongPassword, match="wrong password"):
        decode(fixture("v2-legacy.txt"), "not-the-password")


def test_missing_password_on_an_encrypted_file_names_the_setting() -> None:
    for password in (None, ""):
        with pytest.raises(PasswordRequired, match="AOS_SP_SYNC_PASSWORD"):
            decode(fixture("v2-gzip-encrypted.txt"), password)


def test_truncated_files_are_clear_errors() -> None:
    with pytest.raises(SyncFileError, match="gzip body is corrupt or truncated"):
        decode(fixture("v2-gzip.txt")[:400], None)
    with pytest.raises(SyncFileError, match="not JSON"):
        decode(fixture("v2-plain.txt")[:400], None)
    with pytest.raises(SyncFileError, match="too short"):
        decode(fixture("v2-encrypted.txt")[:len("pf_E4.5__") + 24], PASSWORD)  # 18 bytes, below the 28-byte minimum
    with pytest.raises(WrongPassword, match="corrupt"):
        decode(fixture("v2-encrypted.txt")[:len("pf_E4.5__") + 800], PASSWORD)  # valid base64, broken auth tag
    with pytest.raises(SyncFileError, match="base64"):
        decode("pf_E4.5__###", PASSWORD)


def test_unknown_prefix_is_a_clear_error() -> None:
    with pytest.raises(SyncFileError, match=r"expected the pf_ prefix.*head shape json"):
        decode('{"version": 2}', None)
    with pytest.raises(SyncFileError, match="head shape markup"):
        decode("<html><body>Service unavailable</body></html>", None)
    with pytest.raises(SyncFileError, match="head shape other"):
        decode("pf_X4.5__{}", None)
    with pytest.raises(SyncFileError, match="not UTF-8"):
        decode(b"\xff\xfe", None)
    with pytest.raises(SyncFileError, match="not a JSON object"):
        decode("pf_4.5__[1, 2]", None)


def test_split_format_is_refused_with_the_cause_and_the_setting() -> None:
    tombstone = decode(fixture("v3/sync-data.json"), None)
    ops = decode(fixture("v3/sync-ops.json"), None)
    snapshot = decode(fixture("v3/sync-state__5__0123456789abcdef.json"), None)
    assert tombstone == {"version": 3, "format": "split", "migratedAt": 1760000005000,
                         "note": "migrated to split sync files"}
    with pytest.raises(SplitFormatError, match="migration tombstone") as info:
        load_state({SYNC_FILE: tombstone, OPS_FILE: ops, STATE_FILE: snapshot})
    assert "split" in str(info.value) and "sync settings" in str(info.value) and "nothing changed on the bus" in str(info.value)
    with pytest.raises(SplitFormatError, match=r"sync-ops\.json \(syncVersion 6, snapshot sync-state__5__0123456789abcdef\.json\)"):
        load_state({OPS_FILE: ops})
    with pytest.raises(SyncFileError, match="unsupported version 7"):
        load_state({SYNC_FILE: {"version": 7}})
    with pytest.raises(SyncFileError, match="no state snapshot"):
        load_state({SYNC_FILE: {"version": 2}})
    with pytest.raises(SyncFileError, match="no sync-data.json"):
        load_state({})


# -- Dropbox ------------------------------------------------------------------

class FakeDropbox:
    """The two Dropbox calls the reader makes, as an httpx mock transport."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.tokens = 0
        self.downloads: list[str] = []
        self.expire_first = False
        self.refresh_status = 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["rt-1"]
            assert form["client_id"] == ["key-1"] and form["client_secret"] == ["secret-1"]
            assert "Authorization" not in request.headers
            self.tokens += 1
            if self.refresh_status != 200:
                return httpx.Response(self.refresh_status, json={"error": "invalid_grant",
                                                                  "error_description": "refresh token revoked"})
            return httpx.Response(200, json={"access_token": f"at-{self.tokens}", "expires_in": 14400,
                                             "token_type": "bearer"})
        assert request.url.path == "/2/files/download" and request.method == "POST"
        assert request.headers["Content-Type"] == "application/octet-stream"
        path = json.loads(request.headers["Dropbox-API-Arg"])["path"]
        self.downloads.append(path)
        if request.headers["Authorization"] != f"Bearer at-{self.tokens}" or self.tokens == 0:
            return httpx.Response(401, json={"error_summary": "invalid_access_token/...", "error": {".tag": "invalid_access_token"}})
        if self.expire_first and len(self.downloads) == 1:
            return httpx.Response(401, json={"error_summary": "expired_access_token/", "error": {".tag": "expired_access_token"}})
        if path not in self.files:
            return httpx.Response(409, json={"error_summary": "path/not_found/..", "error": {".tag": "path", "path": {".tag": "not_found"}}})
        return httpx.Response(200, content=self.files[path],
                              headers={"dropbox-api-result": json.dumps({"rev": "0123", "size": len(self.files[path]),
                                                                          "name": path.rsplit("/", 1)[-1]})})


def make_dropbox(fake: FakeDropbox) -> Dropbox:
    return Dropbox("key-1", "secret-1", "rt-1", client=httpx.Client(transport=httpx.MockTransport(fake.handle)))


def test_fetch_state_downloads_the_legacy_file_with_a_refreshed_token() -> None:
    fake = FakeDropbox({"/Apps/sp/sync-data.json": fixture("v2-gzip-encrypted.txt")})
    state = fetch_state(make_dropbox(fake), "/Apps/sp/", PASSWORD)
    assert [r["id"] for r in extract_tasks(state, project="AOS")] == EXPECTED["v2_aos_task_ids"]
    assert fake.tokens == 1 and fake.downloads == ["/Apps/sp/sync-data.json"]


def test_dropbox_refreshes_once_on_an_expired_token_and_reuses_the_token() -> None:
    fake = FakeDropbox({"/sync-data.json": fixture("v2-plain.txt")})
    fake.expire_first = True
    dropbox = make_dropbox(fake)
    assert fetch_state(dropbox, "", None)["task"]["ids"] == ALL_TASK_IDS
    assert fake.tokens == 2 and fake.downloads == ["/sync-data.json"] * 2
    fetch_state(dropbox, "/", None)
    assert fake.tokens == 2 and len(fake.downloads) == 3


def test_fetch_state_without_any_sync_file_names_the_folder() -> None:
    fake = FakeDropbox({})
    with pytest.raises(SyncFileError, match=r"no sync files in Dropbox folder /Apps/sp: neither sync-data\.json nor sync-ops\.json"):
        fetch_state(make_dropbox(fake), "/Apps/sp", None)
    assert fake.downloads == ["/Apps/sp/sync-data.json", "/Apps/sp/sync-ops.json"]


def test_fetch_state_refuses_the_split_folder_precisely() -> None:
    v3 = {f"/sp/{name}": fixture(f"v3/{name}") for name in
          ("sync-data.json", "sync-ops.json", "sync-state.json", "sync-state__5__0123456789abcdef.json")}
    fake = FakeDropbox(v3)
    with pytest.raises(SplitFormatError, match="migration tombstone"):
        fetch_state(make_dropbox(fake), "/sp", None)
    assert fake.downloads == ["/sp/sync-data.json"]
    del v3["/sp/sync-data.json"]
    fake = FakeDropbox(v3)
    with pytest.raises(SplitFormatError, match=r"sync-ops\.json \(syncVersion 6"):
        fetch_state(make_dropbox(fake), "/sp", None)
    assert fake.downloads == ["/sp/sync-data.json", "/sp/sync-ops.json"]


def test_dropbox_errors_are_reported_without_the_token() -> None:
    fake = FakeDropbox({})
    fake.refresh_status = 400
    with pytest.raises(DropboxError, match="refresh token revoked") as info:
        fetch_state(make_dropbox(fake), "/sp", None)
    assert "rt-1" not in str(info.value) and "secret-1" not in str(info.value)

    def server_error(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 1})
        return httpx.Response(500, text="boom")
    dropbox = Dropbox("k", "s", "r", client=httpx.Client(transport=httpx.MockTransport(server_error)))
    with pytest.raises(DropboxError, match="Dropbox files/download returned 500"):
        dropbox.download_file("/x")


def test_auth_prints_the_offline_url_and_the_refresh_token_once(capsys: pytest.CaptureFixture[str]) -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.dropboxapi.com/oauth2/token"
        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "at", "expires_in": 14400, "refresh_token": "rt-new",
                                         "token_type": "bearer", "scope": "files.content.read", "uid": "1",
                                         "account_id": "dbid:x"})
    answers = iter(["  the-code  "])
    code = sp_sync.main(["auth", "--app-key", "key-9", "--app-secret", "sec-9"],
                        client=httpx.Client(transport=httpx.MockTransport(handle)), read=lambda prompt: next(answers))
    out = capsys.readouterr().out
    assert code == 0
    assert "https://www.dropbox.com/oauth2/authorize?client_id=key-9&response_type=code&token_access_type=offline" in out
    assert "AOS_SP_DROPBOX_REFRESH_TOKEN=rt-new" in out and "shown once" in out
    assert seen == {"grant_type": ["authorization_code"], "code": ["the-code"], "client_id": ["key-9"],
                    "client_secret": ["sec-9"]}


def test_auth_reports_a_rejected_code(capsys: pytest.CaptureFixture[str]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "code has expired"})
    code = sp_sync.main(["auth", "--app-key", "k", "--app-secret", "s"],
                        client=httpx.Client(transport=httpx.MockTransport(handle)), read=lambda prompt: "c")
    assert code == 1 and "code has expired" in capsys.readouterr().err
