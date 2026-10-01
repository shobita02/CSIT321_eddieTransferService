"""Remote sinks.

Two implementations behind one interface:

* `PostgresSink`  - the real target on the Bored Owl dev server.
* `SqliteMockSink` - a local file that mimics it, so the whole pipeline can be
  developed and demonstrated before VPN access exists. It implements the same
  constraints (primary keys, the digest key on environment) so that a bug
  caught here is a bug that would have happened on the real server.

Every write is an upsert on a key the client can compute, which is what makes
the "stamp xfer only after remote commit" ordering safe: a replayed batch
updates rows that are already there instead of duplicating them.
"""

from __future__ import annotations

import logging
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import RemoteConfig
from .fakedata import parse_ts
from .localdb import EnvironmentRow, ExperimentalRow, ExperimentRow

log = logging.getLogger(__name__)


@dataclass
class WriteResult:
    sent: int
    applied: int


class RemoteSink(ABC):
    def __init__(self, origin: str, src_host: str) -> None:
        self.origin = origin
        self.src_host = src_host

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def upsert_experiments(self, rows: list[ExperimentRow]) -> WriteResult: ...

    @abstractmethod
    def upsert_experimental(
        self, rows: list[ExperimentalRow], xfer: datetime
    ) -> WriteResult: ...

    @abstractmethod
    def upsert_environment(
        self, rows: list[EnvironmentRow], xfer: datetime
    ) -> WriteResult: ...

    @abstractmethod
    def log_transfer(
        self,
        started_at: datetime,
        table_name: str,
        rows_sent: int,
        rows_applied: int,
        trigger: str,
        ok: bool,
        detail: str | None = None,
    ) -> None: ...

    @abstractmethod
    def counts(self) -> dict[str, int]: ...

    def __enter__(self) -> "RemoteSink":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _ts(value: str | None) -> datetime | None:
    return parse_ts(value) if value else None


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class PostgresSink(RemoteSink):
    def __init__(self, cfg: RemoteConfig, origin: str, src_host: str) -> None:
        super().__init__(origin, src_host)
        self.cfg = cfg
        self._conn = None

    def connect(self) -> None:
        import psycopg2  # imported late so the mock path needs no driver

        self._conn = psycopg2.connect(
            host=self.cfg.host,
            port=self.cfg.port,
            dbname=self.cfg.database,
            user=self.cfg.user,
            password=self.cfg.password,
            sslmode=self.cfg.sslmode,
            connect_timeout=self.cfg.connect_timeout_s,
            application_name="kat-transfer",
        )
        self._conn.autocommit = False
        log.info(
            "connected to postgres %s:%s/%s",
            self.cfg.host,
            self.cfg.port,
            self.cfg.database,
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def upsert_experiments(self, rows: list[ExperimentRow]) -> WriteResult:
        if not rows:
            return WriteResult(0, 0)
        from psycopg2.extras import execute_values

        sql = """
            INSERT INTO experiment
                (systemid, run, description, origin, src_host)
            VALUES %s
            ON CONFLICT (systemid, run) DO UPDATE SET
                description = EXCLUDED.description,
                origin      = EXCLUDED.origin,
                src_host    = EXCLUDED.src_host
        """
        values = [
            (r.systemid, r.run, r.description, self.origin, self.src_host)
            for r in rows
        ]
        with self._conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=500)
            applied = cur.rowcount
        self._conn.commit()
        return WriteResult(len(values), applied)

    def upsert_experimental(
        self, rows: list[ExperimentalRow], xfer: datetime
    ) -> WriteResult:
        if not rows:
            return WriteResult(0, 0)
        from psycopg2.extras import execute_values

        sql = """
            INSERT INTO experimental_data
                (systemid, run, row_no, ts, source, data, xfer, origin, src_host)
            VALUES %s
            ON CONFLICT (systemid, run, row_no) DO UPDATE SET
                ts       = EXCLUDED.ts,
                source   = EXCLUDED.source,
                data     = EXCLUDED.data,
                xfer     = EXCLUDED.xfer,
                origin   = EXCLUDED.origin,
                src_host = EXCLUDED.src_host
        """
        values = [
            (
                r.systemid,
                r.run,
                r.row_no,
                _ts(r.ts),
                r.source,
                r.data,
                xfer,
                self.origin,
                self.src_host,
            )
            for r in rows
        ]
        with self._conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=500)
            applied = cur.rowcount
        self._conn.commit()
        return WriteResult(len(values), applied)

    def upsert_environment(
        self, rows: list[EnvironmentRow], xfer: datetime
    ) -> WriteResult:
        if not rows:
            return WriteResult(0, 0)
        from psycopg2.extras import execute_values

        sql = """
            INSERT INTO environment
                (digest, systemid, ts, data, xfer, origin, src_host)
            VALUES %s
            ON CONFLICT (digest) DO UPDATE SET
                xfer     = EXCLUDED.xfer,
                origin   = EXCLUDED.origin,
                src_host = EXCLUDED.src_host
        """
        # De-duplicate within the batch: ON CONFLICT cannot update the same
        # row twice in one statement, and identical environment samples do
        # occur when a sensor is quiet.
        seen: dict[str, tuple] = {}
        for r in rows:
            seen[r.digest] = (
                r.digest,
                r.systemid,
                _ts(r.ts),
                r.data,
                xfer,
                self.origin,
                self.src_host,
            )
        values = list(seen.values())
        with self._conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=500)
            applied = cur.rowcount
        self._conn.commit()
        return WriteResult(len(rows), applied)

    def log_transfer(
        self,
        started_at: datetime,
        table_name: str,
        rows_sent: int,
        rows_applied: int,
        trigger: str,
        ok: bool,
        detail: str | None = None,
    ) -> None:
        sql = """
            INSERT INTO transfer_log
                (started_at, src_host, table_name, rows_sent, rows_applied,
                 trigger, ok, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        with self._conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    started_at,
                    self.src_host,
                    table_name,
                    rows_sent,
                    rows_applied,
                    trigger,
                    ok,
                    detail,
                ),
            )
        self._conn.commit()

    def counts(self) -> dict[str, int]:
        out = {}
        with self._conn.cursor() as cur:
            for table in ("experiment", "experimental_data", "environment"):
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                out[table] = cur.fetchone()[0]
        return out


# ---------------------------------------------------------------------------
# SQLite stand-in for the remote server
# ---------------------------------------------------------------------------

MOCK_REMOTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment (
    systemid    TEXT NOT NULL,
    run         INTEGER NOT NULL,
    description TEXT,
    origin      TEXT NOT NULL DEFAULT 'real',
    src_host    TEXT,
    ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
    PRIMARY KEY (systemid, run)
);
CREATE TABLE IF NOT EXISTS experimental_data (
    systemid    TEXT NOT NULL,
    run         INTEGER NOT NULL,
    row_no      INTEGER NOT NULL,
    ts          TEXT,
    source      TEXT,
    data        TEXT,
    xfer        TEXT,
    origin      TEXT NOT NULL DEFAULT 'real',
    src_host    TEXT,
    ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
    PRIMARY KEY (systemid, run, row_no),
    FOREIGN KEY (systemid, run) REFERENCES experiment (systemid, run)
);
CREATE TABLE IF NOT EXISTS environment (
    digest      TEXT PRIMARY KEY,
    systemid    TEXT NOT NULL,
    ts          TEXT,
    data        TEXT,
    xfer        TEXT,
    origin      TEXT NOT NULL DEFAULT 'real',
    src_host    TEXT,
    ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now'))
);
CREATE TABLE IF NOT EXISTS transfer_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
    src_host     TEXT,
    table_name   TEXT NOT NULL,
    rows_sent    INTEGER NOT NULL,
    rows_applied INTEGER NOT NULL,
    trigger      TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    detail       TEXT
);
"""


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class SqliteMockSink(RemoteSink):
    """A file on disk that behaves like the remote archive.

    Exists so the team is not blocked on VPN credentials. Swap `kind` in the
    config from "sqlite" to "postgres" and nothing else changes.
    """

    def __init__(self, path: str | Path, origin: str, src_host: str) -> None:
        super().__init__(origin, src_host)
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=10.0)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(MOCK_REMOTE_SCHEMA)
        self._conn.commit()
        log.info("connected to mock remote %s", self.path)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def upsert_experiments(self, rows: list[ExperimentRow]) -> WriteResult:
        if not rows:
            return WriteResult(0, 0)
        sql = """
            INSERT INTO experiment (systemid, run, description, origin, src_host)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (systemid, run) DO UPDATE SET
                description = excluded.description,
                origin      = excluded.origin,
                src_host    = excluded.src_host
        """
        values = [
            (r.systemid, r.run, r.description, self.origin, self.src_host)
            for r in rows
        ]
        cur = self._conn.executemany(sql, values)
        self._conn.commit()
        return WriteResult(len(values), cur.rowcount)

    def upsert_experimental(
        self, rows: list[ExperimentalRow], xfer: datetime
    ) -> WriteResult:
        if not rows:
            return WriteResult(0, 0)
        sql = """
            INSERT INTO experimental_data
                (systemid, run, row_no, ts, source, data, xfer, origin, src_host)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (systemid, run, row_no) DO UPDATE SET
                ts       = excluded.ts,
                source   = excluded.source,
                data     = excluded.data,
                xfer     = excluded.xfer,
                origin   = excluded.origin,
                src_host = excluded.src_host
        """
        stamp = _iso(xfer)
        values = [
            (
                r.systemid,
                r.run,
                r.row_no,
                r.ts,
                r.source,
                r.data,
                stamp,
                self.origin,
                self.src_host,
            )
            for r in rows
        ]
        cur = self._conn.executemany(sql, values)
        self._conn.commit()
        return WriteResult(len(values), cur.rowcount)

    def upsert_environment(
        self, rows: list[EnvironmentRow], xfer: datetime
    ) -> WriteResult:
        if not rows:
            return WriteResult(0, 0)
        sql = """
            INSERT INTO environment
                (digest, systemid, ts, data, xfer, origin, src_host)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (digest) DO UPDATE SET
                xfer     = excluded.xfer,
                origin   = excluded.origin,
                src_host = excluded.src_host
        """
        stamp = _iso(xfer)
        seen: dict[str, tuple] = {}
        for r in rows:
            seen[r.digest] = (
                r.digest,
                r.systemid,
                r.ts,
                r.data,
                stamp,
                self.origin,
                self.src_host,
            )
        cur = self._conn.executemany(sql, list(seen.values()))
        self._conn.commit()
        return WriteResult(len(rows), cur.rowcount)

    def log_transfer(
        self,
        started_at: datetime,
        table_name: str,
        rows_sent: int,
        rows_applied: int,
        trigger: str,
        ok: bool,
        detail: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO transfer_log
                (started_at, src_host, table_name, rows_sent, rows_applied,
                 trigger, ok, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _iso(started_at),
                self.src_host,
                table_name,
                rows_sent,
                rows_applied,
                trigger,
                1 if ok else 0,
                detail,
            ),
        )
        self._conn.commit()

    def counts(self) -> dict[str, int]:
        out = {}
        for table in ("experiment", "experimental_data", "environment"):
            out[table] = self._conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        return out


def build_sink(cfg: RemoteConfig, origin: str, src_host: str) -> RemoteSink:
    if cfg.kind == "postgres":
        return PostgresSink(cfg, origin, src_host)
    if cfg.kind == "sqlite":
        if cfg.path is None:
            raise ValueError("remote.kind = 'sqlite' needs remote.path")
        return SqliteMockSink(cfg.path, origin, src_host)
    raise ValueError(f"unknown remote.kind: {cfg.kind!r}")
