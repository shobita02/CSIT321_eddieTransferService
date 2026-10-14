"""Run the local KAT katdata.db -> NGINX -> API -> PostgreSQL demonstration.

    python tools/demo.py            send rows of ~/katdata.db not yet transferred
    python tools/demo.py --resend   back up katdata.db, clear its xfer stamps, send everything
"""
import argparse
from datetime import datetime
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
KAT_DB = (Path.home() / "katdata.db").resolve()
sys.path.insert(0, str(ROOT))


def clear_stamps(db: Path) -> None:
    """Keep a timestamped copy, then mark every row as not yet transferred."""
    backup = db.with_name(f"katdata.backup-{datetime.now():%Y%m%d-%H%M%S}.db")
    with sqlite3.connect(db) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE ExperimentalData SET xfer = NULL")
        conn.execute("UPDATE Environment SET xfer = NULL")
    print(f"Backed up katdata.db to {backup} and cleared its xfer stamps")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--resend", action="store_true",
                        help="back up katdata.db, clear its xfer stamps and send every row again")
    args = parser.parse_args()

    env_file = ROOT / ".env"
    if not env_file.exists():
        subprocess.run([sys.executable, str(ROOT / "tools/new_demo_env.py")], check=True)
    allowed = {"KAT_PG_DATABASE", "KAT_PG_USER", "KAT_PG_PASSWORD", "KAT_API_WRITE_TOKEN", "KAT_API_READ_TOKEN"}
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            if name in allowed:
                os.environ[name] = value

    from katxfer.config import load
    from katxfer.localdb import LocalKatDb
    from katxfer.service import TransferService
    cfg = load(ROOT / "config.toml")
    if cfg.local.path != KAT_DB or cfg.remote.url != "http://127.0.0.1:8080":
        raise SystemExit(f"Demo requires config.toml to use {KAT_DB} and the local gateway http://127.0.0.1:8080")
    if not KAT_DB.exists():
        raise SystemExit(f"{KAT_DB} not found. Record some data with KAT first.")
    if args.resend:
        clear_stamps(KAT_DB)
    if not any(LocalKatDb(KAT_DB).pending_counts().values()):
        raise SystemExit("Every row in katdata.db is already marked as transferred. "
                         "Run `python tools/demo.py --resend` to send them to the local demo archive again.")

    subprocess.run(["docker", "compose", "up", "-d", "--build", "--wait"], cwd=ROOT, check=True)
    print(TransferService(cfg).status())
    print(f"Transferred {TransferService(cfg).run_once()} rows through the gateway")
    print(TransferService(cfg).status())
    subprocess.run([sys.executable, str(ROOT / "tools/verify_transfer.py")], cwd=ROOT, check=True)
    print("API documentation: http://127.0.0.1:8080/docs")


if __name__ == "__main__":
    main()
