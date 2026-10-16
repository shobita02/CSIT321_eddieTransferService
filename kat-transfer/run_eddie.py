"""Entry point for the packaged Eddie Transfer Service."""

import os
import runpy
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

    # Execute katapp.main as the application entry point.
    runpy.run_module(
        "katapp.main",
        run_name="__main__",
        alter_sys=True,
    )