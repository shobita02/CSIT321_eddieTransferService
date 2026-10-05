#!/usr/bin/env python3
"""Print every row on both sides of the transfer, for demos and evidence.

Shows the local KAT database with its `xfer` column (NULL = not yet sent),
then the remote archive with a duplicate check on each table, then the
transfer log. Run it before and after `python -m katxfer.service --once`.

    python tools/show_state.py
    python tools/show_state.py --limit 50
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from katxfer.config import load as load_config  # noqa: E402


def table(conn, title: str, sql: str, limit: int) -> None:
    # Works for both sqlite3 and psycopg2 connections via a plain cursor.
    cur = conn.cursor()
    cur.execute(f"{sql} LIMIT {limit}")
    cols = [d[0] for d in cur.description]
    rows = [["NULL" if v is None else str(v) for v in r] for r in cur.fetchall()]
    cur.close()
    widths = [
        min(45, max([len(c)] + [len(r[i]) for r in rows])) for i, c in enumerate(cols)
    ]
    print(f"\n{title}")
    print("  " + "  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(v[:w].ljust(w) for v, w in zip(r, widths)))
    if not rows:
        print("  (no rows)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=20, help="rows shown per table")
    args = ap.parse_args()
    cfg = load_config(args.config)

    print(f"=== LOCAL  (KAT)    {cfg.local.path}")
    local = sqlite3.connect(f"file:{cfg.local.path.as_posix()}?mode=ro", uri=True)
    table(local, "Experiment", "SELECT systemid, run, description FROM Experiment", args.limit)
    table(
        local,
        "ExperimentalData   (xfer NULL = not transferred yet)",
        'SELECT systemid, run, "row", timestamp, data, xfer FROM ExperimentalData '
        'ORDER BY systemid, run, "row"',
        args.limit,
    )
    table(
        local,
        "Environment        (xfer NULL = not transferred yet)",
        "SELECT rowid, systemid, timestamp, data, xfer FROM Environment ORDER BY rowid",
        args.limit,
    )
    local.close()

    if cfg.remote.kind == "postgres":
        import psycopg2

        target = f"postgres {cfg.remote.host}:{cfg.remote.port}/{cfg.remote.database}"
        try:
            remote = psycopg2.connect(
                host=cfg.remote.host,
                port=cfg.remote.port,
                dbname=cfg.remote.database,
                user=cfg.remote.user,
                password=cfg.remote.password,
                sslmode=cfg.remote.sslmode,
                connect_timeout=cfg.remote.connect_timeout_s,
            )
        except psycopg2.OperationalError as exc:
            print(f"\n=== REMOTE {target}\n  unreachable: {exc}")
            print("  Is the container running?  docker compose up -d")
            return 1
    else:
        target = str(cfg.remote.path)
        if not cfg.remote.path.exists():
            print(f"\n=== REMOTE {target}\n  (does not exist yet - nothing transferred)")
            return 0
        remote = sqlite3.connect(cfg.remote.path)

    print(f"\n=== REMOTE (archive) {target}")
    table(remote, "experiment", "SELECT systemid, run, description, origin FROM experiment", args.limit)
    table(
        remote,
        "experimental_data",
        "SELECT systemid, run, row_no, ts, data, xfer FROM experimental_data "
        "ORDER BY systemid, run, row_no",
        args.limit,
    )
    table(
        remote,
        "environment",
        "SELECT substr(digest, 1, 12) AS digest, systemid, ts, data, xfer FROM environment",
        args.limit,
    )
    table(
        remote,
        "Duplicate check (total rows must equal distinct keys)",
        "SELECT 'experimental_data' AS tbl, COUNT(*) AS total_rows, "
        "(SELECT COUNT(*) FROM (SELECT DISTINCT systemid, run, row_no FROM experimental_data) AS k) AS distinct_keys "
        "FROM experimental_data "
        "UNION ALL SELECT 'environment', COUNT(*), COUNT(DISTINCT digest) FROM environment",
        args.limit,
    )
    table(
        remote,
        "transfer_log (one line per batch the service committed)",
        "SELECT id, started_at, table_name, rows_sent, trigger FROM transfer_log ORDER BY id",
        args.limit,
    )
    remote.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
