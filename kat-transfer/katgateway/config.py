"""Server settings; database credentials never enter the transfer client."""

from dataclasses import dataclass, field, replace
import os


@dataclass
class DatabaseConfig:
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "csit321"
    user: str = "kat"
    password: str = field(default="", repr=False)
    sslmode: str = "prefer"
    connect_timeout_s: int = 5


@dataclass
class Settings:
    database: DatabaseConfig
    # Separate login for sign-in token issuing; None disables /v1/auth/login.
    issuer: DatabaseConfig | None = None
    # Local demo only: accept sign-in over plain HTTP from the loopback NGINX.
    allow_http_login: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        def required(name):
            value = os.environ.get(name, "")
            if not value or "${" in value:
                raise ValueError(f"Set {name} on the API server")
            return value

        database = DatabaseConfig(
            host=os.environ.get("KAT_PG_HOST", "127.0.0.1"),
            port=int(os.environ.get("KAT_PG_PORT", "5432")),
            database=required("KAT_PG_DATABASE"), user=required("KAT_PG_USER"),
            password=required("KAT_PG_PASSWORD"),
            sslmode=os.environ.get("KAT_PG_SSLMODE", "prefer"),
        )
        issuer = None
        if os.environ.get("KAT_ISSUER_PG_USER"):
            issuer = replace(database, user=os.environ["KAT_ISSUER_PG_USER"],
                             password=required("KAT_ISSUER_PG_PASSWORD"))
        return cls(database=database, issuer=issuer,
                   allow_http_login=os.environ.get("KAT_AUTH_ALLOW_HTTP") == "1")
