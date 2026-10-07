"""Reading from the local KAT SQLite database.

Two rules govern everything in this module:

1. We never modify KAT's data except to stamp `xfer`. That column exists
   precisely so an external process can mark what it has taken, and it is the
   only thing we are entitled to write.

2. We stamp `xfer` only after the remote has committed. The window between
   "remote committed" and "local stamped" can produce a duplicate send after a
   crash, which is why every remote write is an idempotent upsert. The
   opposite ordering would lose data instead, which is not recoverable.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .fakedata import fmt_ts


@dataclass(frozen=True)
class ExperimentalRow:
    systemid: str
    run: int
    row_no: int
    ts: str | int | None  # KAT writes epoch ms; see fakedata.parse_ts
    source: str | None
    data: str | None

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.systemid, self.run, self.row_no)


@dataclass(frozen=True)
class EnvironmentRow:
    systemid: str
    ts: str | int | None  # KAT writes epoch ms; see fakedata.parse_ts
    data: str | None
    rowid: int

    @property
    def key(self) -> tuple[str, str | int | None]:
        """Remote identity: the timestamp, plus systemid because the archive
        collects from several rigs. Never the data, never our rowid."""
        return (self.systemid, self.ts)


@dataclass(frozen=True)
class ExperimentRow:
    systemid: str
    run: int
    description: str | None


class LocalKatDb:
    """Thin, deliberately boring wrapper over the KAT SQLite file."""

    def __init__(self, path: str | Path, busy_timeout_s: float = 10.0) -> None:
        self.path = Path(path)
        self.busy_timeout_s = busy_timeout_s

    @contextmanager
    def connect(self, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        if readonly:
            uri = f"file:{self.path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=self.busy_timeout_s)
        else:
            conn = sqlite3.connect(self.path, timeout=self.busy_timeout_s)
        conn.row_factory = sqlite3.Row
        # KAT may be mid-insert and holding the write lock; wait rather than failing the whole cycle.
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_s * 1000)}")
        try:
            yield conn
        finally:
            conn.close()

    # -- reads ---------------------------------------------------------------

    def pending_experimental(self, limit: int) -> list[ExperimentalRow]:
        sql = """
            SELECT systemid, run, "row" AS row_no, timestamp, source, data
            FROM ExperimentalData
            WHERE xfer IS NULL
            ORDER BY systemid, run, "row"
            LIMIT ?
        """
        with self.connect(readonly=True) as conn:
            return [
                ExperimentalRow(
                    systemid=r["systemid"],
                    run=r["run"],
                    row_no=r["row_no"],
                    ts=r["timestamp"],
                    source=r["source"],
                    data=r["data"],
                )
                for r in conn.execute(sql, (limit,))
            ]

    def pending_environment(self, limit: int) -> list[EnvironmentRow]:
        # We carry SQLite's implicit rowid through to the stamping step as a
        # cheap handle on the row. It never leaves this machine.
        sql = """
            SELECT rowid AS rid, systemid, timestamp, data
            FROM Environment
            WHERE xfer IS NULL
            ORDER BY rowid
            LIMIT ?
        """
        with self.connect(readonly=True) as conn:
            return [
                EnvironmentRow(
                    systemid=r["systemid"],
                    ts=r["timestamp"],
                    data=r["data"],
                    rowid=r["rid"],
                )
                for r in conn.execute(sql, (limit,))
            ]

    def experiments_for(
        self, keys: set[tuple[str, int]]
    ) -> list[ExperimentRow]:
        """Parent rows for the runs in a batch.

        Experiment has no `xfer` column, so there is no watermark to follow.
        Instead we fetch the parents of whatever we are about to ship and
        upsert them first, which satisfies the remote foreign key and costs
        one small query per batch.
        """
        if not keys:
            return []
        clause = " OR ".join(["(systemid = ? AND run = ?)"] * len(keys))
        params: list[object] = []
        for sid, run in sorted(keys):
            params.extend([sid, run])
        sql = f"SELECT systemid, run, description FROM Experiment WHERE {clause}"
        with self.connect(readonly=True) as conn:
            return [
                ExperimentRow(r["systemid"], r["run"], r["description"])
                for r in conn.execute(sql, params)
            ]

    def pending_counts(self) -> dict[str, int]:
        with self.connect(readonly=True) as conn:
            return {
                "ExperimentalData": conn.execute(
                    "SELECT COUNT(*) FROM ExperimentalData WHERE xfer IS NULL"
                ).fetchone()[0],
                "Environment": conn.execute(
                    "SELECT COUNT(*) FROM Environment WHERE xfer IS NULL"
                ).fetchone()[0],
            }

    # -- the one write we are allowed to make --------------------------------

    def mark_experimental_transferred(
        self, rows: list[ExperimentalRow], when: datetime | None = None
    ) -> int:
        if not rows:
            return 0
        stamp = fmt_ts(when or datetime.now(timezone.utc))
        sql = """
            UPDATE ExperimentalData SET xfer = ?
            WHERE systemid = ? AND run = ? AND "row" = ? AND xfer IS NULL
        """
        params = [(stamp, r.systemid, r.run, r.row_no) for r in rows]
        with self.connect() as conn:
            cur = conn.executemany(sql, params)
            conn.commit()
            return cur.rowcount

    def mark_environment_transferred(
        self, rows: list[EnvironmentRow], when: datetime | None = None
    ) -> int:
        if not rows:
            return 0
        stamp = fmt_ts(when or datetime.now(timezone.utc))
        sql = "UPDATE Environment SET xfer = ? WHERE rowid = ? AND xfer IS NULL"
        params = [(stamp, r.rowid) for r in rows]
        with self.connect() as conn:
            cur = conn.executemany(sql, params)
            conn.commit()
            return cur.rowcount
