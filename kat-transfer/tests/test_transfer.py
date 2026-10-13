"""Tests for the transfer service.

These encode the four properties the service has to have, which are the ones
worth defending in the report:

  1. Everything untransferred gets transferred (completeness).
  2. Transferring twice does not duplicate (idempotence).
  3. Parents arrive before children (referential integrity).
  4. A row is stamped locally only after the remote has committed (ordering).
"""

from __future__ import annotations

import dataclasses
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import KAT_SCHEMA, local_pending, remote_count, remote_query

from katxfer.localdb import LocalKatDb
from katxfer.service import TransferService


# -- 1. completeness ---------------------------------------------------------


def test_transfers_everything_pending(cfg):
    assert local_pending(cfg.local.path) == (20, 5)

    moved = TransferService(cfg).run_once()

    assert moved == 25
    assert local_pending(cfg.local.path) == (0, 0)
    assert remote_count(cfg.remote, "experimental_data") == 20
    assert remote_count(cfg.remote, "environment") == 5


def test_batching_does_not_lose_rows(cfg):
    # batch_size is 7 against 20+5 rows, so the drain loop must iterate.
    cfg.local.batch_size = 7
    TransferService(cfg).run_once()
    assert local_pending(cfg.local.path) == (0, 0)
    assert remote_count(cfg.remote, "experimental_data") == 20


def test_second_run_transfers_nothing(cfg):
    TransferService(cfg).run_once()
    assert TransferService(cfg).run_once() == 0


def test_only_new_rows_move(cfg):
    TransferService(cfg).run_once()
    conn = sqlite3.connect(cfg.local.path)
    conn.execute(
        'INSERT INTO ExperimentalData (systemid, run, "row", timestamp, source, '
        'data, xfer) VALUES (?,?,?,?,?,?,NULL)',
        ("K4-RIG-01", 1, 999, "2026-10-01 10:00:00.000", "ADC-CH0", '{"new": true}'),
    )
    conn.commit()
    conn.close()

    assert TransferService(cfg).run_once() == 1
    assert remote_count(cfg.remote, "experimental_data") == 21


# -- 2. idempotence ----------------------------------------------------------


def test_resend_after_crash_does_not_duplicate(cfg):
    """The dangerous window: remote committed, then we died before stamping."""
    TransferService(cfg).run_once()
    conn = sqlite3.connect(cfg.local.path)
    conn.execute("UPDATE ExperimentalData SET xfer = NULL")
    conn.execute("UPDATE Environment SET xfer = NULL")
    conn.commit()
    conn.close()

    moved = TransferService(cfg).run_once()

    assert moved == 25  # it really did re-send
    assert remote_count(cfg.remote, "experimental_data") == 20  # no dupes
    assert remote_count(cfg.remote, "environment") == 5


def test_identical_environment_samples_do_not_collapse(cfg):
    """Identity is (systemid, timestamp), never the content.

    A quiet sensor reporting the same reading at two instants is two samples,
    and both must reach the archive -- see note 3 in schema/remote_postgres.sql.
    """
    conn = sqlite3.connect(cfg.local.path)
    conn.execute(
        "INSERT INTO Environment (systemid, timestamp, data, xfer) "
        "SELECT systemid, '2026-10-01 12:00:00.000', data, NULL "
        "FROM Environment LIMIT 1"
    )
    conn.commit()
    conn.close()

    TransferService(cfg).run_once()

    assert local_pending(cfg.local.path) == (0, 0)
    assert remote_count(cfg.remote, "environment") == 6  # both kept


def test_repeated_environment_key_in_kat_does_not_wedge(cfg):
    """KAT's Environment has no unique constraint, so it can hold two rows for
    the same system and instant. The remote key holds one; the later wins,
    both are stamped, and the service does not get stuck retrying the batch."""
    conn = sqlite3.connect(cfg.local.path)
    conn.execute(
        "INSERT INTO Environment (systemid, timestamp, data, xfer) "
        "SELECT systemid, timestamp, '{\"later\": true}', NULL "
        "FROM Environment ORDER BY rowid LIMIT 1"
    )
    conn.commit()
    conn.close()

    TransferService(cfg).run_once()

    assert local_pending(cfg.local.path) == (0, 0)
    assert remote_count(cfg.remote, "environment") == 5
    data = remote_query(
        cfg.remote, "SELECT data::text FROM environment ORDER BY ts LIMIT 1"
    )[0][0]
    assert data == '{"later": true}'


def test_two_rigs_same_instant_both_kept(cfg, tmp_path):
    """Two laptops can sample at the same instant. The archive key includes
    systemid, so neither overwrites the other."""
    TransferService(cfg).run_once()
    first_ts = sqlite3.connect(cfg.local.path).execute(
        "SELECT timestamp FROM Environment ORDER BY rowid LIMIT 1"
    ).fetchone()[0]

    other = tmp_path / "other_kat.sqlite"
    conn = sqlite3.connect(other)
    conn.executescript(KAT_SCHEMA)
    conn.execute(
        "INSERT INTO Environment (systemid, timestamp, data, xfer) "
        "VALUES (?, ?, ?, NULL)",
        ("K4-RIG-02", first_ts, '{"ambient_c": 99}'),
    )
    conn.commit()
    conn.close()

    other_cfg = dataclasses.replace(
        cfg, local=dataclasses.replace(cfg.local, path=other)
    )
    assert TransferService(other_cfg).run_once() == 1
    assert remote_count(cfg.remote, "environment") == 6


def test_invalid_json_rejected_and_left_unstamped(cfg):
    """The remote `data` column is json. A payload that is not JSON must fail
    the write, and a failed write must never be stamped locally."""
    TransferService(cfg).run_once()
    conn = sqlite3.connect(cfg.local.path)
    conn.execute(
        "INSERT INTO Environment (systemid, timestamp, data, xfer) "
        "VALUES ('K4-RIG-01', '2026-10-01 12:00:00.000', 'ambient_c=20', NULL)"
    )
    conn.commit()
    conn.close()

    import psycopg2

    with pytest.raises(psycopg2.DataError):  # invalid input syntax for type json
        TransferService(cfg).run_once()

    assert local_pending(cfg.local.path) == (0, 1)  # still waiting
    assert remote_count(cfg.remote, "environment") == 5


# -- 3. referential integrity ------------------------------------------------


def test_experiment_parents_arrive_first(cfg):
    """The remote has a FK from experimental_data to experiment.

    Experiment has no xfer column, so there is no watermark to follow; the
    service has to fetch the parents of each batch itself. The remote enforces
    the foreign key, so getting this wrong raises instead of passing quietly.
    """
    TransferService(cfg).run_once()
    assert remote_count(cfg.remote, "experiment") == 2

    orphans = remote_query(
        cfg.remote,
        "SELECT COUNT(*) FROM experimental_data d "
        "LEFT JOIN experiment e ON e.systemid = d.systemid AND e.run = d.run "
        "WHERE e.systemid IS NULL",
    )[0][0]
    assert orphans == 0


# -- 4. ordering and provenance ----------------------------------------------


def test_nothing_is_stamped_when_the_remote_fails(cfg, monkeypatch):
    """If the remote write raises, local rows must stay NULL and be retried."""
    from katxfer import sinks

    def boom(self, rows, xfer):
        raise RuntimeError("remote is down")

    monkeypatch.setattr(sinks.PostgresSink, "upsert_experimental", boom)

    svc = TransferService(cfg)
    try:
        svc.run_once()
    except RuntimeError:
        pass

    pending_exp, _ = local_pending(cfg.local.path)
    assert pending_exp == 20  # nothing stamped, nothing lost


def test_rows_are_tagged_with_origin_and_host(cfg):
    TransferService(cfg).run_once()
    origin, host = remote_query(
        cfg.remote, "SELECT origin, src_host FROM experimental_data LIMIT 1"
    )[0]
    assert origin == "fake"  # so the archive can separate test data from real
    assert host == "testhost"


def test_transfer_log_records_each_batch(cfg):
    TransferService(cfg).run_once()
    rows = remote_query(
        cfg.remote, "SELECT table_name, rows_sent, trigger, ok FROM transfer_log"
    )
    assert rows, "every committed batch should be auditable"
    assert all(ok is True for *_, ok in rows)
    assert all(trigger == "manual" for *_, trigger, _ in rows)
    assert sum(sent for _, sent, _, _ in rows) == 25


# -- optional dependencies ---------------------------------------------------


def test_service_starts_without_pyzmq():
    """pyzmq must stay optional, as the README and requirements.txt promise.

    Regression test: `service.py` used to import `zmqbus` at module scope,
    which imports pyzmq, so the service could not start at all without it --
    not even `--status`, and not even with 0MQ disabled in the config. Run in
    a subprocess because other tests in this suite import zmqbus directly.
    """
    probe = (
        "import katxfer.service, sys;"
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] == 'zmq');"
        "sys.exit('pyzmq imported at module scope: %s' % leaked if leaked else 0)"
    )
    subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parent.parent,
        check=True,
    )


def test_doctor_reports_interpreter_and_dependencies(capsys, cfg, tmp_path):
    """`--doctor` has to work even when things are broken, so it must not
    depend on the config loading successfully."""
    from katxfer.service import main

    assert main(["--doctor", "-c", str(tmp_path / "missing.toml")]) == 0
    out = capsys.readouterr().out

    assert sys.executable in out  # the whole point: which Python is this?
    assert "pyzmq" in out
    assert "psycopg2" in out
    assert "FAILED" in out  # it reported the bad config instead of crashing


# -- local database access ---------------------------------------------------


def test_pending_query_ignores_transferred_rows(kat_db):
    db = LocalKatDb(kat_db)
    rows = db.pending_experimental(limit=100)
    assert len(rows) == 20

    db.mark_experimental_transferred(rows[:5])
    assert len(db.pending_experimental(limit=100)) == 15
    assert db.pending_counts()["ExperimentalData"] == 15


def test_environment_key_is_system_and_timestamp(kat_db):
    """Identity comes from KAT's natural key, never our rowid or the data."""
    db = LocalKatDb(kat_db)
    rows = db.pending_environment(limit=100)
    first = rows[0]

    from katxfer.localdb import EnvironmentRow

    other_rowid_and_data = EnvironmentRow(
        systemid=first.systemid, ts=first.ts, data="{}", rowid=first.rowid + 1000
    )
    assert other_rowid_and_data.key == first.key == (first.systemid, first.ts)
    assert len({r.key for r in rows}) == len(rows)


def test_kat_epoch_millisecond_timestamps(cfg):
    """KAT 1.0.3 stores TIMESTAMP columns as epoch milliseconds, not text.

    Taken from a real katdata.db produced by `ed-insert` / `env-record`.
    """
    from katxfer.fakedata import parse_ts

    assert parse_ts(1790898246165).isoformat() == "2026-10-01T23:44:06.165000+00:00"
    assert parse_ts("1790898246165") == parse_ts(1790898246165)

    conn = sqlite3.connect(cfg.local.path)
    conn.execute(
        "INSERT INTO Experiment (systemid, run, description) VALUES (?,?,?)",
        ("CSIT321FAKE", 9001, "Fake test experiment generated in KAT"),
    )
    conn.execute(
        'INSERT INTO ExperimentalData (systemid, run, "row", timestamp, source, '
        "data, xfer) VALUES (?,?,?,?,?,?,NULL)",
        ("CSIT321FAKE", 9001, 0, 1790898246165, "simulated",
         '{"temperature":22.5,"pressure":101.3}'),
    )
    conn.execute(
        "INSERT INTO Environment (systemid, timestamp, data, xfer) VALUES (?,?,?,NULL)",
        ("CSIT321FAKE", 1790898290276, '{"room_temperature":21.8,"humidity":45.0}'),
    )
    conn.commit()
    conn.close()

    assert TransferService(cfg).run_once() == 27
    ts = remote_query(
        cfg.remote, "SELECT ts FROM experimental_data WHERE systemid = 'CSIT321FAKE'"
    )[0][0]
    assert ts == parse_ts(1790898246165)  # stored as a real timestamptz
