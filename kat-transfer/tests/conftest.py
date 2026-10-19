"""Shared fixtures.

Every test builds its own KAT database in a tmp directory. The remote is a
real PostgreSQL: a `kat_gateway_test` database created in the local Docker container
with the test compose override, and emptied before
each test. The demo database (`csit321`) is never touched.

Point the tests at another server with KAT_TEST_PG_HOST / _PORT / _USER /
_PASSWORD. If PostgreSQL is unreachable, tests that need it are skipped.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
import sys
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from katxfer.config import (  # noqa: E402
    LocalConfig,
    RemoteConfig,
    ServiceConfig,
    ZmqConfig,
)
from katgateway.config import DatabaseConfig, Settings
from katxfer.fakedata import fmt_ts  # noqa: E402

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"
KAT_SCHEMA = (SCHEMA_DIR / "kat_sqlite.sql").read_text()
REMOTE_SCHEMA = (SCHEMA_DIR / "remote_postgres.sql").read_text()
TEST_DB = "kat_gateway_test"
WRITE_TOKEN = "test-write-token-0123456789-abcdef"
READ_TOKEN = "test-read-token-0123456789-abcdefg"


def seed(db: Path, runs: int = 2, rows: int = 10, env: int = 5) -> None:
    """A tiny KAT database with everything untransferred."""
    conn = sqlite3.connect(db)
    conn.executescript(KAT_SCHEMA)
    t0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    for run in range(1, runs + 1):
        conn.execute(
            "INSERT INTO Experiment (systemid, run, description) VALUES (?,?,?)",
            ("K4-RIG-01", run, f"test run {run}"),
        )
        for row in range(1, rows + 1):
            conn.execute(
                'INSERT INTO ExperimentalData '
                '(systemid, run, "row", timestamp, source, data, xfer) '
                "VALUES (?,?,?,?,?,?,NULL)",
                (
                    "K4-RIG-01",
                    run,
                    row,
                    fmt_ts(t0 + timedelta(seconds=row)),
                    "ADC-CH0",
                    json.dumps({"run": run, "row": row, "v": 1.0}),
                ),
            )
    for i in range(env):
        conn.execute(
            "INSERT INTO Environment (systemid, timestamp, data, xfer) "
            "VALUES (?,?,?,NULL)",
            ("K4-RIG-01", fmt_ts(t0 + timedelta(minutes=i)), json.dumps({"ambient_c": 20 + i})),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def kat_db(tmp_path: Path) -> Path:
    db = tmp_path / "kat.sqlite"
    seed(db)
    return db


def _connect(remote: DatabaseConfig, dbname: str | None = None):
    import psycopg2

    return psycopg2.connect(
        host=remote.host,
        port=remote.port,
        dbname=dbname or remote.database,
        user=remote.user,
        password=remote.password,
        connect_timeout=remote.connect_timeout_s,
    )


@pytest.fixture(scope="session")
def pg_remote():
    """Create a fresh `kat_gateway_test` database for this test session."""
    admin = DatabaseConfig(
        host=os.environ.get("KAT_TEST_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("KAT_TEST_PG_PORT", "25432")),
        database="csit321",
        user=os.environ.get("KAT_TEST_PG_USER", "kat"),
        password=os.environ.get("KAT_TEST_PG_PASSWORD", "kat-local-demo-only"),
        connect_timeout_s=3,
    )
    try:
        conn = _connect(admin)
    except Exception as exc:  # noqa: BLE001 - ImportError or OperationalError
        if os.environ.get("KAT_REQUIRE_PG") == "1":
            pytest.fail(f"Required PostgreSQL integration database unavailable: {type(exc).__name__}")
        pytest.skip(
            f"PostgreSQL not reachable at {admin.host}:{admin.port} ({exc}). "
            "Start it with: docker compose -f docker-compose.yml -f docker-compose.test.yml up -d"
        )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        cur.execute(f"CREATE DATABASE {TEST_DB}")
    conn.close()

    remote = dataclasses.replace(admin, database=TEST_DB)
    conn = _connect(remote)
    with conn.cursor() as cur:
        cur.execute(REMOTE_SCHEMA)
    conn.commit()
    conn.close()
    yield remote


@pytest.fixture()
def remote(pg_remote: DatabaseConfig) -> DatabaseConfig:
    """The test database, emptied so every test starts from nothing."""
    conn = _connect(pg_remote)
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE api_token, api_user, experiment, experimental_data, environment, transfer_log "
            "RESTART IDENTITY CASCADE"
        )
        cur.execute(
            "INSERT INTO api_user (username, max_role, granted_by) VALUES "
            "('test-writer', 'write', 'test-admin'), ('test-reader', 'read', 'test-admin')"
        )
        cur.executemany(
            """INSERT INTO api_token (username, role, token_hash, token_prefix)
               VALUES (%s, %s, %s, %s)""",
            [
                ("test-writer", "write", hashlib.sha256(WRITE_TOKEN.encode()).hexdigest(), WRITE_TOKEN[:8]),
                ("test-reader", "read", hashlib.sha256(READ_TOKEN.encode()).hexdigest(), READ_TOKEN[:8]),
            ],
        )
    conn.commit()
    conn.close()
    return pg_remote


@pytest.fixture()
def cfg(kat_db: Path, remote: DatabaseConfig, api_url) -> ServiceConfig:
    return ServiceConfig(
        local=LocalConfig(path=kat_db, batch_size=7, busy_timeout_s=5.0),
        remote=RemoteConfig(url=api_url, token=WRITE_TOKEN),
        zmq=ZmqConfig(enabled=False),
        sweep_interval_s=1.0,
        notify_debounce_s=0.0,
        origin="fake",
        src_host="testhost",
    )


def remote_query(remote: DatabaseConfig, sql: str) -> list[tuple]:
    conn = _connect(remote)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.close()


def remote_count(remote: DatabaseConfig, table: str) -> int:
    return remote_query(remote, f"SELECT COUNT(*) FROM {table}")[0][0]


def local_pending(path: Path) -> tuple[int, int]:
    conn = sqlite3.connect(path)
    try:
        a = conn.execute(
            "SELECT COUNT(*) FROM ExperimentalData WHERE xfer IS NULL"
        ).fetchone()[0]
        b = conn.execute(
            "SELECT COUNT(*) FROM Environment WHERE xfer IS NULL"
        ).fetchone()[0]
        return a, b
    finally:
        conn.close()


@pytest.fixture(scope="session")
def api_url(pg_remote):
    """Real HTTP transport over loopback, backed by the isolated test archive."""
    import uvicorn
    from katgateway.app import create_app
    app = create_app(Settings(pg_remote, issuer=pg_remote))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(5)
        pytest.fail("Test HTTP gateway failed to start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
