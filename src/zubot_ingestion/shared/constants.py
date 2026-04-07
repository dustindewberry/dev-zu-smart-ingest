"""Transitional stub of shared constants.

NOTE: This is a MINIMAL stub containing only the symbols this task imports.
The canonical full-featured constants module is produced by task-26
(witty-maple worktree, commit 50fe47a) and is a strict superset of this
stub. On merge the canonical version MUST overwrite this stub. Values are
byte-identical to the canonical version so no behavioral drift.
"""

from __future__ import annotations

# Service identifiers
SERVICE_NAME: str = "zubot-ingestion"
SERVICE_VERSION: str = "0.1.0"

# Auth-exempt routes (subset needed by /health)
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
