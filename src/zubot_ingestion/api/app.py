"""TRANSITIONAL FastAPI factory for the sharp-spark worktree.

This file exists ONLY because the sharp-spark worktree was branched from
the initial commit and does not contain the canonical ``app.py`` produced
by sibling task-4 (warm-cedar). It mirrors the canonical factory structure
so the sharp-spark unit-test suite can boot a working FastAPI application
end-to-end against the rate-limit middleware.

On merge with the canonical task-4 output, the following THREE additions
made by this task (step-24, CAP-030) MUST be ported into the canonical
``create_app()``:

1. ``app.state.limiter = limiter`` — attaches the slowapi limiter to the
   application state so per-route ``@limiter.limit(...)`` decorators can
   find it via ``request.app.state.limiter``.
2. ``app.add_exception_handler(RateLimitExceeded, custom_429_handler)`` —
   installs the custom JSON 429 response format defined in
   :mod:`zubot_ingestion.api.middleware.rate_limit`.
3. ``app.add_middleware(SlowAPIMiddleware)`` — registers the limiter as a
   Starlette middleware. Must be added AFTER AuthMiddleware so the
   rate-limit key function can observe ``request.state.auth_context``.

All three lines are grouped in a single "Rate limiting (CAP-030)" block
below to make the port trivial.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
from zubot_ingestion.shared.constants import SERVICE_NAME, SERVICE_VERSION


def create_app() -> FastAPI:
    """Construct and return the FastAPI application instance."""
    app = FastAPI(
        title="Zubot Ingestion Service",
        version=SERVICE_VERSION,
        description=f"{SERVICE_NAME} — construction-document ingestion pipeline.",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # CORS — wide-open for local development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------ #
    # Rate limiting (CAP-030) — port these three lines into the canonical
    # task-4 create_app() on merge.
    # ------------------------------------------------------------------ #
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, custom_429_handler)
    app.add_middleware(SlowAPIMiddleware)

    # Routers
    app.include_router(extract_routes.router)
    app.include_router(batches_routes.router)
    app.include_router(jobs_routes.router)
    app.include_router(review_routes.router)
    app.include_router(health_routes.router)
    app.include_router(metrics_routes.router)

    return app


__all__ = ["create_app"]
