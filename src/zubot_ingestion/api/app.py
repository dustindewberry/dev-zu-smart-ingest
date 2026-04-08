"""FastAPI application factory.

Implements CAP-001 (web application skeleton) and wires together the
middleware and routers produced by sibling steps:

* CORS middleware (open in development; locked down later via config)
* Authentication middleware (CAP-026; enforces API key / WOD JWT)
* Rate limiting middleware (CAP-030; slowapi + Redis)
* Request logging middleware (CAP-029; structured JSON via structlog)
* A lifespan context manager that configures structured logging on
  startup and logs startup / shutdown
* Placeholder global exception handlers (concrete custom exception classes
  will be registered here as later steps add domain-specific errors)
* OpenAPI metadata sourced from ``zubot_ingestion.shared.constants``
* Routers mounted from each step's router module (extract, health,
  batches, jobs, review, metrics)
* OTEL tracer provider initialized on startup via ``setup_otel`` (CAP-027)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from zubot_ingestion.api.middleware.auth import AuthMiddleware
from zubot_ingestion.api.middleware.logging import RequestLoggingMiddleware
from zubot_ingestion.api.middleware.rate_limit import (
    RateLimitExceeded,
    SlowAPIMiddleware,
    custom_429_handler,
    limiter,
)
from zubot_ingestion.api.routes import batches as batches_routes
from zubot_ingestion.api.routes import extract as extract_routes
from zubot_ingestion.api.routes import health as health_routes
from zubot_ingestion.api.routes import jobs as jobs_routes
from zubot_ingestion.api.routes import metrics as metrics_routes
from zubot_ingestion.api.routes import review as review_routes
from zubot_ingestion.config import get_settings
from zubot_ingestion.infrastructure.logging.config import (
    get_logger,
    setup_logging,
)
from zubot_ingestion.infrastructure.otel.instrumentation import setup_otel
from zubot_ingestion.shared.constants import SERVICE_NAME, SERVICE_VERSION

if TYPE_CHECKING:  # pragma: no cover - import only for type-checkers
    pass

logger = get_logger(SERVICE_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup before ``yield``, shutdown after.

    Configures structured logging (CAP-029) and initializes OTEL tracing
    on startup, then logs the transitions. Later steps will attach
    concrete resource handles (database engine, Redis pool, Ollama HTTP
    client) to ``app.state`` here.
    """
    settings = get_settings()
    setup_logging(log_level=settings.LOG_LEVEL, log_dir=settings.LOG_DIR)
    setup_otel(otlp_endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT)
    logger.info(
        "service.startup",
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
        log_level=settings.LOG_LEVEL,
    )
    try:
        yield
    finally:
        logger.info(
            "service.shutdown",
            service=SERVICE_NAME,
            version=SERVICE_VERSION,
        )


async def _unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Catch-all 500 handler.

    Custom domain exceptions defined by later steps should register their
    own, more specific handlers; this exists so unanticipated errors never
    leak a stack trace to the client. Logs via structlog so the unhandled
    error inherits whatever request_id the RequestLoggingMiddleware bound
    for this request.
    """
    logger.exception(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error_type=type(exc).__name__,
        error_message=str(exc),
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
    settings = get_settings()
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
    # Store settings on app.state so route handlers (and tests) can access
    # the same singleton.
    app.state.settings = settings

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

    # Request logging (CAP-029) — bind a request_id, log start/end, and
    # clear context on completion. Registered after AuthMiddleware so the
    # bound request context can observe ``request.state.user_id`` if auth
    # populated it.
    app.add_middleware(RequestLoggingMiddleware)

    # ------------------------------------------------------------------ #
    # Rate limiting (CAP-030) — slowapi + Redis. Must be added AFTER
    # AuthMiddleware so the rate-limit key function can observe
    # ``request.state.auth_context``.
    # ------------------------------------------------------------------ #
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, custom_429_handler)
    app.add_middleware(SlowAPIMiddleware)

    # Placeholder exception handler. Specific custom exceptions will be
    # registered alongside the routes that raise them in later steps.
    app.add_exception_handler(Exception, _unhandled_exception_handler)

    # Routers
    # - CAP-009 (POST /extract)
    # - CAP-010 (GET /batches/{batch_id})
    # - CAP-011 (GET /jobs/{job_id})
    # - CAP-003 (GET /health) — step-13 (keen-hare)
    # - CAP-028 (GET /metrics) — step-23 (witty-atlas)
    # - CAP-030 (POST /review/...) — step-24 (sharp-spark)
    app.include_router(extract_routes.router)
    app.include_router(batches_routes.router)
    app.include_router(jobs_routes.router)
    app.include_router(review_routes.router)
    app.include_router(health_routes.router)
    app.include_router(metrics_routes.router)

    return app


__all__ = ["create_app", "lifespan"]
