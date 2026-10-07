"""Remote sinks.

`PostgresSink` writes to the PostgreSQL archive: the local Docker container
for development and tests, or the Bored Owl dev server for real. Both are
built from schema/remote_postgres.sql, so they behave identically.

Every write is an upsert on a key the client can compute, which is what makes
the "stamp xfer only after remote commit" ordering safe: a replayed batch
updates rows that are already there instead of duplicating them.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

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


def _ts(value: str | int | None) -> datetime | None:
    # KAT stores epoch milliseconds; generated data stores ISO text.
    return parse_ts(value) if value is not None and value != "" else None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class PostgresSink(RemoteSink):
    def __init__(self, cfg: RemoteConfig, origin: str, src_host: str) -> None:
        super().__init__(origin, src_host)
        self.cfg = cfg
        self._conn = None

    def connect(self) -> None:
        import psycopg2  # imported late so --doctor works without the driver

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
                (systemid, ts, data, xfer, origin, src_host)
            VALUES %s
            ON CONFLICT (systemid, ts) DO UPDATE SET
                data     = EXCLUDED.data,
                xfer     = EXCLUDED.xfer,
                origin   = EXCLUDED.origin,
                src_host = EXCLUDED.src_host
        """
        # KAT's Environment has no unique constraint, so a batch can repeat a
        # (systemid, ts) key. ON CONFLICT cannot update the same row twice in
        # one statement, so keep the last one -- the only one a keyed remote
        # table can hold.
        seen: dict[tuple, tuple] = {}
        for r in rows:
            ts = _ts(r.ts)
            seen[(r.systemid, ts)] = (
                r.systemid, ts, r.data, xfer, self.origin, self.src_host
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


def build_sink(cfg: RemoteConfig, origin: str, src_host: str) -> RemoteSink:
    return PostgresSink(cfg, origin, src_host)
