"""Read the gateway address before sign-in, when no token exists yet."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tomllib

from katxfer.config import RemoteConfig


@dataclass(frozen=True)
class GatewaySettings:
    url: str
    ca_file: str | None
    timeout_s: float


def default_config_path() -> Path:
    """config.toml beside the exe wins; otherwise the copy built into the exe (tools/build_app.py)."""
    if getattr(sys, "frozen", False):
        beside = Path(sys.executable).resolve().parent / "config.toml"
        return beside if beside.exists() else Path(sys._MEIPASS) / "config.toml"
    return Path(__file__).resolve().parent.parent / "config.toml"


def gateway_settings(path: str | Path) -> GatewaySettings:
    """The [remote] section of a client config, checked by the same rules as the service.

    katxfer.config.load() needs the token, so it can only run after sign-in.
    """
    path = Path(path)
    with open(path, "rb") as fh:
        remote = tomllib.load(fh).get("remote", {})
    ca_file = remote.get("ca_file")
    if ca_file:
        ca = Path(os.path.expandvars(ca_file)).expanduser()
        ca_file = str(ca if ca.is_absolute() else (path.parent / ca).resolve())
    # A placeholder token lets RemoteConfig apply its URL and TLS checks.
    checked = RemoteConfig(
        url=os.path.expandvars(remote.get("url", "http://127.0.0.1:8080")),
        token="placeholder-before-sign-in",
        timeout_s=float(remote.get("timeout_s", 30)),
        ca_file=ca_file,
    )
    return GatewaySettings(checked.url, checked.ca_file, checked.timeout_s)
