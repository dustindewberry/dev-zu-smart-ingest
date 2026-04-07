"""Celery application stub with structured-logging signal hooks (CAP-029).

NOTE: The full Celery configuration is the responsibility of task-7
(celery-configuration). This module exists in this worktree only to host
the ``task_prerun`` / ``task_postrun`` / ``task_failure`` signal handlers
that bind/clear job context for the structured logger. On merge with
task-7's branch, the canonical Celery app definition takes precedence and
the three signal handlers below should be ported into it verbatim.
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import (
    task_failure,
    task_postrun,
    task_prerun,
    worker_process_init,
)

from zubot_ingestion.config import get_settings
from zubot_ingestion.infrastructure.logging.config import (
    bind_job_context,
    clear_context,
    get_logger,
    setup_logging,
)

_logger = get_logger(__name__)


# Minimal Celery app definition — the canonical task-7 version configures
# brokers, queues, retry policy, etc.
celery_app = Celery("zubot_ingestion")


@worker_process_init.connect
def _init_worker_logging(**_kwargs: Any) -> None:
    """Configure structured logging in each Celery worker process."""
    settings = get_settings()
    setup_logging(log_level=settings.LOG_LEVEL, log_dir=settings.LOG_DIR)


def _extract_job_context(kwargs: dict[str, Any] | None) -> dict[str, str | None]:
    """Pull job_id / batch_id / file_hash out of a Celery task kwargs dict."""
    kwargs = kwargs or {}
    return {
        "job_id": kwargs.get("job_id"),
        "batch_id": kwargs.get("batch_id"),
        "file_hash": kwargs.get("file_hash"),
    }


@task_prerun.connect
def _on_task_prerun(
    task_id: str | None = None,
    task: Any = None,
    args: tuple[Any, ...] | None = None,
    kwargs: dict[str, Any] | None = None,
    **_extra: Any,
) -> None:
    """Bind job context (and the celery task_id) before a task runs."""
    ctx = _extract_job_context(kwargs)
    job_id = ctx["job_id"] or task_id or "unknown"
    bind_job_context(
        job_id=str(job_id),
        batch_id=ctx["batch_id"],
        file_hash=ctx["file_hash"],
    )
    _logger.info(
        "celery.task.start",
        task_name=getattr(task, "name", None),
        celery_task_id=task_id,
    )


@task_postrun.connect
def _on_task_postrun(
    task_id: str | None = None,
    task: Any = None,
    state: str | None = None,
    **_extra: Any,
) -> None:
    """Log completion and clear bound job context."""
    _logger.info(
        "celery.task.end",
        task_name=getattr(task, "name", None),
        celery_task_id=task_id,
        state=state,
    )
    clear_context()


@task_failure.connect
def _on_task_failure(
    task_id: str | None = None,
    exception: BaseException | None = None,
    einfo: Any = None,
    **_extra: Any,
) -> None:
    """Log task failures via structlog (context is still bound here)."""
    _logger.error(
        "celery.task.failure",
        celery_task_id=task_id,
        error_type=type(exception).__name__ if exception else None,
        error_message=str(exception) if exception else None,
        traceback=str(einfo) if einfo else None,
    )
