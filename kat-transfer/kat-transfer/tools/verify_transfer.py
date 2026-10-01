#!/usr/bin/env python3
"""Check that what left the laptop actually arrived.

Compares the local KAT database against the remote archive row by row and
reports four things:

  missing    - stamped locally as transferred, absent remotely (data loss)
  mismatched - present both sides, but the payload differs (corruption)
  unstamped  - present remotely, still NULL locally (a crash between the
               remote commit and the local stamp; harmless, it will be
               re-sent and upserted, but worth knowing about)
  pending    - not yet transferred (normal if the service is running)

Exit status is non-zero if anything in the first two categories is found, so
this can go straight into a CI step or a pre-demo check.

    python3 tools/verify_transfer.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from katxfer.config import load as load_config  # noqa: E402
from katxfer.localdb import EnvironmentRow  # noqa: E402


def fetch_remote_experimental(cfg) -> dict[tuple, tuple]:
    if cfg.remote.kind == "sqlite":
        conn = sqlite3.connect(cfg.remote.path)
        rows = conn.execute(
            "SELECT systemid, run, row_no, data FROM experimental_data"
        ).fetchall()
        conn.close()
    else:
        import psycopg2

        conn = psycopg2.connect(
            host=cfg.remote.host,
            port=cfg.remote.port,
            dbname=cfg.remote.database,
            user=cfg.remote.user,
            password=cfg.remote.password,
            sslmode=cfg.remote.sslmode,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT systemid, run, row_no, data FROM experimental_data")
            rows = cur.fetchall()
        conn.close()
    return {(r[0], r[1], r[2]): r[3] for r in rows}


def fetch_remote_environment(cfg) -> set[str]:
    if cfg.remote.kind == "sqlite":
        conn = sqlite3.connect(cfg.remote.path)
        rows = conn.execute("SELECT digest FROM environment").fetchall()
        conn.close()
    else:
        import psycopg2

        conn = psycopg2.connect(
            host=cfg.remote.host,
            port=cfg.remote.port,
            dbname=cfg.remote.database,
            user=cfg.remote.user,
            password=cfg.remote.password,
            sslmode=cfg.remote.sslmode,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT digest FROM environment")
            rows = cur.fetchall()
        conn.close()
    return {r[0].strip() for r in rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", type=Path, default=None)
    ap.add_argument("--show", type=int, default=5, help="example keys to print")
    args = ap.parse_args()

    cfg = load_config(args.config)
    local = sqlite3.connect(f"file:{cfg.local.path}?mode=ro", uri=True)
    local.row_factory = sqlite3.Row

    remote_exp = fetch_remote_experimental(cfg)
    remote_env = fetch_remote_environment(cfg)

    missing, mismatched, unstamped, pending = [], [], [], 0

    for r in local.execute(
        'SELECT systemid, run, "row" AS row_no, data, xfer FROM ExperimentalData'
    ):
        key = (r["systemid"], r["run"], r["row_no"])
        there = key in remote_exp
        if r["xfer"] is None:
            if there:
                unstamped.append(key)
            else:
                pending += 1
            continue
        if not there:
            missing.append(key)
        elif remote_exp[key] != r["data"]:
            mismatched.append(key)

    env_missing, env_unstamped, env_pending = [], [], 0
    for r in local.execute("SELECT rowid, systemid, timestamp, data, xfer FROM Environment"):
        digest = EnvironmentRow(r["systemid"], r["timestamp"], r["data"], r["rowid"]).digest
        there = digest in remote_env
        if r["xfer"] is None:
            if there:
                env_unstamped.append(digest[:12])
            else:
                env_pending += 1
        elif not there:
            env_missing.append(digest[:12])

    local.close()

    def report(label: str, items: list, extra: str = "") -> None:
        n = len(items)
        flag = "FAIL" if n and label.endswith(("missing", "mismatched")) else "ok  "
        print(f"  [{flag}] {label:<28} {n:>8,}{extra}")
        for key in items[: args.show]:
            print(f"           {key}")
        if n > args.show:
            print(f"           ... and {n - args.show:,} more")

    print(f"local : {cfg.local.path}")
    target = (
        cfg.remote.path
        if cfg.remote.kind == "sqlite"
        else f"{cfg.remote.host}:{cfg.remote.port}/{cfg.remote.database}"
    )
    print(f"remote: {target}\n")

    print("ExperimentalData")
    report("missing", missing)
    report("mismatched", mismatched)
    report("unstamped (will re-send)", unstamped)
    print(f"  [ok  ] {'pending':<28} {pending:>8,}")
    print(f"  [ok  ] {'remote rows':<28} {len(remote_exp):>8,}")

    print("\nEnvironment")
    report("missing", env_missing)
    report("unstamped (will re-send)", env_unstamped)
    print(f"  [ok  ] {'pending':<28} {env_pending:>8,}")
    print(f"  [ok  ] {'remote rows':<28} {len(remote_env):>8,}")

    bad = len(missing) + len(mismatched) + len(env_missing)
    print("\n" + ("VERIFIED - no loss, no corruption" if bad == 0 else f"{bad} PROBLEM(S)"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
