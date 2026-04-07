"""Single source of truth for string constants used across the service.

NOTE (transitional stub): This worker (task-7 / step-7 celery-configuration)
was branched from the initial commit and therefore could NOT see the canonical
constants module produced by task-26 (commit 50fe47a, witty-maple worktree).

This file exposes ONLY the symbols needed by task-7's celery_app.py and
task_queue.py:

    - CELERY_BROKER_DB
    - CELERY_RESULT_DB
    - CELERY_TASK_NAME_EXTRACTION
    - CELERY_QUEUE_DEFAULT
    - SERVICE_NAME
    - SERVICE_VERSION

All values are byte-identical to the canonical task-26 module so this stub
can be safely overwritten by the canonical superset on merge.
"""

from __future__ import annotations

# Service identifiers
SERVICE_NAME: str = "zubot-ingestion"
SERVICE_VERSION: str = "0.1.0"

# Celery / Redis
CELERY_BROKER_DB: int = 2
CELERY_RESULT_DB: int = 3
CELERY_TASK_NAME_EXTRACTION: str = "zubot_ingestion.tasks.extract_document"
CELERY_QUEUE_DEFAULT: str = "zubot_ingestion"


__all__ = [
    "SERVICE_NAME",
    "SERVICE_VERSION",
    "CELERY_BROKER_DB",
    "CELERY_RESULT_DB",
    "CELERY_TASK_NAME_EXTRACTION",
    "CELERY_QUEUE_DEFAULT",
]
