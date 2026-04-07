"""Transitional FastAPI application factory for the sharp-owl worktree.

This file exists ONLY because the canonical task-4 (warm-cedar) ``app.py``
is not present in this worktree's branch. It mirrors the canonical factory's
structure (CORS, lifespan, generic 500 handler, OpenAPI metadata sourced
from :mod:`zubot_ingestion.shared.constants`) and additionally wires the
:class:`AuthMiddleware` produced by THIS task (step-10) into the middleware
stack via ``app.add_middleware``.

On merge with the canonical task-4 output, the canonical factory must absorb
the single ``app.add_middleware(AuthMiddleware)`` line below. The rest of
this stub is byte-equivalent to the canonical version and may be discarded
in favour of warm-cedar's copy.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from zubot_ingestion.api.middleware.auth import AuthMiddleware
from zubot_ingestion.shared.constants import SERVICE_NAME, SERVICE_VERSION

logger = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup before ``yield``, shutdown after."""
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
    """Catch-all 500 handler so unanticipated errors never leak a stack."""
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

    # CORS — wide-open for local development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Authentication — CAP-026, implemented in this task (step-10). Must be
    # registered AFTER CORS so that pre-flight OPTIONS requests are handled
    # by CORSMiddleware before AuthMiddleware sees them.
    app.add_middleware(AuthMiddleware)

    app.add_exception_handler(Exception, _unhandled_exception_handler)

    return app


__all__ = ["create_app", "lifespan"]
