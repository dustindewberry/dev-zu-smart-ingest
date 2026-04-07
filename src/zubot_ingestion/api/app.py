"""FastAPI application factory.

Implements CAP-001 (web application skeleton). This module deliberately
contains *only* the wiring needed to construct a runnable ``FastAPI``
instance:

* CORS middleware (open in development; locked down later via config)
* A lifespan context manager that logs startup / shutdown
* Placeholder global exception handlers (concrete custom exception classes
  will be registered here as later steps add domain-specific errors)
* OpenAPI metadata sourced from ``zubot_ingestion.shared.constants``

No routes are registered here. Each later step (health, extract, batches,
jobs, review, metrics) is responsible for mounting its own ``APIRouter``
on the instance returned by :func:`create_app`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from zubot_ingestion.shared.constants import SERVICE_NAME, SERVICE_VERSION

if TYPE_CHECKING:  # pragma: no cover - import only for type-checkers
    pass

logger = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup before ``yield``, shutdown after.

    For now this is a placeholder that simply logs the transitions. Later
    steps will attach concrete resource handles (database engine, Redis
    pool, Ollama HTTP client, OTEL provider) to ``app.state`` here.
    """
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
    """Catch-all 500 handler.

    Custom domain exceptions defined by later steps should register their
    own, more specific handlers; this exists so unanticipated errors never
    leak a stack trace to the client.
    """
    logger.exception(
        "unhandled_exception",
        extra={
            "path": request.url.path,
            "method": request.method,
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "message": "An unexpected error occurred.",
        },
    )


def create_app() -> FastAPI:
    """Construct and return the FastAPI application instance.

    This is a factory rather than a module-level singleton so that tests
    can build isolated app instances and so that the ASGI runner
    (uvicorn) can be configured via :mod:`zubot_ingestion.__main__`.
    """
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

    # CORS — wide-open for local development. A later step will tighten
    # this via the Settings object once route-level auth is in place.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Placeholder exception handler. Specific custom exceptions will be
    # registered alongside the routes that raise them in later steps.
    app.add_exception_handler(Exception, _unhandled_exception_handler)

    # NOTE: No routes are registered here. Routers are mounted by later
    # steps (health, extract, batches, jobs, review, metrics).

    return app
