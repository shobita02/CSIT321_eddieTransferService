"""Server settings; database credentials never enter the transfer client."""

from dataclasses import dataclass, field
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
    write_token: str = field(repr=False)
    read_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if any(len(token) < 32 or "${" in token for token in (self.write_token, self.read_token)):
            raise ValueError("Set distinct KAT_API_WRITE_TOKEN and KAT_API_READ_TOKEN (at least 32 characters)")
        if self.write_token == self.read_token:
            raise ValueError("Read and write tokens must be different")

    @classmethod
    def from_env(cls) -> "Settings":
        def required(name):
            value = os.environ.get(name, "")
            if not value or "${" in value:
                raise ValueError(f"Set {name} on the API server")
            return value

        return cls(
            database=DatabaseConfig(
                host=os.environ.get("KAT_PG_HOST", "127.0.0.1"),
                port=int(os.environ.get("KAT_PG_PORT", "5432")),
                database=required("KAT_PG_DATABASE"), user=required("KAT_PG_USER"),
                password=required("KAT_PG_PASSWORD"),
                sslmode=os.environ.get("KAT_PG_SSLMODE", "prefer"),
            ),
            write_token=required("KAT_API_WRITE_TOKEN"),
            read_token=required("KAT_API_READ_TOKEN"),
        )
