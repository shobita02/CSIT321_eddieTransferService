"""Package the server source for a native Linux deployment, without local secrets."""
from __future__ import annotations

import ast
import hashlib
import io
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output" / "kat-gateway-server.tar.gz"

SETUP = """# Prepare the gateway in a team server account

This package contains the API source and administrator templates. It contains
no credentials, VPN profile, databases or Python environment.

On the Ubuntu server using your assigned account, after uploading kat-gateway-server.tar.gz:

```bash
mkdir -p ~/csit321-gateway
tar -xzf ~/kat-gateway-server.tar.gz -C ~/csit321-gateway
cd ~/csit321-gateway/kat-transfer
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-server.txt
.venv/bin/python -c "import fastapi, uvicorn, psycopg2; print('Server dependencies are ready')"
```

These commands prepare a user-owned environment. They do not start the API,
write to PostgreSQL, install a system service, or change NGINX.

Continue with docs/deployment.md for server settings and the foreground API
check. Once dependencies are ready, run each launcher command separately:

```bash
.venv/bin/python -m katgateway --configure
.venv/bin/python -m katgateway --check
.venv/bin/python -m katgateway --port 8001
```

Configure prompts for the restricted API database password, checks archive
counts and writes .env.live.json with permissions 600 in the current account's
checkout. Apply schema/migrations/002_api_tokens.sql to the live archive as its
owner before starting the new gateway. Create users and tokens separately with
the owner-only CLI; the command above binds only to 127.0.0.1:8001. Leave it
running for the SSH-tunnel check; the administrator's NGINX route is separate.
Coordinate the selected account: only one API instance can use port 8001 at a time.

The administrator service template uses TEAM_USER and the path
/home/TEAM_USER/csit321-gateway/kat-transfer. Replace every TEAM_USER with the
selected server username and use the actual absolute checkout path if it differs.
remote_postgres.sql is only for a new archive and must not be executed on live
data. The separate token migration above is safe for the existing archive.

SOURCE_MANIFEST.json records the SHA-256 of every packaged source file.
"""


def main() -> None:
    files = sorted((ROOT / "katgateway").glob("*.py"))
    files += [ROOT / name for name in (
        "requirements-server.txt", ".gitignore",
        "deploy/nginx.live.conf", "deploy/katgateway.service.example", "deploy/grants.example.sql",
        "docs/deployment.md", "docs/gateway-api.md", "docs/pgadmin-checks.sql",
        "schema/remote_postgres.sql", "schema/migrations/002_api_tokens.sql",
    )]
    manifest = {}
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"Missing or unexpected source file: {path.name}")
        if path.suffix == ".py":
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=path.name)
        manifest[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(OUTPUT, "w:gz") as archive:
        for path in files:
            name = "kat-transfer/" + path.relative_to(ROOT).as_posix()
            info = archive.gettarinfo(str(path), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            with path.open("rb") as stream:
                archive.addfile(info, stream)
        for name, content in (
            ("SERVER_SETUP.md", SETUP),
            ("SOURCE_MANIFEST.json", json.dumps({"component": "katgateway", "files": manifest}, indent=2) + "\n"),
        ):
            blob = content.encode("utf-8")
            info = tarfile.TarInfo("kat-transfer/" + name)
            info.size = len(blob)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(blob))

    with tarfile.open(OUTPUT, "r:gz") as archive:
        names = archive.getnames()
        if any(not name.startswith("kat-transfer/") or ".." in Path(name).parts for name in names):
            raise SystemExit("Unexpected archive member")
        if any(name.endswith((".ovpn", ".sqlite", ".db", ".pyc"))
               or "/.venv/" in name or Path(name).name.startswith(".env") for name in names):
            raise SystemExit("Private or generated material found in package")
        stored = json.loads(archive.extractfile("kat-transfer/SOURCE_MANIFEST.json").read())
        for name, digest in stored["files"].items():
            data = archive.extractfile("kat-transfer/" + name).read()
            if hashlib.sha256(data).hexdigest() != digest:
                raise SystemExit(f"Package integrity mismatch: {name}")
    print(f"Server package created and verified: {OUTPUT}")
    print(f"{len(names)} files; {OUTPUT.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
