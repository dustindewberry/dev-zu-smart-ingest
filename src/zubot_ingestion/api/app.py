"""FastAPI application factory.

NOTE: This is a copy of the canonical task-4 (warm-cedar) ``create_app``
factory with the addition of mounting the health router from this task
(step-13). On merge:
  - The canonical create_app() body from task-4 should be taken as the
    base.
  - The single line ``app.include_router(health.router)`` (and the
    matching import) below must be ported into the canonical factory.
No other behavioural drift is introduced.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from zubot_ingestion.api.routes import health
from zubot_ingestion.shared.constants import SERVICE_NAME, SERVICE_VERSION

logger = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup before yield, shutdown after."""
    logger.info(
        "service.startup",
        extra={"service": SERVICE_NAME, "version": SERVICE_VERSION},
    )
    try:
        yield
    finally:
        logger.info(
            "service.shutdown",
            extra={"service": SERVICE_NAME, "version": SERVICE_VERSION},
        )


async def _unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Catch-all 500 handler."""
    logger.exception(
        "unhandled_exception",
        extra={"path": request.url.path, "method": request.method},
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "message": "An unexpected error occurred.",
        },
    )


def create_app() -> FastAPI:
    """Construct and return the FastAPI application instance."""
    app = FastAPI(
        title="Zubot Ingestion Service",
        version=SERVICE_VERSION,
        description=(
            "Construction-document ingestion pipeline: PDF intake, "
            "metadata extraction, companion generation, and publication "
            "to ChromaDB / Elasticsearch."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_exception_handler(Exception, _unhandled_exception_handler)

    # Mount route modules. Each later step adds its own router here.
    # ── step-13 (CAP-003) ────────────────────────────────────────────
    app.include_router(health.router)

    return app
