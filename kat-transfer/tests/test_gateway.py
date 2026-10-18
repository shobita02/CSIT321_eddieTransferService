"""Access control, validation and failure ordering at the HTTP boundary."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import httpx
import psycopg2
import pytest
from fastapi.testclient import TestClient

from conftest import READ_TOKEN, WRITE_TOKEN, local_pending, remote_count, _connect
from katgateway.app import create_app
from katgateway.config import DatabaseConfig, Settings
from katgateway.models import Receipt
from katgateway.storage import ArchiveStore
from katxfer.config import LocalConfig, RemoteConfig, ServiceConfig, ZmqConfig, load
from katxfer.service import TransferService
from katxfer.sinks import GatewayError, HttpSink

HEADERS = {"Authorization": "Bearer " + WRITE_TOKEN}
READ_HEADERS = {"Authorization": "Bearer " + READ_TOKEN}


def batch(rows, **extra):
    return {"origin": "fake", "src_host": "testhost", "started_at": "2026-10-13T00:00:00Z",
            "trigger": "manual", "rows": rows, **extra}


class RecordingStore:
    def __init__(self):
        self.writes = []

    def counts(self):
        return {"experiment": 0, "experimental_data": 0, "environment": 0}

    def records(self, *args, **kwargs):
        return []

    def find_token(self, token_hash):
        identities = {
            hashlib.sha256(WRITE_TOKEN.encode()).hexdigest(): (2, "writer", "write", "write"),
            hashlib.sha256(READ_TOKEN.encode()).hexdigest(): (1, "reader", "read", "read"),
        }
        return identities.get(token_hash)

    def write_batch(self, table, payload, token_id):
        self.writes.append((table, payload, token_id))
        return Receipt(sent=len(payload.rows), applied=len(payload.rows))


@pytest.fixture
def client():
    store = RecordingStore()
    app = create_app(Settings(DatabaseConfig()), store)
    with TestClient(app) as client:
        yield client, store


def test_authentication_and_read_only_permissions(client, caplog):
    api, store = client
    assert api.get("/health").status_code == 200
    assert api.get("/v1/status").status_code == 401
    assert api.get("/v1/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.get("/v1/status", headers=READ_HEADERS).status_code == 200
    body = batch([{"systemid": "RIG", "run": 1}])
    assert api.post("/v1/ingest/experiments", json=body, headers=READ_HEADERS).status_code == 403
    assert not store.writes
    assert api.post("/v1/ingest/experiments", json=body, headers=HEADERS).json() == {
        "committed": True, "sent": 1, "applied": 1}
    assert store.writes[-1][2] == 2
    leaked_token = "a-never-logged-invalid-bearer"
    response = api.get("/v1/status", headers={"Authorization": "Bearer " + leaked_token})
    assert response.status_code == 401
    assert leaked_token not in response.text
    assert leaked_token not in caplog.text


@pytest.mark.parametrize("changes", [
    {"data": "not json"}, {"data": "{\"v\":NaN}"}, {"ts": None},
    {"systemid": "x" * 21}, {"systemid": ""}, {"surprise": "extra"},
    {"ts": "2026-10-13T00:00:00"},
])
def test_invalid_environment_never_reaches_database(client, changes):
    api, store = client
    row = {"systemid": "RIG", "ts": "2026-10-13T00:00:00Z", "data": '{"v":1}'}
    row.update(changes)
    response = api.post("/v1/ingest/environment", headers=HEADERS,
                        json=batch([row], xfer="2026-10-13T00:00:01Z"))
    assert response.status_code == 422
    assert not store.writes
    assert all("input" not in error for error in response.json()["detail"])


def test_bounded_requests_and_reads(client):
    api, store = client
    rows = [{"systemid": "RIG", "run": 1}] * 1001
    assert api.post("/v1/ingest/experiments", headers=HEADERS, json=batch(rows)).status_code == 422
    assert api.post("/v1/ingest/experiments", headers=HEADERS,
                    content=b" " * (8 * 1024 * 1024 + 1)).status_code == 413
    assert api.get("/v1/experiments?limit=1001", headers=READ_HEADERS).status_code == 422
    assert api.get("/v1/experiments?offset=-1", headers=READ_HEADERS).status_code == 422
    assert api.get("/v1/environment?from_ts=2026-10-14T00:00:00Z&to_ts=2026-10-13T00:00:00Z",
                   headers=READ_HEADERS).status_code == 422
    assert not store.writes


def test_database_failure_does_not_disclose_details(client, monkeypatch):
    api, store = client
    def fail():
        raise psycopg2.OperationalError("SECRET password and SQL payload")
    monkeypatch.setattr(store, "counts", fail)
    response = api.get("/v1/status", headers=READ_HEADERS)
    assert response.status_code == 503
    assert "SECRET" not in response.text


def test_openapi_includes_auth_and_ingestion_models(client):
    api, _ = client
    schema = api.get("/openapi.json").json()
    assert "/v1/ingest/experimental-data" in schema["paths"]
    assert schema["paths"]["/v1/ingest/experiments"]["post"]["security"]


@pytest.mark.parametrize("response", [
    {"committed": False, "sent": 7, "applied": 7},
    {"committed": True, "sent": 6, "applied": 6},
    {"committed": True, "sent": 7, "applied": 0},
    {"committed": True, "sent": True, "applied": True},
])
def test_unconfirmed_receipt_leaves_sqlite_pending(kat_db, monkeypatch, response):
    cfg = ServiceConfig(LocalConfig(kat_db, batch_size=7),
                        RemoteConfig(token=WRITE_TOKEN), ZmqConfig(enabled=False))
    def handler(request):
        body = json.loads(request.content)
        receipt = {"committed": True, "sent": len(body["rows"]), "applied": len(body["rows"])}
        return httpx.Response(200, json=response if request.url.path.endswith("experimental-data") else receipt)
    def connect(sink):
        sink._client = httpx.Client(base_url=cfg.remote.url + "/", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(HttpSink, "connect", connect)
    with pytest.raises(GatewayError):
        TransferService(cfg).run_once()
    assert local_pending(kat_db) == (20, 5)


def test_client_starts_without_postgres_driver():
    probe = "import katxfer.service, sys; assert 'psycopg2' not in sys.modules; assert 'katgateway' not in sys.modules"
    subprocess.run([sys.executable, "-c", probe], cwd=Path(__file__).resolve().parent.parent, check=True)


def test_config_migration_and_token_privacy(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="HTTPS"):
        RemoteConfig(url="http://192.168.40.100", token=WRITE_TOKEN)
    with pytest.raises(ValueError, match="KAT_API_WRITE_TOKEN"):
        RemoteConfig(token="${KAT_API_WRITE_TOKEN}")
    assert WRITE_TOKEN not in repr(RemoteConfig(token=WRITE_TOKEN))
    path = tmp_path / "old.toml"
    path.write_text('[remote]\nhost="127.0.0.1"\nuser="kat"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Direct PostgreSQL"):
        load(path)
    assert Settings(DatabaseConfig()).database == DatabaseConfig()


def test_read_filters_and_payload_preservation(cfg, remote):
    TransferService(cfg).run_once()
    with TestClient(create_app(Settings(remote))) as api:
        response = api.get("/v1/experimental-data", headers=READ_HEADERS,
                           params={"systemid": "K4-RIG-01", "run": 1, "limit": 3, "offset": 2})
        items = response.json()["items"]
        assert [r["row_no"] for r in items] == [3, 4, 5]
        assert all(r["run"] == 1 for r in items)
        assert items[0]["data"] == '{"run": 1, "row": 3, "v": 1.0}'
        assert api.get("/v1/experiments/K4-RIG-01/1", headers=READ_HEADERS).status_code == 200
        assert api.get("/v1/experiments/absent/1", headers=READ_HEADERS).status_code == 404
        assert api.get("/v1/experimental-data", headers=READ_HEADERS,
                       params={"systemid": "' OR 1=1 --"}).json()["items"] == []
        env = api.get("/v1/environment", headers=READ_HEADERS,
                      params={"from_ts": "2026-10-01T09:01:00Z", "to_ts": "2026-10-01T09:02:00Z"})
        assert len(env.json()["items"]) == 2


def test_lost_acknowledgement_retries_without_duplicates(cfg, remote, monkeypatch):
    original = HttpSink.request
    def lost(sink, method, path, **kwargs):
        receipt = original(sink, method, path, **kwargs)
        if method == "POST" and path.endswith("experimental-data"):
            raise GatewayError("Simulated lost response after database commit")
        return receipt
    monkeypatch.setattr(HttpSink, "request", lost)
    with pytest.raises(GatewayError):
        TransferService(cfg).run_once()
    assert local_pending(cfg.local.path) == (20, 5)
    assert remote_count(remote, "experimental_data") == 7
    monkeypatch.setattr(HttpSink, "request", original)
    assert TransferService(cfg).run_once() == 25
    assert remote_count(remote, "experimental_data") == 20
    assert remote_count(remote, "environment") == 5
    assert local_pending(cfg.local.path) == (0, 0)


def test_audit_failure_rolls_back_data_and_leaves_local_pending(cfg, remote):
    conn = _connect(remote)
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE transfer_log ADD CONSTRAINT reject_test_rows CHECK (rows_sent < 0)")
        conn.commit()
        with pytest.raises(GatewayError):
            TransferService(cfg).run_once()
        assert remote_count(remote, "experimental_data") == 0
        assert local_pending(cfg.local.path) == (20, 5)
    finally:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE transfer_log DROP CONSTRAINT reject_test_rows")
        conn.commit()
        conn.close()


def test_missing_parent_rejects_entire_batch(remote):
    app = create_app(Settings(remote))
    with TestClient(app) as api:
        rows = [{"systemid": "RIG", "run": 999, "row_no": 1,
                 "ts": None, "data": '{}'}]
        response = api.post("/v1/ingest/experimental-data", headers=HEADERS,
                            json=batch(rows, xfer="2026-10-13T00:00:01Z"))
        assert response.status_code == 409
    assert remote_count(remote, "experimental_data") == 0
    assert remote_count(remote, "transfer_log") == 0


def test_revoked_expired_and_inactive_tokens_are_rejected(remote):
    conn = _connect(remote)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE api_token SET revoked_at = now() WHERE token_hash = %s",
                (hashlib.sha256(WRITE_TOKEN.encode()).hexdigest(),),
            )
            cur.execute(
                "UPDATE api_token SET expires_at = now() - interval '1 second' WHERE token_hash = %s",
                (hashlib.sha256(READ_TOKEN.encode()).hexdigest(),),
            )
        conn.commit()
    finally:
        conn.close()
    with TestClient(create_app(Settings(remote))) as api:
        assert api.get("/v1/status", headers=HEADERS).status_code == 401
        assert api.get("/v1/status", headers=READ_HEADERS).status_code == 401


def test_user_deactivation_and_role_downgrade_apply_to_existing_tokens(remote):
    store = ArchiveStore(remote)
    with TestClient(create_app(Settings(remote))) as api:
        assert store.set_user_role("test-writer", "read", "test-admin")
        assert api.get("/v1/status", headers=HEADERS).status_code == 200
        assert api.post("/v1/ingest/experiments", headers=HEADERS,
                        json=batch([{"systemid": "RIG", "run": 1}])).status_code == 403
        assert store.set_user_role("test-writer", "write", "test-admin")
        assert store.disable_user("test-writer")
        assert api.get("/v1/status", headers=HEADERS).status_code == 401


def test_separate_write_tokens_are_audited_and_only_hashes_are_stored(remote):
    second_token = "second-write-token-not-stored-012345"
    second_hash = hashlib.sha256(second_token.encode()).hexdigest()
    conn = _connect(remote)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_user (username, max_role, granted_by) VALUES ('second-writer', 'write', 'admin')"
            )
            cur.execute(
                """INSERT INTO api_token (username, role, token_hash, token_prefix)
                   VALUES ('second-writer', 'write', %s, %s)""",
                (second_hash, second_token[:8]),
            )
            cur.execute("SELECT id FROM api_token WHERE token_hash = %s", (second_hash,))
            second_id = cur.fetchone()[0]
            cur.execute("SELECT id FROM api_token WHERE token_hash = %s",
                        (hashlib.sha256(WRITE_TOKEN.encode()).hexdigest(),))
            first_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    with TestClient(create_app(Settings(remote))) as api:
        payload = batch([{"systemid": "RIG", "run": 1}])
        assert api.post("/v1/ingest/experiments", headers=HEADERS, json=payload).status_code == 200
        assert api.post("/v1/ingest/experiments",
                        headers={"Authorization": "Bearer " + second_token}, json=payload).status_code == 200
        transfers = api.get("/v1/transfers", headers=HEADERS).json()["items"]
    assert [(row["api_token_id"], row["uploaded_by"]) for row in transfers] == [
        (first_id, "test-writer"), (second_id, "second-writer")
    ]
    rows = remote_query(remote, "SELECT token_hash FROM api_token ORDER BY id")
    hashes = {row[0].strip() for row in rows}
    assert second_hash in hashes
    assert second_token not in hashes


def test_token_admin_storage_create_list_and_revoke(remote):
    store = ArchiveStore(remote)
    store.add_user("read-only", "read", "test-admin")
    with pytest.raises(ValueError, match="not permitted"):
        store.create_token("read-only", "write")

    token_id, token = store.create_token("read-only", "read", note="research laptop")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    listed = next(token_row for token_row in store.list_tokens() if token_row["id"] == token_id)
    assert listed["token_prefix"] == token[:8]
    assert listed["role"] == "read"
    assert token not in repr(listed)
    assert remote_query(remote, "SELECT token_hash FROM api_token WHERE id = %s" % token_id)[0][0].strip() == token_hash

    with TestClient(create_app(Settings(remote))) as api:
        headers = {"Authorization": "Bearer " + token}
        assert api.get("/v1/status", headers=headers).status_code == 200
        assert store.revoke_token(token[:8])
        assert api.get("/v1/status", headers=headers).status_code == 401
    assert not store.revoke_token(str(token_id))

    _, disabled_token = store.create_token("read-only", "read")
    assert store.disable_user("read-only")
    with TestClient(create_app(Settings(remote))) as api:
        assert api.get("/v1/status", headers={"Authorization": "Bearer " + disabled_token}).status_code == 401
    assert all(row["revoked_at"] is not None
               for row in store.list_tokens() if row["username"] == "read-only")
