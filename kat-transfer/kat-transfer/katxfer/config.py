"""Configuration loading.

Everything the service needs to know lives in one TOML file so that moving
from "laptop + mock remote" to "laptop + Bored Owl dev server" is a config
change, not a code change.
"""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


@dataclass
class LocalConfig:
    """The KAT-side SQLite database we read from."""

    path: Path
    # How many rows to pull and ship in a single transaction.
    batch_size: int = 500
    # Seconds to wait for the SQLite write lock. KAT holds it while inserting.
    busy_timeout_s: float = 10.0


@dataclass
class ZmqConfig:
    """The notification channel KAT publishes on."""

    enabled: bool = True
    # KAT is the publisher, so the service connects outward to it.
    endpoint: str = "tcp://127.0.0.1:5556"
    # Empty string subscribes to everything. KAT's topics are not finalised
    # yet, so we default to "all" and filter in Python.
    topics: list[str] = field(default_factory=lambda: [""])
    # If True the service binds instead of connects. Only useful when you are
    # testing without a publisher.
    bind: bool = False


@dataclass
class RemoteConfig:
    """Where rows are shipped to."""

    # "postgres" once the dev server is reachable, "sqlite" for the mock.
    kind: str = "sqlite"

    # --- sqlite mock remote ---
    path: Path | None = None

    # --- postgres ---
    host: str = "192.168.40.100"
    port: int = 5432
    database: str = "csit321"
    user: str = ""
    password: str = ""
    sslmode: str = "prefer"
    connect_timeout_s: int = 10


@dataclass
class ServiceConfig:
    local: LocalConfig
    remote: RemoteConfig
    zmq: ZmqConfig
    # Safety-net sweep. ZeroMQ PUB/SUB is lossy by design (see docs/zeromq.md),
    # so we never rely on notifications alone.
    sweep_interval_s: float = 30.0
    # After a notification wakes us, keep absorbing notifications for this long
    # before draining. KAT publishes per row, so without this a 20 row/s run
    # becomes 20 transactions per second -- one network round trip per row once
    # the remote is Postgres across the VPN. Raising it trades latency for
    # fewer, larger batches.
    notify_debounce_s: float = 0.5
    # Marks every row this service ships. Set to "fake" while testing against
    # generated data so the remote archive can tell the two apart.
    origin: str = "fake"
    src_host: str = field(default_factory=socket.gethostname)
    # Seconds to back off after a failed remote write, doubling up to the cap.
    retry_base_s: float = 2.0
    retry_max_s: float = 60.0
    log_level: str = "INFO"


def _expand(value: str) -> str:
    """Allow ${ENV_VAR} in string config values, for credentials."""
    return os.path.expandvars(value) if isinstance(value, str) else value


def load(path: str | Path | None = None) -> ServiceConfig:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"No config at {path}. Copy config.example.toml to config.toml."
        )
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    base = path.parent

    def resolve(p: str) -> Path:
        q = Path(_expand(p)).expanduser()
        return q if q.is_absolute() else (base / q).resolve()

    local_raw = raw.get("local", {})
    local = LocalConfig(
        path=resolve(local_raw.get("path", "data/kat.sqlite")),
        batch_size=int(local_raw.get("batch_size", 500)),
        busy_timeout_s=float(local_raw.get("busy_timeout_s", 10.0)),
    )

    remote_raw = dict(raw.get("remote", {}))
    remote = RemoteConfig(
        kind=remote_raw.get("kind", "sqlite"),
        path=resolve(remote_raw["path"]) if remote_raw.get("path") else None,
        host=_expand(remote_raw.get("host", "192.168.40.100")),
        port=int(remote_raw.get("port", 5432)),
        database=_expand(remote_raw.get("database", "csit321")),
        user=_expand(remote_raw.get("user", "")),
        password=_expand(remote_raw.get("password", "")),
        sslmode=remote_raw.get("sslmode", "prefer"),
        connect_timeout_s=int(remote_raw.get("connect_timeout_s", 10)),
    )

    zmq_raw = raw.get("zmq", {})
    zmq_cfg = ZmqConfig(
        enabled=bool(zmq_raw.get("enabled", True)),
        endpoint=_expand(zmq_raw.get("endpoint", "tcp://127.0.0.1:5556")),
        topics=list(zmq_raw.get("topics", [""])),
        bind=bool(zmq_raw.get("bind", False)),
    )

    svc_raw = raw.get("service", {})
    return ServiceConfig(
        local=local,
        remote=remote,
        zmq=zmq_cfg,
        sweep_interval_s=float(svc_raw.get("sweep_interval_s", 30.0)),
        notify_debounce_s=float(svc_raw.get("notify_debounce_s", 0.5)),
        origin=svc_raw.get("origin", "fake"),
        src_host=_expand(svc_raw.get("src_host", socket.gethostname())),
        retry_base_s=float(svc_raw.get("retry_base_s", 2.0)),
        retry_max_s=float(svc_raw.get("retry_max_s", 60.0)),
        log_level=svc_raw.get("log_level", "INFO"),
    )
