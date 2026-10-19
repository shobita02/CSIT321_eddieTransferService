"""Build the desktop app as one Windows exe: dist/EddieTransfer.exe (needs pyinstaller).

The chosen client config is built into the exe, so the exe runs on its own from the
desktop. A config.toml placed beside the exe overrides it. Configs hold only the
gateway URL and a ${KAT_API_WRITE_TOKEN} placeholder, never a token or password.

    python tools/build_app.py                          # office gateway through the SSH tunnel
    python tools/build_app.py -c config.live.toml      # office HTTPS gateway, once NGINX is set up
    python tools/build_app.py -c config.toml --name EddieTransferLocal   # local Docker gateway
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--config", default="config.tunnel.toml", help="client config to build in")
    parser.add_argument("--name", default="EddieTransfer", help="exe name")
    args = parser.parse_args()

    config = (ROOT / args.config).resolve()
    if 'token = "${KAT_API_WRITE_TOKEN}"' not in config.read_text(encoding="utf-8"):
        raise SystemExit(f"{config.name}: [remote] token must be the ${{KAT_API_WRITE_TOKEN}} placeholder")

    import PyInstaller.__main__

    with tempfile.TemporaryDirectory() as work:
        # Bundled under the name the app looks for (katapp.appconfig.default_config_path).
        bundled = Path(work) / "config.toml"
        shutil.copyfile(config, bundled)
        PyInstaller.__main__.run([
            str(ROOT / "run_eddie.py"),
            "--name", args.name,
            "--onefile",
            "--windowed",
            "--noconfirm",
            "--clean",
            "--collect-data", "customtkinter",
            "--add-data", f"{bundled}{os.pathsep}.",
            "--distpath", str(ROOT / "dist"),
            "--workpath", str(Path(work) / "build"),
            "--specpath", work,
            "--paths", str(ROOT),
        ])
    print(f"Built {ROOT / 'dist' / (args.name + '.exe')} with {config.name} built in")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
