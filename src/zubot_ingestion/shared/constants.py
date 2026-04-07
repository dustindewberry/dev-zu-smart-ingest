"""TRANSITIONAL STUB — minimal subset of the canonical task-26 constants module.

This file exists ONLY because the sharp-spark worktree (task step-24
rate-limiting) was branched from the initial commit and therefore does not
contain the canonical ``src/zubot_ingestion/shared/constants.py`` produced by
sibling task-26 (witty-maple, commit 50fe47a).

The constants and ``__all__`` entries below are byte-identical to the canonical
file. On merge with task-26's output, the canonical, full-featured constants
module is a strict superset of this stub and MUST overwrite this file.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Service identifiers
# ---------------------------------------------------------------------------

SERVICE_NAME: str = "zubot-ingestion"
SERVICE_VERSION: str = "0.1.0"


# ---------------------------------------------------------------------------
# Celery / Redis
# ---------------------------------------------------------------------------

CELERY_BROKER_DB: int = 2
CELERY_RESULT_DB: int = 3
RATE_LIMIT_REDIS_DB: int = 4

CELERY_TASK_NAME_EXTRACTION: str = "zubot_ingestion.tasks.extract_document"
CELERY_QUEUE_DEFAULT: str = "zubot_ingestion"


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
    "CELERY_BROKER_DB",
    "CELERY_RESULT_DB",
    "RATE_LIMIT_REDIS_DB",
    "CELERY_TASK_NAME_EXTRACTION",
    "CELERY_QUEUE_DEFAULT",
    "AUTH_EXEMPT_PATHS",
]
