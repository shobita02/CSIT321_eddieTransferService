"""Configuration loading.

Everything the service needs to know lives in one TOML file so that moving
from "laptop + local Docker PostgreSQL" to "laptop + Bored Owl dev server" is a config
change, not a code change.
"""

from __future__ import annotations

import os
import socket
import tomllib
from urllib.parse import urlsplit
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
    """HTTP gateway connection. The laptop has no database credentials."""

    url: str = "http://127.0.0.1:8080"
    token: str = field(default="", repr=False)
    timeout_s: float = 30.0
    ca_file: str | None = None

    def __post_init__(self) -> None:
        self.url = self.url.rstrip("/")
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("remote.url must be an HTTP or HTTPS gateway URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("remote.url cannot contain credentials, a query or fragment")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Use HTTPS for a gateway outside this machine")
        if not self.token or "${" in self.token:
            raise ValueError("Set KAT_API_WRITE_TOKEN before loading the client config")
        if self.timeout_s <= 0:
            raise ValueError("remote.timeout_s must be positive")


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
    if not 1 <= local.batch_size <= 1000:
        raise ValueError("local.batch_size must be between 1 and 1000")

    remote_raw = dict(raw.get("remote", {}))
    if remote_raw.get("kind", "gateway") != "gateway" or any(
        key in remote_raw for key in ("host", "port", "database", "user", "password", "sslmode")
    ):
        raise ValueError(
            "Direct PostgreSQL client configs are no longer supported. Use "
            "[remote] kind = 'gateway', url and token; database credentials belong on the server."
        )
    remote = RemoteConfig(
        url=_expand(remote_raw.get("url", "http://127.0.0.1:8080")),
        token=_expand(remote_raw.get("token", "${KAT_API_WRITE_TOKEN}")),
        timeout_s=float(remote_raw.get("timeout_s", 30)),
        ca_file=str(resolve(remote_raw["ca_file"])) if remote_raw.get("ca_file") else None,
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
