"""Run the local SQLite -> NGINX -> API -> PostgreSQL demonstration."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
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
    from katxfer.service import TransferService
    cfg = load(ROOT / "config.toml")
    if cfg.local.path != (ROOT / "data/kat.sqlite").resolve() or cfg.origin != "fake":
        raise SystemExit("Demo requires config.toml to use data/kat.sqlite and origin='fake'")
    subprocess.run(["docker", "compose", "up", "-d", "--build", "--wait"], cwd=ROOT, check=True)
    if not cfg.local.path.exists():
        subprocess.run([sys.executable, str(ROOT / "tools/make_fake_db.py"),
                        "--rigs", "2", "--runs", "2", "--rows", "100", "--env", "10"],
                       cwd=ROOT, check=True)
    print(TransferService(cfg).status())
    print(f"Transferred {TransferService(cfg).run_once()} rows through the gateway")
    print(TransferService(cfg).status())
    subprocess.run([sys.executable, str(ROOT / "tools/verify_transfer.py")], cwd=ROOT, check=True)
    print("API documentation: http://127.0.0.1:8080/docs")


if __name__ == "__main__":
    main()
