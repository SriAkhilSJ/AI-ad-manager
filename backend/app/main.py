"""FastAPI application factory.

Responsibilities:
* validate configuration (refuse unsafe production settings),
* configure structured logging with secret redaction,
* install the structured error envelope for every failure mode,
* assign a request id + emit an access log line per request,
* bootstrap the founder account on an empty database (development),
* expose ``/api/v1/system`` (health + integration status) and the versioned API.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.router import api_router
from app.api.v1.system import router as system_router
from app.core.config import Settings, get_settings
from app.core.errors import (
    AppError,
    app_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import get_rate_limiter
from app.core.security import Role, hash_password
from app.db.session import dispose_engine, get_session_factory
from app.models.user import User

logger = get_logger("crusor.api")


async def _bootstrap_founder(settings: Settings) -> None:
    """Create the founder account if the user table is empty (dev convenience).

    Production requires an operator to create the first user explicitly; the
    function is still safe there because it only acts on an empty database.
    """
    factory = get_session_factory()
    async with factory() as session:
        count = int((await session.execute(select(func.count()).select_from(User))).scalar_one())
        if count > 0:
            return
        if settings.is_production and settings.bootstrap_founder_password in {
            "change-me-dev-password",
            "",
        }:
            logger.warning("founder_bootstrap_skipped", reason="placeholder password in production")
            return
        user = User(
            email=settings.bootstrap_founder_email.lower(),
            full_name="Founder",
            hashed_password=hash_password(settings.bootstrap_founder_password),
            role=Role.FOUNDER.value,
        )
        session.add(user)
        await session.commit()
        logger.info("founder_bootstrapped", email=user.email)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.validate_for_environment()
    configure_logging(
        level="DEBUG" if settings.debug else "INFO", json_logs=settings.is_production
    )
    logger.info(
        "app_starting",
        environment=settings.environment,
        database="postgresql" if settings.database_url.startswith("postgres") else "sqlite",
    )
    try:
        await _bootstrap_founder(settings)
    except Exception:  # noqa: BLE001 - the API must still start to report status
        logger.exception("founder_bootstrap_failed")
    yield
    await dispose_engine()
    logger.info("app_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    docs_enabled = not settings.is_production or settings.debug

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0-phase1",
        description=(
            "Autonomous AI Marketing Director for Crusor AI. Phase 1 exposes the "
            "brand knowledge base, campaign/content versioning, the approval state "
            "machine and a LangGraph director with human-in-the-loop approval. "
            "Publishing is intentionally disabled until an approved platform "
            "transport is configured."
        ),
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-Id"],
        expose_headers=["X-Request-Id", "Idempotency-Replayed"],
    )

    # ------------------------------------------------------------- middleware
    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Response-Time-Ms"] = str(duration_ms)
        # Never log query strings (they can contain tokens) or headers.
        logger.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
        return response

    # --------------------------------------------------------- error handlers
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # ------------------------------------------------------------------ routes
    app.include_router(system_router, prefix=settings.api_v1_prefix)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return JSONResponse(
            {
                "name": settings.app_name,
                "version": "1.0.0-phase1",
                "api": settings.api_v1_prefix,
                "health": f"{settings.api_v1_prefix}/system/health",
                "status": f"{settings.api_v1_prefix}/system/status",
                "mode": "human_approval_required",
            }
        )

    @app.get(f"{settings.api_v1_prefix}/", include_in_schema=False)
    async def api_index() -> dict[str, object]:
        return {
            "version": "v1",
            "resources": [
                "auth", "brand", "campaigns", "content", "creative",
                "director", "agents", "social", "analytics", "usage", "system",
            ],
        }

    return app


app = create_app()


def reset_rate_limiter_for_tests() -> None:
    get_rate_limiter().reset()
