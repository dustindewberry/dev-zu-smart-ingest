"""Transitional constants stub for the sharp-owl worktree (task step-10).

This file exists ONLY because the canonical task-26 (witty-maple) constants
module is not present in this worktree's branch. It contains the minimal set
of symbols needed by the auth middleware in this task — namely
``AUTH_EXEMPT_PATHS`` and the ``SERVICE_NAME`` / ``SERVICE_VERSION``
identifiers used by the FastAPI application factory.

On merge with the canonical task-26 output (witty-maple commit 50fe47a), the
canonical, full-featured constants module is a strict superset of this stub
and MUST overwrite this file. All values defined here are byte-identical to
the canonical version so behaviour is unchanged either way.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Service identifiers
# ---------------------------------------------------------------------------

SERVICE_NAME: str = "zubot-ingestion"
SERVICE_VERSION: str = "0.1.0"


# ---------------------------------------------------------------------------
# Auth-exempt routes
# ---------------------------------------------------------------------------

AUTH_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/redoc",
    }
)


__all__ = [
    "SERVICE_NAME",
    "SERVICE_VERSION",
    "AUTH_EXEMPT_PATHS",
]
