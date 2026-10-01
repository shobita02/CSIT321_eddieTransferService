"""Shared fixtures.

Every test builds its own KAT database and its own mock remote in a tmp
directory, so tests never touch data/ and can run in any order.
"""

from __future__ import annotations

import sqlite3
import sys
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
from katxfer.fakedata import fmt_ts  # noqa: E402

KAT_SCHEMA = (Path(__file__).resolve().parent.parent / "schema" / "kat_sqlite.sql").read_text()


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
                    f"{run}.{row},1.0,2.0",
                ),
            )
    for i in range(env):
        conn.execute(
            "INSERT INTO Environment (systemid, timestamp, data, xfer) "
            "VALUES (?,?,?,NULL)",
            ("K4-RIG-01", fmt_ts(t0 + timedelta(minutes=i)), f"ambient_c={20 + i}"),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def kat_db(tmp_path: Path) -> Path:
    db = tmp_path / "kat.sqlite"
    seed(db)
    return db


@pytest.fixture()
def cfg(tmp_path: Path, kat_db: Path) -> ServiceConfig:
    return ServiceConfig(
        local=LocalConfig(path=kat_db, batch_size=7, busy_timeout_s=5.0),
        remote=RemoteConfig(kind="sqlite", path=tmp_path / "remote.sqlite"),
        zmq=ZmqConfig(enabled=False),
        sweep_interval_s=1.0,
        notify_debounce_s=0.0,
        origin="fake",
        src_host="testhost",
    )


def remote_count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


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
