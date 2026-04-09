"""Celery application wiring for the Zubot Smart Ingestion Service.

Implements CAP-005 (background task queue) and CAP-020 (extraction
orchestrator entry-point). Configures the Celery app with a Redis broker
(DB 2) and Redis result backend (DB 3), JSON serialization for both
arguments and results, and the retry/ack policy required by the
architecture:

    - ``worker_prefetch_multiplier = 1``       (long-running tasks, no prefetch)
    - ``task_acks_late = True``                (ack after success, not on receive)
    - ``task_reject_on_worker_lost = True``    (redeliver on crash)
    - ``broker_connection_retry_on_startup``   (keep reconnecting at boot)
    - ``task_default_retry_delay = 2``         (base retry delay)
    - ``task_max_retries = 3``                 (cap retries)

The ``extract_document_task`` runs the full :class:`ExtractionOrchestrator`
pipeline for a single job:

    1. Fetch the :class:`Job` from the repository.
    2. Load the PDF bytes from temporary storage at
       ``/tmp/zubot-ingestion/{batch_id}/{job_id}.pdf``.
    3. Build the orchestrator with all of its dependencies via
       :func:`zubot_ingestion.services.build_orchestrator`.
    4. Run :meth:`ExtractionOrchestrator.run_pipeline`.
    5. Persist the result, status, confidence_score, and confidence_tier
       back to the repository.
    6. On any unhandled exception, mark the job as ``FAILED`` and re-raise
       so Celery's retry policy can take over.

Layering: this module lives in the application (services) layer. It imports
only from :mod:`zubot_ingestion.config`, :mod:`zubot_ingestion.shared`, and
:mod:`zubot_ingestion.domain`. Infrastructure adapters are constructed
inside the factory function in :mod:`zubot_ingestion.services.__init__`,
never imported here directly.

Structured logging (CAP-029): the worker_process_init signal configures
structlog inside each Celery worker process; task_prerun / task_postrun /
task_failure bind and clear job context on contextvars so every log line
emitted while a task runs inherits ``job_id`` / ``batch_id`` / ``file_hash``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from celery import Celery
from celery.signals import (
    task_failure,
    task_postrun,
    task_prerun,
    worker_process_init,
)

from zubot_ingestion.config import get_settings
from zubot_ingestion.domain.enums import JobStatus
from zubot_ingestion.infrastructure.logging.config import (
    bind_job_context,
    clear_context,
    get_logger,
    setup_logging,
)
from zubot_ingestion.services import build_orchestrator, get_job_repository
from zubot_ingestion.shared.constants import (
    CELERY_BROKER_DB,
    CELERY_QUEUE_DEFAULT,
    CELERY_RESULT_DB,
    CELERY_TASK_NAME_EXTRACTION,
)

_LOG = logging.getLogger(__name__)
_struct_logger = get_logger(__name__)

#: Root directory for temporary PDF storage written by the API on submit.
TEMP_PDF_ROOT: Path = Path("/tmp/zubot-ingestion")


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
    worker_concurrency=_settings.CELERY_WORKER_CONCURRENCY,
    worker_prefetch_multiplier=_settings.CELERY_WORKER_PREFETCH_MULTIPLIER,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    task_default_retry_delay=2,
    task_max_retries=3,
)


# ---------------------------------------------------------------------------
# Structured-logging signal hooks (CAP-029)
# ---------------------------------------------------------------------------


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
    _struct_logger.info(
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
    _struct_logger.info(
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
    _struct_logger.error(
        "celery.task.failure",
        celery_task_id=task_id,
        error_type=type(exception).__name__ if exception else None,
        error_message=str(exception) if exception else None,
        traceback=str(einfo) if einfo else None,
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
    """Run the extraction pipeline for a single job (CAP-020).

    Args:
        job_id: String UUID of the persisted :class:`Job` to process.

    Returns:
        A small dict with ``job_id``, ``status``, ``confidence_score``, and
        ``confidence_tier`` for the result backend so callers polling
        :meth:`ITaskQueue.get_task_status` can read the high-level outcome
        without re-querying the repository.

    Retry/cleanup policy:
        ``autoretry_for=(Exception,)`` is configured on the task decorator
        so Celery will automatically retry on any unhandled exception. The
        manual ``except`` block gates the ``_mark_job_failed`` cleanup on
        ``self.request.retries >= self.max_retries`` so FAILED is only
        written once at terminal failure — this prevents the
        FAILED↔PROCESSING oscillation that would otherwise occur when the
        pre-pipeline PROCESSING marker and a mid-retry FAILED marker raced
        against each other.
    """
    parsed_job_id = UUID(job_id)

    try:
        return asyncio.run(_run_extract_document_task(parsed_job_id))
    except Exception as exc:  # noqa: BLE001 - mark failed then re-raise
        _LOG.exception("extract_document_task_failed", extra={"job_id": job_id})
        # Only write FAILED on the terminal retry attempt. Earlier retries
        # leave the status at PROCESSING so the job does not oscillate
        # FAILED↔PROCESSING across the retry cycle. ``autoretry_for`` will
        # still re-raise for Celery to schedule the next retry.
        if self.request.retries >= self.max_retries:
            try:
                asyncio.run(_mark_job_failed(parsed_job_id, exc))
            except Exception:  # noqa: BLE001 - never let cleanup mask the original
                _LOG.exception(
                    "extract_document_task_failed_cleanup_failed",
                    extra={"job_id": job_id},
                )
        raise


async def _run_extract_document_task(job_id: UUID) -> dict[str, Any]:
    """Async body of :func:`extract_document_task`.

    Separated from the synchronous Celery task body so it can be unit
    tested directly with ``asyncio.run`` (without going through Celery's
    eager-mode shim).

    Repository access is scoped to a single session via the
    :func:`get_job_repository` async context manager. The pre-pipeline
    ``update_job_status(PROCESSING)`` is the ONLY PROCESSING write in the
    task — the success path uses :meth:`JobRepository.update_job_result`
    so the indexed columns (``confidence_score``, ``confidence_tier``,
    ``processing_time_ms``, ``otel_trace_id``, ``pipeline_trace``) are
    actually populated.
    """
    async with get_job_repository() as repo:
        job = await repo.get_job(job_id)
        if job is None:
            raise LookupError(f"job {job_id} not found in repository")

        # Fetch the parent batch so we can forward deployment_id / node_id to
        # the orchestrator's metadata writer (CAP-023) and the callback URL
        # to the webhook delivery stage (CAP-025). Failure to load the
        # batch is tolerated — the orchestrator will fall back to the
        # 'default' ChromaDB collection if both IDs are None, and will
        # skip the callback stage if callback_url is None.
        deployment_id: int | None = None
        node_id: int | None = None
        callback_url: str | None = None
        try:
            batch = await repo.get_batch(job.batch_id)
            if batch is not None:
                deployment_id = batch.deployment_id
                node_id = batch.node_id
                callback_url = batch.callback_url
        except Exception:  # noqa: BLE001 - tolerate missing batch
            _LOG.warning(
                "extract_document_task_batch_lookup_failed",
                extra={
                    "job_id": str(job.job_id),
                    "batch_id": str(job.batch_id),
                },
            )

        # Source the webhook authentication credential from settings so
        # receivers can authenticate the webhook sender via X-API-Key.
        # We use the single-tenant service API key (ZUBOT_INGESTION_API_KEY)
        # because Batch has no per-submission api_key column and the
        # AuthContext discriminator does not carry the literal key string.
        # An empty string falls through to CallbackHttpClient which omits
        # the header entirely, preserving current behaviour in local/CI
        # environments where the key is unset.
        task_settings = get_settings()
        callback_api_key: str = task_settings.ZUBOT_INGESTION_API_KEY or ""

        pdf_path = TEMP_PDF_ROOT / str(job.batch_id) / f"{job.job_id}.pdf"
        pdf_bytes = pdf_path.read_bytes()

        orchestrator = build_orchestrator()

        # Mark the job as PROCESSING before running the pipeline so the API
        # can observe the in-flight state. This is the ONLY pre-pipeline
        # status write — there is no mid-pipeline PROCESSING reset inside
        # the retry path.
        await repo.update_job_status(
            job.job_id,
            status=JobStatus.PROCESSING,
            result=None,
            error_message=None,
        )

        pipeline_start = time.perf_counter()
        pipeline_result = await orchestrator.run_pipeline(
            job,
            pdf_bytes,
            deployment_id=deployment_id,
            node_id=node_id,
            callback_url=callback_url,
            api_key=callback_api_key,
        )
        processing_time_ms = int((time.perf_counter() - pipeline_start) * 1000)

        # Choose the persisted job status from the assessment tier:
        #   AUTO   -> COMPLETED   (auto-publish)
        #   SPOT   -> COMPLETED   (publish, flag for spot-check)
        #   REVIEW -> REVIEW      (block on human reviewer)
        tier = pipeline_result.confidence_assessment.tier
        confidence_score = pipeline_result.confidence_assessment.overall_confidence
        new_status = (
            JobStatus.REVIEW if tier.value == "review" else JobStatus.COMPLETED
        )

        result_payload: dict[str, Any] = {
            "drawing_number": pipeline_result.extraction_result.drawing_number,
            "title": pipeline_result.extraction_result.title,
            "document_type": (
                pipeline_result.extraction_result.document_type.value
                if pipeline_result.extraction_result.document_type is not None
                else None
            ),
            "confidence_score": confidence_score,
            "confidence_tier": tier.value,
            "sidecar": pipeline_result.sidecar.metadata_attributes,
            "pipeline_trace": pipeline_result.pipeline_trace,
        }

        # Persist the full result via update_job_result so the dedicated
        # indexed columns (confidence_score, confidence_tier,
        # processing_time_ms, otel_trace_id, pipeline_trace) are populated
        # alongside the JSONB result blob. update_job_result marks the row
        # as COMPLETED; REVIEW-tier rows need an explicit status override.
        await repo.update_job_result(
            job_id=job.job_id,
            result=result_payload,
            confidence_tier=tier,
            confidence_score=confidence_score,
            processing_time_ms=processing_time_ms,
            otel_trace_id=pipeline_result.otel_trace_id,
            pipeline_trace=pipeline_result.pipeline_trace,
        )
        if new_status is JobStatus.REVIEW:
            # update_job_result always marks the row COMPLETED; for the
            # REVIEW tier we overwrite with the REVIEW status so the row
            # is blocked on a human reviewer.
            await repo.update_job_status(
                job.job_id,
                status=JobStatus.REVIEW,
                result=None,
                error_message=None,
            )

        return {
            "job_id": str(job.job_id),
            "status": new_status.value,
            "confidence_score": confidence_score,
            "confidence_tier": tier.value,
        }


async def _mark_job_failed(job_id: UUID, exc: BaseException) -> None:
    """Persist a FAILED status + error message to the repository.

    Opens its own :func:`get_job_repository` session so the cleanup is
    self-contained and can be driven from a sync ``except`` block via
    ``asyncio.run(_mark_job_failed(job_id, exc))``.
    """
    async with get_job_repository() as repo:
        await repo.update_job_status(
            job_id,
            status=JobStatus.FAILED,
            result=None,
            error_message=f"{type(exc).__name__}: {exc}",
        )


__all__ = [
    "app",
    "BROKER_URL",
    "RESULT_BACKEND_URL",
    "TEMP_PDF_ROOT",
    "extract_document_task",
]
