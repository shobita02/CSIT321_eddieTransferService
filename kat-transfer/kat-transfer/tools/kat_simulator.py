#!/usr/bin/env python3
"""Stand-in for KAT's data generator plus its 0MQ publisher.

Writes new rows into the local KAT database at a steady rate and announces
each write on a PUB socket, which is the behaviour the real KAT release is
expected to have. Running this next to the transfer service gives you the
whole live pipeline on one laptop.

    python3 tools/kat_simulator.py --rate 20 --duration 60

Swap it out when Jonathan's k4 generator is available: the transfer service
never imports this file, and only cares that rows appear with `xfer` NULL.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from katxfer.fakedata import Rig, SOURCES, SYSTEM_IDS, fmt_ts  # noqa: E402
from katxfer.zmqbus import (  # noqa: E402
    TOPIC_ENVIRONMENT,
    TOPIC_EXPERIMENTAL,
    TOPIC_RUN_FINISHED,
    TOPIC_RUN_STARTED,
    Publisher,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "kat_sqlite.sql"

log = logging.getLogger("kat-sim")
_stop = False


def _handle_stop(*_: object) -> None:
    global _stop
    _stop = True


def next_run_number(conn: sqlite3.Connection, systemid: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(run), 0) + 1 FROM Experiment WHERE systemid = ?",
        (systemid,),
    ).fetchone()
    return int(row[0])


def simulate(
    db_path: Path,
    endpoint: str,
    rate: float,
    duration: float,
    systemid: str,
    env_every: float,
    seed: int,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.executescript(SCHEMA.read_text())
    conn.commit()

    rig = Rig(systemid, seed)
    run = next_run_number(conn, systemid)

    with Publisher(endpoint) as pub:
        conn.execute(
            "INSERT OR IGNORE INTO Experiment (systemid, run, description) "
            "VALUES (?, ?, ?)",
            (systemid, run, f"Simulated live run {run} on {systemid}"),
        )
        conn.commit()
        pub.send(TOPIC_RUN_STARTED, {"systemid": systemid, "run": run})
        log.info("run %d started on %s -> %s", run, systemid, db_path)

        interval = 1.0 / rate if rate > 0 else 0.0
        started = time.monotonic()
        next_env = started
        row_no = 0
        env_written = 0

        while not _stop and (duration <= 0 or time.monotonic() - started < duration):
            cycle_start = time.monotonic()
            row_no += 1
            now = datetime.now(timezone.utc)

            conn.execute(
                'INSERT OR IGNORE INTO ExperimentalData '
                '(systemid, run, "row", timestamp, source, data, xfer) '
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    systemid,
                    run,
                    row_no,
                    fmt_ts(now),
                    SOURCES[row_no % len(SOURCES)],
                    rig.experimental_payload(row_no),
                ),
            )

            if time.monotonic() >= next_env:
                conn.execute(
                    "INSERT INTO Environment (systemid, timestamp, data, xfer) "
                    "VALUES (?, ?, ?, NULL)",
                    (systemid, fmt_ts(now), rig.environment_payload(env_written)),
                )
                env_written += 1
                next_env = time.monotonic() + env_every
                conn.commit()
                # The notification carries identifying detail but no payload.
                # A subscriber that trusts the body to be complete will lose
                # data the first time a message is dropped; the body is here
                # for logging and for routing, not as a substitute for the
                # database.
                pub.send(
                    TOPIC_ENVIRONMENT,
                    {"systemid": systemid, "timestamp": fmt_ts(now), "count": 1},
                )

            conn.commit()
            pub.send(
                TOPIC_EXPERIMENTAL,
                {
                    "systemid": systemid,
                    "run": run,
                    "row": row_no,
                    "timestamp": fmt_ts(now),
                },
            )

            if row_no % max(1, int(rate * 5)) == 0:
                log.info("run %d: %d rows, %d env samples", run, row_no, env_written)

            sleep_for = interval - (time.monotonic() - cycle_start)
            if sleep_for > 0:
                time.sleep(sleep_for)

        pub.send(
            TOPIC_RUN_FINISHED,
            {"systemid": systemid, "run": run, "rows": row_no},
        )
        log.info("run %d finished: %d rows, %d env samples", run, row_no, env_written)

    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "kat.sqlite")
    ap.add_argument("--endpoint", default="tcp://*:5556")
    ap.add_argument("--rate", type=float, default=10.0, help="rows per second")
    ap.add_argument(
        "--duration", type=float, default=0.0, help="seconds; 0 runs until Ctrl-C"
    )
    ap.add_argument("--systemid", default=SYSTEM_IDS[0])
    ap.add_argument(
        "--env-every", type=float, default=5.0, help="seconds between env samples"
    )
    ap.add_argument("--seed", type=int, default=99)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    simulate(
        args.db,
        args.endpoint,
        args.rate,
        args.duration,
        args.systemid,
        args.env_every,
        args.seed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
