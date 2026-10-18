"""Authenticated ingestion gateway and paginated researcher API."""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime
import psycopg2

from .config import Settings
from .limits import BodyLimit
from .models import (EnvironmentBatch, ExperimentBatch, ExperimentalBatch, LoginRequest, LoginResponse,
                     Receipt, TokenIdentity)
from .passwords import verify_password
from .ratelimit import FailedLoginLimit
from .storage import ArchiveStore

log = logging.getLogger("katgateway")
Limit = Annotated[int, Query(ge=1, le=1000)]
Offset = Annotated[int, Query(ge=0)]
RigFilter = Annotated[str | None, Query(min_length=1, max_length=20)]
OriginFilter = Literal["real", "fake"] | None


@dataclass(frozen=True)
class Access:
    token_id: int
    username: str
    role: Literal["read", "write"]
    expires_at: datetime | None = None


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def create_app(settings: Settings | None = None, store=None, issuer_store=None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store if store is not None else ArchiveStore(settings.database)
    if issuer_store is None and settings.issuer is not None:
        issuer_store = ArchiveStore(settings.issuer)
    login_limit = FailedLoginLimit()
    app = FastAPI(title="CSIT321 KAT Gateway and API", version="1.0.0",
                  description="Authenticated KAT ingestion and archive reads. Data is acknowledged only after commit.")
    app.state.store = store
    app.add_middleware(BodyLimit)
    security = HTTPBearer(auto_error=False)

    def role(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)]):
        token = credentials.credentials if credentials else ""
        identity = store.find_token(hashlib.sha256(token.encode()).hexdigest()) if token else None
        if identity is None:
            raise HTTPException(401, "A valid API bearer token is required", headers={"WWW-Authenticate": "Bearer"})
        token_id, username, token_role, max_role, expires_at = identity
        effective_role: Literal["read", "write"] = (
            "write" if token_role == max_role == "write" else "read"
        )
        return Access(token_id=token_id, username=username, role=effective_role, expires_at=expires_at)

    def write_access(access: Annotated[Access, Depends(role)]):
        if access.role != "write":
            raise HTTPException(403, "This token has read access only")
        return access

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

    reads = APIRouter(prefix="/v1")
    writes = APIRouter(prefix="/v1/ingest", dependencies=[Depends(write_access)])

    @reads.get("/status", tags=["archive"], dependencies=[Depends(role)])
    def status():
        return {"counts": store.counts()}

    @writes.post("/experiments", response_model=Receipt, tags=["ingest"])
    def ingest_experiments(batch: ExperimentBatch, access: Annotated[Access, Depends(write_access)]):
        return store.write_batch("experiment", batch, access.token_id)

    @writes.post("/experimental-data", response_model=Receipt, tags=["ingest"])
    def ingest_experimental(batch: ExperimentalBatch, access: Annotated[Access, Depends(write_access)]):
        return store.write_batch("experimental_data", batch, access.token_id)

    @writes.post("/environment", response_model=Receipt, tags=["ingest"])
    def ingest_environment(batch: EnvironmentBatch, access: Annotated[Access, Depends(write_access)]):
        return store.write_batch("environment", batch, access.token_id)

    def page(table, limit, offset, **filters):
        return {"items": store.records(table, limit=limit, offset=offset, **filters),
                "limit": limit, "offset": offset}

    @reads.get("/experiments", tags=["archive"], dependencies=[Depends(role)])
    def experiments(limit: Limit = 100, offset: Offset = 0,
                    systemid: RigFilter = None, origin: OriginFilter = None):
        return page("experiment", limit, offset, systemid=systemid, origin=origin)

    @reads.get("/experiments/{systemid}/{run}", tags=["archive"], dependencies=[Depends(role)])
    def experiment(systemid: str, run: int):
        rows = store.records("experiment", limit=1, systemid=systemid, run=run)
        if not rows:
            raise HTTPException(404, "Experiment not found")
        return rows[0]

    @reads.get("/experimental-data", tags=["archive"], dependencies=[Depends(role)])
    def experimental_data(limit: Limit = 100, offset: Offset = 0,
                          systemid: RigFilter = None, run: int | None = None,
                          origin: OriginFilter = None):
        return page("experimental_data", limit, offset, systemid=systemid, run=run, origin=origin)

    @reads.get("/environment", tags=["archive"], dependencies=[Depends(role)])
    def environment(limit: Limit = 100, offset: Offset = 0,
                    systemid: RigFilter = None, origin: OriginFilter = None,
                    from_ts: AwareDatetime | None = None, to_ts: AwareDatetime | None = None):
        if from_ts is not None and to_ts is not None and from_ts > to_ts:
            raise HTTPException(422, "from_ts must be no later than to_ts")
        return page("environment", limit, offset, systemid=systemid, origin=origin,
                    from_ts=from_ts, to_ts=to_ts)

    @reads.get("/transfers", tags=["archive"], dependencies=[Depends(role)])
    def transfers(limit: Limit = 100, offset: Offset = 0):
        return page("transfer_log", limit, offset)

    auth = APIRouter(prefix="/v1/auth", tags=["auth"])

    def issuer():
        if issuer_store is None:
            raise HTTPException(503, "Sign-in is not enabled on this server")
        return issuer_store

    def require_https(request: Request):
        forwarded = request.headers.get("x-forwarded-proto")
        if forwarded is not None:
            secure = forwarded.split(",")[0].strip().lower() == "https"
        else:
            secure = request.url.scheme == "https" or (request.client and request.client.host in LOOPBACK)
        if not secure and not settings.allow_http_login:
            raise HTTPException(403, "Sign-in requires HTTPS")

    @auth.post("/login", response_model=LoginResponse, dependencies=[Depends(require_https)])
    def login(body: LoginRequest, response: Response):
        tokens = issuer()
        wait = login_limit.retry_after(body.username)
        if wait:
            raise HTTPException(429, "Too many failed sign-in attempts; try again later",
                                headers={"Retry-After": str(wait)})
        account = tokens.user_credentials(body.username)
        password_hash, max_role, active = account if account else (None, None, False)
        # Always run the hash check so unknown users take as long as wrong passwords.
        if not verify_password(body.password, password_hash) or not active:
            login_limit.failed(body.username)
            raise HTTPException(401, "Incorrect username or password")
        login_limit.succeeded(body.username)
        if body.role == "write" and max_role != "write":
            raise HTTPException(403, "Your account can view but not send data")
        try:
            token_id, token, expires_at = tokens.create_token(body.username, body.role, body.expires_days, "sign-in")
        except ValueError:
            # The user was disabled or lowered between the check and the insert.
            raise HTTPException(403, "Your account is not permitted to hold this role") from None
        response.headers["Cache-Control"] = "no-store"
        log.info("Issued %s token %s to %s via sign-in", body.role, token_id, body.username)
        return LoginResponse(token=token, token_id=token_id, username=body.username,
                             role=body.role, expires_at=expires_at)

    @auth.get("/me", response_model=TokenIdentity)
    def me(access: Annotated[Access, Depends(role)]):
        return TokenIdentity(token_id=access.token_id, username=access.username,
                             role=access.role, expires_at=access.expires_at)

    @auth.post("/logout", status_code=204)
    def logout(access: Annotated[Access, Depends(role)]):
        issuer().revoke_token_by_id(access.token_id)
        return Response(status_code=204)

    app.include_router(reads)
    app.include_router(writes)
    app.include_router(auth)
    return app
