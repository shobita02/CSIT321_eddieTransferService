"""Create local demo database settings without replacing an existing file."""
from pathlib import Path

path = Path(__file__).resolve().parent.parent / ".env"
if path.exists():
    raise SystemExit(".env already exists; reuse it or edit it explicitly")
path.write_text(
    "KAT_PG_DATABASE=csit321\nKAT_PG_USER=kat\nKAT_PG_PASSWORD=kat-local-demo-only\n",
    encoding="utf-8",
)
print("Created .env with local demo database settings. Keep this file out of Git.")
