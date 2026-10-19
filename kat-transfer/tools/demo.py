"""Run the local KAT katdata.db -> NGINX -> API -> PostgreSQL demonstration.

    python tools/demo.py            send rows of ~/katdata.db not yet transferred
    python tools/demo.py --resend   back up katdata.db, clear its xfer stamps, send everything
"""
import argparse
from datetime import datetime
import os
from pathlib import Path
import secrets
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
    allowed = {"KAT_PG_DATABASE", "KAT_PG_USER", "KAT_PG_PASSWORD"}
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            if name in allowed:
                os.environ[name] = value

    from katxfer.localdb import LocalKatDb
    if not KAT_DB.exists():
        raise SystemExit(f"{KAT_DB} not found. Record some data with KAT first.")
    if args.resend:
        clear_stamps(KAT_DB)
    if not any(LocalKatDb(KAT_DB).pending_counts().values()):
        raise SystemExit("Every row in katdata.db is already marked as transferred. "
                         "Run `python tools/demo.py --resend` to send them to the local demo archive again.")

    subprocess.run(["docker", "compose", "up", "-d", "--build", "--wait"], cwd=ROOT, check=True)
    # A demo volume created before a migration existed does not rerun the schema; apply them (idempotent).
    migrations = "
".join(path.read_text(encoding="utf-8")
                           for path in sorted((ROOT / "schema/migrations").glob("*.sql")))
    subprocess.run(["docker", "compose", "exec", "-T", "remote-db", "psql", "-q", "-v", "ON_ERROR_STOP=1",
                    "-U", os.environ["KAT_PG_USER"], "-d", os.environ["KAT_PG_DATABASE"]],
                   cwd=ROOT, check=True, input=migrations, capture_output=True, text=True)
    username = "demo-" + secrets.token_hex(4)
    admin = ["docker", "compose", "exec", "-T", "gateway", "python", "-m", "katgateway",
             "--admin-user", os.environ["KAT_PG_USER"],
             "--admin-password-env", "KAT_PG_PASSWORD"]
    subprocess.run(
        [*admin, "user", "add", username, "--role", "write", "--granted-by", "local-demo"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    issued = subprocess.run(
        [*admin, "token", "create", "--user", username, "--role", "write",
         "--expires", "90d", "--note", "local demo"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    prefix = f"Token "
    token_line = next((line for line in issued.stdout.splitlines() if line.startswith(prefix)), "")
    if ": " not in token_line:
        raise RuntimeError("Token administration command did not return the one-time token")
    os.environ["KAT_API_WRITE_TOKEN"] = token_line.rsplit(": ", 1)[1]
    # A sign-in password for the same demo user, for trying /v1/auth/login and the desktop app.
    password = secrets.token_urlsafe(18)
    subprocess.run(
        ["docker", "compose", "exec", "-T", "-e", f"KAT_DEMO_PASSWORD={password}", *admin[4:],
         "user", "set-password", username, "--password-env", "KAT_DEMO_PASSWORD"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    managed = ("KAT_API_WRITE_TOKEN=", "KAT_DEMO_USER=", "KAT_DEMO_PASSWORD=")
    env_lines = [line for line in env_file.read_text(encoding="utf-8-sig").splitlines()
                 if not line.startswith(managed)]
    env_lines.append(f"KAT_API_WRITE_TOKEN={os.environ['KAT_API_WRITE_TOKEN']}")
    env_lines.append(f"KAT_DEMO_USER={username}")
    env_lines.append(f"KAT_DEMO_PASSWORD={password}")
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    from katxfer.config import load
    from katxfer.service import TransferService
    cfg = load(ROOT / "config.toml")
    if cfg.local.path != KAT_DB or cfg.remote.url != "http://127.0.0.1:8080":
        raise SystemExit(f"Demo requires config.toml to use {KAT_DB} and the local gateway http://127.0.0.1:8080")

    print(TransferService(cfg).status())
    print(f"Transferred {TransferService(cfg).run_once()} rows through the gateway")
    print(TransferService(cfg).status())
    subprocess.run([sys.executable, str(ROOT / "tools/verify_transfer.py")], cwd=ROOT, check=True)
    print("API documentation: http://127.0.0.1:8080/docs")


if __name__ == "__main__":
    main()
