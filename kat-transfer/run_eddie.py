"""Entry point for the packaged Eddie Transfer Service."""

import os
import sys
from pathlib import Path


def configure_working_directory():
    if getattr(sys, "frozen", False):
        app_dir = Path(sys.executable).resolve().parent
    else:
        app_dir = Path(__file__).resolve().parent

    os.chdir(app_dir)


if __name__ == "__main__":
    configure_working_directory()

    # Same entry point as `python -m katapp`, so the exe also accepts -c.
    from katapp.__main__ import main

    raise SystemExit(main())