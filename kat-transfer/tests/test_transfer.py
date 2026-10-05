"""Tests for the transfer service.

These encode the four properties the service has to have, which are the ones
worth defending in the report:

  1. Everything untransferred gets transferred (completeness).
  2. Transferring twice does not duplicate (idempotence).
  3. Parents arrive before children (referential integrity).
  4. A row is stamped locally only after the remote has committed (ordering).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

from conftest import local_pending, remote_count

from katxfer.localdb import LocalKatDb
from katxfer.service import TransferService


# -- 1. completeness ---------------------------------------------------------


def test_transfers_everything_pending(cfg):
    assert local_pending(cfg.local.path) == (20, 5)

    moved = TransferService(cfg).run_once()

    assert moved == 25
    assert local_pending(cfg.local.path) == (0, 0)
    assert remote_count(cfg.remote.path, "experimental_data") == 20
    assert remote_count(cfg.remote.path, "environment") == 5


def test_batching_does_not_lose_rows(cfg):
    # batch_size is 7 against 20+5 rows, so the drain loop must iterate.
    cfg.local.batch_size = 7
    TransferService(cfg).run_once()
    assert local_pending(cfg.local.path) == (0, 0)
    assert remote_count(cfg.remote.path, "experimental_data") == 20


def test_second_run_transfers_nothing(cfg):
    TransferService(cfg).run_once()
    assert TransferService(cfg).run_once() == 0


def test_only_new_rows_move(cfg):
    TransferService(cfg).run_once()
    conn = sqlite3.connect(cfg.local.path)
    conn.execute(
        'INSERT INTO ExperimentalData (systemid, run, "row", timestamp, source, '
        'data, xfer) VALUES (?,?,?,?,?,?,NULL)',
        ("K4-RIG-01", 1, 999, "2026-10-01 10:00:00.000", "ADC-CH0", "new"),
    )
    conn.commit()
    conn.close()

    assert TransferService(cfg).run_once() == 1
    assert remote_count(cfg.remote.path, "experimental_data") == 21


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
    assert remote_count(cfg.remote.path, "experimental_data") == 20  # no dupes
    assert remote_count(cfg.remote.path, "environment") == 5


def test_identical_environment_samples_collapse(cfg):
    """Environment has no unique key, so identity is a digest of the content.

    A quiet sensor emitting the same reading twice at the same instant is
    indistinguishable from a duplicated row, and collapses. This test exists to
    make that behaviour explicit rather than accidental -- see note 3 in
    schema/remote_postgres.sql.
    """
    conn = sqlite3.connect(cfg.local.path)
    conn.execute(
        "INSERT INTO Environment (systemid, timestamp, data, xfer) "
        "SELECT systemid, timestamp, data, NULL FROM Environment LIMIT 1"
    )
    conn.commit()
    conn.close()

    TransferService(cfg).run_once()

    assert local_pending(cfg.local.path) == (0, 0)  # both stamped locally
    assert remote_count(cfg.remote.path, "environment") == 5  # collapsed to one


# -- 3. referential integrity ------------------------------------------------


def test_experiment_parents_arrive_first(cfg):
    """The remote has a FK from experimental_data to experiment.

    Experiment has no xfer column, so there is no watermark to follow; the
    service has to fetch the parents of each batch itself. With foreign keys
    enforced on the mock remote, getting this wrong raises instead of passing
    quietly.
    """
    TransferService(cfg).run_once()
    assert remote_count(cfg.remote.path, "experiment") == 2

    conn = sqlite3.connect(cfg.remote.path)
    orphans = conn.execute(
        "SELECT COUNT(*) FROM experimental_data d "
        "LEFT JOIN experiment e ON e.systemid = d.systemid AND e.run = d.run "
        "WHERE e.systemid IS NULL"
    ).fetchone()[0]
    conn.close()
    assert orphans == 0


# -- 4. ordering and provenance ----------------------------------------------


def test_nothing_is_stamped_when_the_remote_fails(cfg, monkeypatch):
    """If the remote write raises, local rows must stay NULL and be retried."""
    from katxfer import sinks

    def boom(self, rows, xfer):
        raise RuntimeError("remote is down")

    monkeypatch.setattr(sinks.SqliteMockSink, "upsert_experimental", boom)

    svc = TransferService(cfg)
    try:
        svc.run_once()
    except RuntimeError:
        pass

    pending_exp, _ = local_pending(cfg.local.path)
    assert pending_exp == 20  # nothing stamped, nothing lost


def test_rows_are_tagged_with_origin_and_host(cfg):
    TransferService(cfg).run_once()
    conn = sqlite3.connect(cfg.remote.path)
    origin, host = conn.execute(
        "SELECT origin, src_host FROM experimental_data LIMIT 1"
    ).fetchone()
    conn.close()
    assert origin == "fake"  # so the archive can separate test data from real
    assert host == "testhost"


def test_transfer_log_records_each_batch(cfg):
    TransferService(cfg).run_once()
    conn = sqlite3.connect(cfg.remote.path)
    rows = conn.execute(
        "SELECT table_name, rows_sent, trigger, ok FROM transfer_log"
    ).fetchall()
    conn.close()
    assert rows, "every committed batch should be auditable"
    assert all(ok == 1 for *_, ok in rows)
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


def test_environment_digest_is_content_addressed(kat_db):
    """Digest must depend on KAT's content only, never on our local rowid."""
    db = LocalKatDb(kat_db)
    rows = db.pending_environment(limit=100)
    first = rows[0]

    from katxfer.localdb import EnvironmentRow

    same_content_other_rowid = EnvironmentRow(
        systemid=first.systemid, ts=first.ts, data=first.data, rowid=first.rowid + 1000
    )
    assert same_content_other_rowid.digest == first.digest
    assert len({r.digest for r in rows}) == len(rows)


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
    conn = sqlite3.connect(cfg.remote.path)
    ts = conn.execute(
        "SELECT ts FROM experimental_data WHERE systemid = 'CSIT321FAKE'"
    ).fetchone()[0]
    conn.close()
    assert ts == "2026-10-01 23:44:06.165"  # normalised, as Postgres would store it
