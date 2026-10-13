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
    # Used only for the local SQLite connection; archive reads use HTTP below.
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

    from katxfer.sinks import HttpSink, GatewayError

    try:
        with HttpSink(cfg.remote, cfg.origin, cfg.src_host) as sink:
            print(f"\nREMOTE (gateway) {cfg.remote.url}")
            print("  Counts:", sink.counts())
            for resource in ("experiments", "experimental-data", "environment", "transfers"):
                page = sink.request("GET", f"v1/{resource}",
                                    params={"limit": min(args.limit, 1000), "offset": 0})
                print(f"\n{resource}")
                for row in page["items"]:
                    print(" ", row)
                if not page["items"]:
                    print("  (no rows)")
    except GatewayError as exc:
        print(f"Gateway unavailable: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
