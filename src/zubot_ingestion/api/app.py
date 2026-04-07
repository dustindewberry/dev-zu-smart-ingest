"""FastAPI application factory — minimal stub for the witty-atlas worktree.

This worktree was branched from the initial commit and does not see the
canonical create_app() factory produced by task-4 (warm-cedar). The
canonical version is a strict superset of this stub (it adds CORS,
exception handlers, lifespan, OpenAPI metadata, and registers all the
sibling routers). On merge, the canonical task-4 ``create_app()`` body
must be taken as the base; THIS file's only behavioural contribution is
the single line ``app.include_router(metrics_router.router)`` — that
line MUST be ported into the canonical factory immediately after the
other ``include_router(...)`` calls.

The reason this stub exists at all is to make
``tests/unit/test_metrics_route.py`` runnable in isolation. Tests
construct a real FastAPI app via ``create_app()`` and use Starlette's
``TestClient`` to verify the ``/metrics`` route is registered and
returns the correct content-type and body.
"""

from __future__ import annotations

from fastapi import FastAPI

from zubot_ingestion.api.routes import metrics as metrics_router
from zubot_ingestion.shared.constants import SERVICE_NAME, SERVICE_VERSION

__all__ = ["create_app"]


def create_app() -> FastAPI:
    """Build a minimal FastAPI app with the /metrics route wired in."""
    app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION)
    app.include_router(metrics_router.router)
    return app
