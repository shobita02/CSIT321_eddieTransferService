"""Authenticated ingestion gateway and paginated researcher API."""

import logging
import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime
import psycopg2

from .config import Settings
from .models import EnvironmentBatch, ExperimentBatch, ExperimentalBatch, Receipt
from .storage import ArchiveStore
from .limits import BodyLimit

log = logging.getLogger("katgateway")
Limit = Annotated[int, Query(ge=1, le=1000)]
Offset = Annotated[int, Query(ge=0)]
RigFilter = Annotated[str | None, Query(min_length=1, max_length=20)]
OriginFilter = Literal["real", "fake"] | None


def create_app(settings: Settings | None = None, store=None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store if store is not None else ArchiveStore(settings.database)
    app = FastAPI(title="CSIT321 KAT Gateway and API", version="1.0.0",
                  description="Authenticated KAT ingestion and archive reads. Data is acknowledged only after commit.")
    app.state.store = store
    app.add_middleware(BodyLimit)
    security = HTTPBearer(auto_error=False)

    def role(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)]):
        token = credentials.credentials if credentials else ""
        if secrets.compare_digest(token.encode(), settings.write_token.encode()):
            return "write"
        if secrets.compare_digest(token.encode(), settings.read_token.encode()):
            return "read"
        raise HTTPException(401, "A valid API bearer token is required", headers={"WWW-Authenticate": "Bearer"})

    def write_access(access: Annotated[str, Depends(role)]):
        if access != "write":
            raise HTTPException(403, "This token has read access only")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # FastAPI's default error includes input values. Never reflect scientific
        # payloads or an arbitrary request body in error responses.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": error["loc"], "type": error["type"], "msg": error["msg"]} for error in exc.errors()
        ]})

    @app.exception_handler(psycopg2.Error)
    async def database_error(request, exc):
        if isinstance(exc, psycopg2.IntegrityError):
            code, message = 409, "Archive constraint failed; check parent experiments and row keys"
        elif isinstance(exc, psycopg2.DataError):
            code, message = 422, "Archive rejected a field value"
        else:
            code, message = 503, "Archive temporarily unavailable; retry the unconfirmed batch"
        # Exception text may contain SQL, credentials or submitted data.
        log.warning("Archive request failed (%s)", type(exc).__name__)
        return JSONResponse(status_code=code, content={"detail": message})

    @app.get("/health", tags=["health"])
    def health():
        return {"status": "ok"}

    reads = APIRouter(prefix="/v1", dependencies=[Depends(role)])
    writes = APIRouter(prefix="/v1/ingest", dependencies=[Depends(write_access)])

    @reads.get("/status", tags=["archive"])
    def status():
        return {"counts": store.counts()}

    @writes.post("/experiments", response_model=Receipt, tags=["ingest"])
    def ingest_experiments(batch: ExperimentBatch):
        return store.write_batch("experiment", batch)

    @writes.post("/experimental-data", response_model=Receipt, tags=["ingest"])
    def ingest_experimental(batch: ExperimentalBatch):
        return store.write_batch("experimental_data", batch)

    @writes.post("/environment", response_model=Receipt, tags=["ingest"])
    def ingest_environment(batch: EnvironmentBatch):
        return store.write_batch("environment", batch)

    def page(table, limit, offset, **filters):
        return {"items": store.records(table, limit=limit, offset=offset, **filters),
                "limit": limit, "offset": offset}

    @reads.get("/experiments", tags=["archive"])
    def experiments(limit: Limit = 100, offset: Offset = 0,
                    systemid: RigFilter = None, origin: OriginFilter = None):
        return page("experiment", limit, offset, systemid=systemid, origin=origin)

    @reads.get("/experiments/{systemid}/{run}", tags=["archive"])
    def experiment(systemid: str, run: int):
        rows = store.records("experiment", limit=1, systemid=systemid, run=run)
        if not rows:
            raise HTTPException(404, "Experiment not found")
        return rows[0]

    @reads.get("/experimental-data", tags=["archive"])
    def experimental_data(limit: Limit = 100, offset: Offset = 0,
                          systemid: RigFilter = None, run: int | None = None,
                          origin: OriginFilter = None):
        return page("experimental_data", limit, offset, systemid=systemid, run=run, origin=origin)

    @reads.get("/environment", tags=["archive"])
    def environment(limit: Limit = 100, offset: Offset = 0,
                    systemid: RigFilter = None, origin: OriginFilter = None,
                    from_ts: AwareDatetime | None = None, to_ts: AwareDatetime | None = None):
        if from_ts is not None and to_ts is not None and from_ts > to_ts:
            raise HTTPException(422, "from_ts must be no later than to_ts")
        return page("environment", limit, offset, systemid=systemid, origin=origin,
                    from_ts=from_ts, to_ts=to_ts)

    @reads.get("/transfers", tags=["archive"])
    def transfers(limit: Limit = 100, offset: Offset = 0):
        return page("transfer_log", limit, offset)

    app.include_router(reads)
    app.include_router(writes)
    return app
