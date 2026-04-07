"""Celery application wiring for the Zubot Smart Ingestion Service.

Implements CAP-005 (background task queue). Configures the Celery app with a
Redis broker (DB 2) and Redis result backend (DB 3), JSON serialization for
both arguments and results, and the retry/ack policy required by the
architecture:

    - ``worker_prefetch_multiplier = 1``       (long-running tasks, no prefetch)
    - ``task_acks_late = True``                (ack after success, not on receive)
    - ``task_reject_on_worker_lost = True``    (redeliver on crash)
    - ``broker_connection_retry_on_startup``   (keep reconnecting at boot)
    - ``task_default_retry_delay = 2``         (base retry delay)
    - ``task_max_retries = 3``                 (cap retries)

The ``extract_document_task`` defined here is an intentional placeholder: it
registers the task name (``CELERY_TASK_NAME_EXTRACTION``) so the rest of the
system can enqueue work via :class:`ITaskQueue`, but the real extraction
pipeline is implemented in task-16 (orchestrator-and-confidence). Calling it
directly at this step raises :class:`NotImplementedError`.

Layering: this module lives in the application (services) layer. It imports
only from :mod:`zubot_ingestion.config` and :mod:`zubot_ingestion.shared`,
never from the ``infrastructure`` or ``api`` layers.
"""

from __future__ import annotations

from celery import Celery

from zubot_ingestion.config import get_settings
from zubot_ingestion.shared.constants import (
    CELERY_BROKER_DB,
    CELERY_QUEUE_DEFAULT,
    CELERY_RESULT_DB,
    CELERY_TASK_NAME_EXTRACTION,
)


def _build_redis_url(base: str, db: int) -> str:
    """Append a Redis database index to ``base``.

    Handles an optional trailing slash so that both ``redis://host:6379`` and
    ``redis://host:6379/`` produce the same final URL. The blueprint default
    (``redis://redis:6379``) has no trailing slash, but defensive parsing lets
    operators override the URL without surprises.
    """
    stripped = base.rstrip("/")
    return f"{stripped}/{db}"


_settings = get_settings()

BROKER_URL: str = _build_redis_url(_settings.REDIS_URL, CELERY_BROKER_DB)
RESULT_BACKEND_URL: str = _build_redis_url(_settings.REDIS_URL, CELERY_RESULT_DB)


app = Celery(
    "zubot_ingestion",
    broker=BROKER_URL,
    backend=RESULT_BACKEND_URL,
)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_default_queue=CELERY_QUEUE_DEFAULT,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    task_default_retry_delay=2,
    task_max_retries=3,
)


@app.task(
    bind=True,
    name=CELERY_TASK_NAME_EXTRACTION,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=8,
    retry_jitter=True,
)
def extract_document_task(self, job_id: str) -> dict:  # type: ignore[no-untyped-def]
    """Placeholder extraction task.

    The full implementation lives in task-16 (orchestrator-and-confidence).
    This stub exists at step-7 only to register the task under
    ``CELERY_TASK_NAME_EXTRACTION`` so :class:`ITaskQueue` implementations
    have a concrete task to ``delay()`` against.
    """
    raise NotImplementedError("Implemented in step-16")


__all__ = [
    "app",
    "BROKER_URL",
    "RESULT_BACKEND_URL",
    "extract_document_task",
]
