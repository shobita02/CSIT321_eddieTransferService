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

SETUP = """# Prepare the gateway in Chris's server account

This package contains the API source and administrator templates. It contains
no credentials, VPN profile, databases or Python environment.

On the Ubuntu server as chrisw, after uploading kat-gateway-server.tar.gz:

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

Configure prompts for the database password, checks archive counts and writes
.env.live.json with permissions 600. It creates separate read/write API tokens
without printing them. The command above binds only to 127.0.0.1:8001. Leave it
running for the SSH-tunnel check; the administrator's NGINX route is separate.

The administrator templates use /home/chrisw/csit321-gateway/kat-transfer;
adjust them if a different directory is used. The schema file is a reference
only: the existing experimentaldata archive already has the expected tables.
Do not execute schema initialisation SQL as part of this preparation.

SOURCE_MANIFEST.json records the SHA-256 of every packaged source file.
"""


def main() -> None:
    files = sorted((ROOT / "katgateway").glob("*.py"))
    files += [ROOT / name for name in (
        "requirements-server.txt", ".gitignore",
        "deploy/nginx.live.conf", "deploy/katgateway.service.example", "deploy/grants.example.sql",
        "docs/deployment.md", "docs/gateway-api.md", "docs/pgadmin-checks.sql",
        "schema/remote_postgres.sql",
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
