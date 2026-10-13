"""Create local demo tokens without printing them or replacing an existing file."""
import secrets
from pathlib import Path

path = Path(__file__).resolve().parent.parent / ".env"
if path.exists():
    raise SystemExit(".env already exists; reuse it or edit it explicitly")
path.write_text(
    "KAT_PG_DATABASE=csit321\nKAT_PG_USER=kat\nKAT_PG_PASSWORD=kat-local-demo-only\n"
    f"KAT_API_WRITE_TOKEN={secrets.token_urlsafe(32)}\n"
    f"KAT_API_READ_TOKEN={secrets.token_urlsafe(32)}\n",
    encoding="utf-8",
)
print("Created .env with separate local demo tokens. Keep this file out of Git.")
