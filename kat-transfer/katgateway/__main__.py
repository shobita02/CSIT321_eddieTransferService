"""Native server launcher with a private, reusable settings file."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import getpass
import json
import os
from pathlib import Path
import secrets
import stat
import sys

import psycopg2

from .config import DatabaseConfig, Settings
from .storage import ArchiveStore

DEFAULT_SETTINGS = Path(__file__).resolve().parent.parent / ".env.live.json"


def save_settings(path: Path, settings: Settings) -> None:
    """Create only; a repeated setup cannot replace existing credentials/tokens."""
    payload = {"version": 1, "database": asdict(settings.database),
               "write_token": settings.write_token, "read_token": settings.read_token}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")


def load_settings(path: Path) -> Settings:
    if path.is_symlink():
        raise ValueError("The settings file must be a regular private file")
    if os.name == "posix":
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Settings must belong to this account and have permissions 600")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(raw, dict) or set(raw) != {"version", "database", "read_token", "write_token"}
            or type(raw["version"]) is not int or raw["version"] != 1):
        raise ValueError("Unsupported server settings format")
    db = raw["database"]
    fields = {"host", "port", "database", "user", "password", "sslmode", "connect_timeout_s"}
    if not isinstance(db, dict) or set(db) != fields:
        raise ValueError("Incomplete database settings")
    if any(not isinstance(db[name], str) or not db[name] or "${" in db[name]
           for name in ("host", "database", "user", "password", "sslmode")):
        raise ValueError("Database settings must contain nonempty values")
    if (type(db["port"]) is not int or not 1 <= db["port"] <= 65535
            or type(db["connect_timeout_s"]) is not int or db["connect_timeout_s"] <= 0):
        raise ValueError("Invalid database port or timeout")
    if any(not isinstance(raw[name], str) for name in ("write_token", "read_token")):
        raise ValueError("Invalid API tokens")
    return Settings(DatabaseConfig(**db), raw["write_token"], raw["read_token"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--configure", action="store_true", help="check the archive and create private settings")
    actions.add_argument("--check", action="store_true", help="read archive counts without starting the API")
    actions.add_argument("--print-token", choices=("read", "write"), help="print only the selected token for an authorised client")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=5432)
    parser.add_argument("--pg-database", default="experimentaldata")
    parser.add_argument("--pg-user", default="csit")
    parser.add_argument("--pg-sslmode", default="prefer")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--root-path", default="", help="use /csit321 when the NGINX route is configured")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or not 1 <= args.pg_port <= 65535:
        parser.error("ports must be between 1 and 65535")
    try:
        if args.configure:
            if args.settings.exists() or args.settings.is_symlink():
                raise FileExistsError("Settings already exist; use --check to verify them")
            password = getpass.getpass(f"PostgreSQL password for {args.pg_user}: ")
            if not password:
                raise ValueError("No database password entered")
            settings = Settings(
                DatabaseConfig(host=args.pg_host, port=args.pg_port, database=args.pg_database,
                               user=args.pg_user, password=password, sslmode=args.pg_sslmode),
                secrets.token_urlsafe(32), secrets.token_urlsafe(32),
            )
            counts = ArchiveStore(settings.database).counts()
            save_settings(args.settings, settings)
            print("Archive connection verified:", counts)
            print("Private server settings saved; separate read and write tokens generated.")
            return 0
        settings = load_settings(args.settings)
        if args.check:
            print("Archive connection verified:", ArchiveStore(settings.database).counts())
            return 0
        if args.print_token:
            # Explicit opt-in only; normal startup and setup never print secrets.
            print(settings.read_token if args.print_token == "read" else settings.write_token)
            return 0
        from .app import create_app
        import uvicorn
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port,
                    root_path=args.root_path, access_log=False)
        return 0
    except psycopg2.Error as exc:
        print(f"Archive check failed ({type(exc).__name__}); check the server address and database login.", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        # OSError/JSON errors do not need a traceback or submitted values.
        if isinstance(exc, FileNotFoundError):
            message = "Server settings are missing; run --configure first"
        elif isinstance(exc, FileExistsError):
            message = "Settings already exist; use --check to verify them"
        elif isinstance(exc, json.JSONDecodeError):
            message = "The server settings file is not valid JSON"
        else:
            message = "Server setup failed; check settings, file permissions and the selected port"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
