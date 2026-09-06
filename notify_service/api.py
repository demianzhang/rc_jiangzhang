import logging
import secrets
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from .config import Settings
from .middleware import RequestSizeLimit
from .models import (
    NotificationDetail, NotificationRequest, NotificationView, Status, SubmissionResponse,
)
from .policy import normalize_url
from .store import ConflictError, NotFoundError, Store


logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.initialize()
        yield

    app = FastAPI(title="Reliable HTTP Notifications", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestSizeLimit)
    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authenticate(key: Annotated[str | None, Depends(api_key_header)]) -> None:
        if key is None or not secrets.compare_digest(key.encode(), settings.api_key.encode()):
            raise HTTPException(status_code=401, detail="Invalid API key")

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error_handler(request: Request, exc: sqlite3.OperationalError):
        logger.error("database_unavailable error_type=%s", type(exc).__name__)
        return JSONResponse(
            status_code=503, content={"detail": "Database unavailable; retry with the same Idempotency-Key"},
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # FastAPI's default validation response echoes inputs, including supplier secrets.
        errors = [
            {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.get("/healthz")
    def health():
        store.health()
        return {"status": "ok"}

    @app.post(
        "/v1/notifications", status_code=202, response_model=SubmissionResponse,
        dependencies=[Depends(authenticate)],
    )
    def submit(
        payload: NotificationRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]+$"),
        ],
    ):
        if normalize_url(payload.url)[1] not in settings.allowed_origins:
            raise HTTPException(status_code=422, detail="Destination origin is not allowed")
        notification_id, status, duplicate = store.enqueue(
            idempotency_key, payload, settings.max_attempts,
        )
        logger.info("notification_accepted id=%s deduplicated=%s", notification_id, duplicate)
        return SubmissionResponse(id=notification_id, status=status, deduplicated=duplicate)

    @app.get(
        "/v1/notifications", response_model=list[NotificationView],
        dependencies=[Depends(authenticate)],
    )
    def list_notifications(
        status: Status | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ):
        return store.list(status, limit)

    @app.get(
        "/v1/notifications/{notification_id}", response_model=NotificationDetail,
        dependencies=[Depends(authenticate)],
    )
    def detail(notification_id: str):
        return store.get(notification_id)

    @app.post(
        "/v1/notifications/{notification_id}/redrive", status_code=202,
        response_model=NotificationDetail, dependencies=[Depends(authenticate)],
    )
    def redrive(notification_id: str):
        store.redrive(notification_id)
        logger.warning("notification_redriven id=%s", notification_id)
        return store.get(notification_id)

    return app
