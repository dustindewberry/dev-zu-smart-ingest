"""FastAPI application factory with structured-logging integration (CAP-029).

NOTE: This file is created in this worktree as part of task-25 (structured
logging) because the canonical version produced by task-4 is not present
on this branch. On merge, the canonical task-4 file should be taken as the
base and the three logging-specific additions in this file should be
ported over:

1. ``setup_logging(settings.LOG_LEVEL, settings.LOG_DIR)`` in lifespan
2. ``app.add_middleware(RequestLoggingMiddleware)``
3. Exception handlers that log via ``get_logger(__name__).exception(...)``
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from zubot_ingestion.api.middleware.logging import RequestLoggingMiddleware
from zubot_ingestion.config import Settings, get_settings
from zubot_ingestion.infrastructure.logging.config import (
    get_logger,
    setup_logging,
)
from zubot_ingestion.shared.constants import SERVICE_NAME, SERVICE_VERSION

_logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: configure logging on startup, no-op on shutdown."""
    settings = get_settings()
    setup_logging(log_level=settings.LOG_LEVEL, log_dir=settings.LOG_DIR)
    _logger.info(
        "service.startup",
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
        log_level=settings.LOG_LEVEL,
    )
    try:
        yield
    finally:
        _logger.info("service.shutdown", service=SERVICE_NAME)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title=SERVICE_NAME,
        version=SERVICE_VERSION,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestLoggingMiddleware)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        # Use structlog so the unhandled error inherits whatever request_id
        # the RequestLoggingMiddleware bound for this request.
        _logger.exception(
            "unhandled_exception",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error"},
        )

    return app
