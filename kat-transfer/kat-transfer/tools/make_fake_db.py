#!/usr/bin/env python3
"""Create a fake KAT database on this machine.

Stands in for "install KAT, run the data generator, point it at a local
SQLite file" until that path is working. The schema is KAT's, verbatim.

    python3 tools/make_fake_db.py --reset --runs 3 --rows 2000

Produces data/kat.sqlite with every row's `xfer` NULL, i.e. the whole
database looks untransferred, which is exactly what the transfer service
should find on first run.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from katxfer.fakedata import (  # noqa: E402
    Rig,
    SYSTEM_IDS,
    environment_rows,
    experiment_rows,
    experimental_data_rows,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "kat_sqlite.sql"


def build(
    db_path: Path,
    runs: int,
    rows: int,
    env_samples: int,
    rigs: int,
    reset: bool,
    seed: int,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if reset and db_path.exists():
        db_path.unlink()
        print(f"removed existing {db_path}")

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA.read_text())
    conn.commit()

    rig_objs = [Rig(SYSTEM_IDS[i % len(SYSTEM_IDS)], seed + i) for i in range(rigs)]

    # Back-date the data so it looks like it was collected over the last few
    # hours rather than all at this instant.
    start = datetime.now(timezone.utc) - timedelta(hours=3)

    experiments = experiment_rows(rig_objs, runs)
    conn.executemany(
        "INSERT OR IGNORE INTO Experiment (systemid, run, description) VALUES (?, ?, ?)",
        experiments,
    )

    total_exp = 0
    for rig in rig_objs:
        for run in range(1, runs + 1):
            batch = experimental_data_rows(
                rig, run, rows, start + timedelta(minutes=12 * run)
            )
            conn.executemany(
                'INSERT OR IGNORE INTO ExperimentalData '
                '(systemid, run, "row", timestamp, source, data, xfer) '
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
            total_exp += len(batch)

    total_env = 0
    for rig in rig_objs:
        batch = environment_rows(rig, env_samples, start)
        conn.executemany(
            "INSERT INTO Environment (systemid, timestamp, data, xfer) "
            "VALUES (?, ?, ?, ?)",
            batch,
        )
        total_env += len(batch)

    conn.commit()

    counts = {
        name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for name in ("Experiment", "ExperimentalData", "Environment")
    }
    conn.close()

    print(f"wrote {db_path}")
    print(f"  Experiment       {counts['Experiment']:>8,}")
    print(f"  ExperimentalData {counts['ExperimentalData']:>8,}  (+{total_exp:,} this run)")
    print(f"  Environment      {counts['Environment']:>8,}  (+{total_env:,} this run)")
    print(f"  size             {db_path.stat().st_size / 1_048_576:>8.1f} MB")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "kat.sqlite")
    ap.add_argument("--rigs", type=int, default=2, help="distinct systemids")
    ap.add_argument("--runs", type=int, default=3, help="runs per rig")
    ap.add_argument("--rows", type=int, default=2000, help="data rows per run")
    ap.add_argument("--env", type=int, default=400, help="environment samples per rig")
    ap.add_argument("--seed", type=int, default=321)
    ap.add_argument("--reset", action="store_true", help="delete the file first")
    args = ap.parse_args()

    build(
        args.db,
        runs=args.runs,
        rows=args.rows,
        env_samples=args.env,
        rigs=args.rigs,
        reset=args.reset,
        seed=args.seed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
