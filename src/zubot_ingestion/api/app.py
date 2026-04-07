"""FastAPI application factory.

Implements CAP-001 (web application skeleton) and wires together the
middleware and routers produced by sibling steps:

* CORS middleware (open in development; locked down later via config)
* Authentication middleware (CAP-026; enforces API key / WOD JWT)
* A lifespan context manager that logs startup / shutdown
* Placeholder global exception handlers (concrete custom exception classes
  will be registered here as later steps add domain-specific errors)
* OpenAPI metadata sourced from ``zubot_ingestion.shared.constants``
* Routers mounted from each step's router module (extract, health,
  batches, jobs, review, metrics)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from zubot_ingestion.api.middleware.auth import AuthMiddleware
from zubot_ingestion.api.routes import batches as batches_routes
from zubot_ingestion.api.routes import extract as extract_routes
from zubot_ingestion.api.routes import health as health_routes
from zubot_ingestion.api.routes import jobs as jobs_routes
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

    # Authentication — CAP-026, implemented in step-10 (sharp-owl). Must be
    # registered AFTER CORS so that pre-flight OPTIONS requests are handled
    # by CORSMiddleware before AuthMiddleware sees them.
    app.add_middleware(AuthMiddleware)

    # Placeholder exception handler. Specific custom exceptions will be
    # registered alongside the routes that raise them in later steps.
    app.add_exception_handler(Exception, _unhandled_exception_handler)

    # Routers
    # - CAP-009 (POST /extract)
    # - CAP-010 (GET /batches/{batch_id})
    # - CAP-011 (GET /jobs/{job_id})
    # - CAP-003 (GET /health) — step-13 (keen-hare)
    # Future steps mount additional routers (review, metrics) here.
    app.include_router(extract_routes.router)
    app.include_router(batches_routes.router)
    app.include_router(jobs_routes.router)
    app.include_router(health_routes.router)

    return app


__all__ = ["create_app", "lifespan"]
