"""Native server launcher and database-owner token administration commands."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import getpass
import json
import os
from pathlib import Path
import re
import stat
import sys

import psycopg2

from .config import DatabaseConfig, Settings
from .storage import ArchiveStore

DEFAULT_SETTINGS = Path(__file__).resolve().parent.parent / ".env.live.json"


def save_settings(path: Path, settings: Settings) -> None:
    """Create only; a repeated setup cannot replace existing database credentials."""
    payload = {"version": 2, "database": asdict(settings.database)}
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
    if (not isinstance(raw, dict) or set(raw) != {"version", "database"}
            or type(raw["version"]) is not int or raw["version"] != 2):
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
    return Settings(DatabaseConfig(**db))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--configure", action="store_true", help="check the archive and create private settings")
    actions.add_argument("--check", action="store_true", help="read archive counts without starting the API")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--admin-user", help="database owner/issuer login for token and user commands")
    parser.add_argument("--admin-password-env", help="read the issuer password from this environment variable")
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=5432)
    parser.add_argument("--pg-database", default="experimentaldata")
    parser.add_argument("--pg-user", default="csit")
    parser.add_argument("--pg-sslmode", default="prefer")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--root-path", default="", help="use /csit321 when the NGINX route is configured")

    commands = parser.add_subparsers(dest="command")
    tokens = commands.add_parser("token", help="manage API bearer tokens")
    token_actions = tokens.add_subparsers(dest="token_action", required=True)
    create = token_actions.add_parser("create")
    create.add_argument("--user", required=True)
    create.add_argument("--role", choices=("read", "write"), required=True)
    create.add_argument("--expires", default="90d")
    create.add_argument("--note")
    token_actions.add_parser("list")
    revoke = token_actions.add_parser("revoke")
    revoke.add_argument("selector", help="token id or identifying prefix")

    users = commands.add_parser("user", help="manage who is allowed API tokens")
    user_actions = users.add_subparsers(dest="user_action", required=True)
    add = user_actions.add_parser("add")
    add.add_argument("username")
    add.add_argument("--role", choices=("read", "write"), required=True)
    add.add_argument("--granted-by", required=True)
    add.add_argument("--note")
    set_role = user_actions.add_parser("set-role")
    set_role.add_argument("username")
    set_role.add_argument("role", choices=("read", "write"))
    set_role.add_argument("--granted-by", required=True)
    disable = user_actions.add_parser("disable")
    disable.add_argument("username")
    return parser


def _admin_store(settings: Settings, admin_user: str | None, password_env: str | None) -> ArchiveStore:
    if not admin_user:
        raise ValueError("Specify --admin-user for database-owner commands")
    password = os.environ.get(password_env, "") if password_env else getpass.getpass(
        f"PostgreSQL password for {admin_user}: "
    )
    if not password:
        raise ValueError("No database password entered")
    return ArchiveStore(replace(settings.database, user=admin_user, password=password))


def _expiry_days(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)d", value)
    if not match or int(match.group(1)) > 90:
        raise ValueError("Token expiry must be between 1d and 90d")
    return int(match.group(1))


def _run_admin(args, settings: Settings) -> int:
    store = _admin_store(settings, args.admin_user, args.admin_password_env)
    if args.command == "token":
        if args.token_action == "create":
            token_id, token = store.create_token(
                args.user, args.role, _expiry_days(args.expires), args.note
            )
            print(f"Token {token_id} (shown once): {token}")
        elif args.token_action == "list":
            for row in store.list_tokens():
                print(json.dumps({**row, "revoked": row["revoked_at"] is not None},
                                 default=str, sort_keys=True))
        else:
            if not store.revoke_token(args.selector):
                raise ValueError("No active token matched that id or prefix")
            print("Token revoked.")
    elif args.user_action == "add":
        store.add_user(args.username, args.role, args.granted_by, args.note)
        print("User added.")
    elif args.user_action == "set-role":
        if not store.set_user_role(args.username, args.role, args.granted_by):
            raise ValueError("No active user matched that username")
        print("User role updated.")
    else:
        if not store.disable_user(args.username):
            raise ValueError("No active user matched that username")
        print("User disabled.")
    return 0


def main(argv=None) -> int:
    parser = _parser()
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
            settings = Settings(DatabaseConfig(
                host=args.pg_host, port=args.pg_port, database=args.pg_database,
                user=args.pg_user, password=password, sslmode=args.pg_sslmode,
            ))
            counts = ArchiveStore(settings.database).counts()
            save_settings(args.settings, settings)
            print("Archive connection verified:", counts)
            print("Private server settings saved.")
            return 0
        try:
            settings = load_settings(args.settings)
        except FileNotFoundError:
            if args.command and os.environ.get("KAT_PG_DATABASE") and os.environ.get("KAT_PG_USER"):
                settings = Settings.from_env()
            else:
                raise
        if args.command:
            if args.check:
                parser.error("admin commands cannot be combined with --check")
            return _run_admin(args, settings)
        if args.check:
            print("Archive connection verified:", ArchiveStore(settings.database).counts())
            return 0
        from .app import create_app
        import uvicorn
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port,
                    root_path=args.root_path, access_log=False)
        return 0
    except psycopg2.Error as exc:
        print(f"Archive operation failed ({type(exc).__name__}); check the database login and permissions.",
              file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        if isinstance(exc, FileNotFoundError):
            message = "Server settings are missing; run --configure first"
        elif isinstance(exc, FileExistsError):
            message = "Settings already exist; use --check to verify them"
        elif isinstance(exc, json.JSONDecodeError):
            message = "The server settings file is not valid JSON"
        else:
            message = str(exc)
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
