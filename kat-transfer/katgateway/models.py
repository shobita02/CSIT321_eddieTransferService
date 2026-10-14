"""Bounded, validated ingestion contracts matching the existing archive."""

import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

SystemId = Annotated[str, Field(min_length=1, max_length=20)]
PgInt = Annotated[int, Field(strict=True, ge=-2147483648, le=2147483647)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Experiment(Model):
    systemid: SystemId
    run: PgInt
    description: str | None = Field(default=None, max_length=100_000)


class PayloadRow(Model):
    systemid: SystemId
    ts: AwareDatetime | None
    data: str | None = Field(max_length=1_000_000)

    @field_validator("data")
    @classmethod
    def valid_json(cls, value):
        if value is not None:
            def reject_constant(_):
                raise ValueError("Non-finite JSON numbers are not supported")
            try:
                json.loads(value, parse_constant=reject_constant)
            except (ValueError, RecursionError) as exc:
                raise ValueError("data must contain valid JSON text") from exc
        return value


class Experimental(PayloadRow):
    run: PgInt
    row_no: PgInt
    source: str | None = Field(default=None, max_length=20)


class Environment(PayloadRow):
    ts: AwareDatetime


class Batch(Model):
    origin: Literal["real", "fake"]
    src_host: str = Field(min_length=1, max_length=64)
    started_at: AwareDatetime
    trigger: Literal["manual", "startup", "sweep", "zmq"] = "manual"


class ExperimentBatch(Batch):
    rows: list[Experiment] = Field(min_length=1, max_length=1000)


class ExperimentalBatch(Batch):
    xfer: AwareDatetime
    rows: list[Experimental] = Field(min_length=1, max_length=1000)


class EnvironmentBatch(Batch):
    xfer: AwareDatetime
    rows: list[Environment] = Field(min_length=1, max_length=1000)


class Receipt(Model):
    committed: Literal[True] = True
    sent: int
    applied: int
