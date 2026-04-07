"""Minimal stub of zubot_ingestion.api.app for the OTEL task.

TRANSITIONAL stub created by silver-falcon (task-22 / step-22). The canonical
full FastAPI application factory is produced by task-4 (warm-cedar worktree,
commit 03626b1) and extended by tasks 10 (auth middleware), 11 (extract
route), 12 (status endpoints), 13 (health), 20 (review), 23 (metrics), 24
(rate limit), and 25 (structured logging).

ON MERGE:
  - Take the canonical task-4 (and downstream sibling) create_app() factory
    as the base.
  - Port the ONE OTEL-specific addition from this file: the lifespan handler
    must call `setup_otel(otlp_endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT)`
    on startup, BEFORE any other startup hooks (so that auto-instrumentors
    can wrap the FastAPI app the moment it is constructed).
  - The setup_otel() call is idempotent (TracerProvider replacement is
    permitted by the OTEL SDK and our wrapper guards against duplicate
    instrumentation calls), so it is safe to invoke from a lifespan handler
    that may run multiple times in test scenarios.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from zubot_ingestion.config import get_settings
from zubot_ingestion.infrastructure.otel.instrumentation import setup_otel


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan handler — wires up OTEL on startup."""
    settings = get_settings()
    setup_otel(otlp_endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT)
    yield
    # Shutdown hooks (none for OTEL — the BatchSpanProcessor is flushed
    # automatically when the TracerProvider is replaced).


def create_app() -> FastAPI:
    """Build the FastAPI application with the OTEL lifespan hook attached."""
    settings = get_settings()
    app = FastAPI(
        title="Zubot Smart Ingestion Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Store settings on app.state so route handlers (and tests) can access
    # the same singleton.
    app.state.settings = settings
    return app


__all__ = ["create_app", "lifespan"]
