"""Application service for batch submission and job lookup (CAP-009).

Implements :class:`zubot_ingestion.domain.protocols.IJobService`. The
primary entry point is :meth:`JobService.submit_batch` which:

1. Validates each uploaded file as a PDF (magic bytes + ``.pdf`` extension)
2. Hashes each file (SHA-256) and checks the repository for an existing
   completed job with the same hash — duplicates are reused from cache
3. Persists a new :class:`~zubot_ingestion.domain.entities.Batch` together
   with one :class:`~zubot_ingestion.domain.entities.Job` per NEW (non-
   duplicate) file, via
   :meth:`~zubot_ingestion.domain.protocols.IJobRepository.create_batch`
   (single transaction)
4. Writes uploaded file bytes to
   ``{temp_storage_root}/{batch_id}/{job_id}.pdf``
5. Enqueues one Celery task per new job via
   :meth:`~zubot_ingestion.domain.protocols.ITaskQueue.enqueue_extraction`
6. Returns a :class:`~zubot_ingestion.shared.types.BatchSubmissionResult`
   containing the batch id, the full list of job summaries (including
   cached duplicates), the total file count, and the polling URL

This module only depends on domain protocols and shared types — it does
NOT import from ``zubot_ingestion.infrastructure`` directly. Concrete
adapter instances are wired in by the API layer via
``get_job_service`` FastAPI dependency factory.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from zubot_ingestion.domain.entities import Batch, Job
from zubot_ingestion.domain.enums import ExtractionMode, JobStatus
from zubot_ingestion.domain.protocols import (
    IJobRepository,
    IJobService,
    IPDFProcessor,
    ITaskQueue,
)
from zubot_ingestion.shared.types import (
    AuthContext,
    BatchId,
    BatchSubmissionResult,
    BatchWithJobs,
    FileHash,
    JobDetail,
    JobId,
    JobSummary,
    SubmissionParams,
    UploadedFile,
)

# ---------------------------------------------------------------------------
# Module-level defaults
# ---------------------------------------------------------------------------

#: Maximum allowed uploaded file size in bytes (100 MB). Mirrors the value
#: enforced at the API layer via HTTP 413 responses.
MAX_PDF_BYTES: int = 100 * 1024 * 1024

#: Default temp storage root for persisting uploaded file bytes. The
#: service creates a batch-scoped subdirectory under this root for each
#: submission (``{root}/{batch_id}/{job_id}.pdf``).
DEFAULT_TEMP_STORAGE_ROOT: Path = Path("/tmp/zubot-ingestion")

#: Magic-byte prefix shared by all valid PDF files (``%PDF``).
_PDF_MAGIC: bytes = b"%PDF"


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class InvalidPDFError(ValueError):
    """Raised when an uploaded file is not a valid PDF.

    This is caught by the API layer and translated into an HTTP 400
    response. Callers should not catch it themselves.
    """


class OversizeFileError(ValueError):
    """Raised when an uploaded file exceeds :data:`MAX_PDF_BYTES`.

    Translated into an HTTP 413 response at the API boundary.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_valid_pdf(file: UploadedFile) -> bool:
    """Return True if ``file`` starts with the PDF magic bytes AND has a
    ``.pdf`` filename extension (case-insensitive).

    Both checks must pass — the extension check alone is insufficient
    because clients can rename arbitrary files, and the magic-byte check
    alone would admit binaries with a spoofed header and unrelated
    extension.
    """
    if not file.filename.lower().endswith(".pdf"):
        return False
    if not file.content.startswith(_PDF_MAGIC):
        return False
    return True


def _sha256(data: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _now() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def _build_poll_url(batch_id: UUID) -> str:
    """Return the GET URL for polling a batch's status."""
    return f"/batches/{batch_id}"


# ---------------------------------------------------------------------------
# JobService
# ---------------------------------------------------------------------------


class JobService:
    """Implements :class:`IJobService`.

    Collaborators are injected as protocols so the service can be unit
    tested with simple in-memory fakes. The repository is responsible for
    transactional persistence (batch + jobs in a single commit). The task
    queue fires extraction Celery tasks. The PDF processor is accepted
    for future validation expansion (e.g. metadata sanity checks) but is
    not strictly required by ``submit_batch`` today.
    """

    def __init__(
        self,
        repository: IJobRepository,
        task_queue: ITaskQueue,
        pdf_processor: IPDFProcessor,
        *,
        temp_storage_root: Path | None = None,
        max_pdf_bytes: int = MAX_PDF_BYTES,
    ) -> None:
        self._repository = repository
        self._task_queue = task_queue
        self._pdf_processor = pdf_processor
        self._temp_storage_root = temp_storage_root or DEFAULT_TEMP_STORAGE_ROOT
        self._max_pdf_bytes = max_pdf_bytes

    # ------------------------------------------------------------------
    # IJobService.submit_batch
    # ------------------------------------------------------------------

    async def submit_batch(
        self,
        files: list[UploadedFile],
        params: SubmissionParams,
        auth_context: AuthContext,
    ) -> BatchSubmissionResult:
        """Validate, persist, and enqueue a batch of PDF extraction jobs.

        Validation errors raise :class:`InvalidPDFError` or
        :class:`OversizeFileError` BEFORE any persistence side effect so
        that a single bad file cannot corrupt a half-written batch.
        """
        if not files:
            raise InvalidPDFError("No files provided")

        # --- 1. Validation pass (no side effects) --------------------- #
        for f in files:
            if len(f.content) > self._max_pdf_bytes:
                raise OversizeFileError(
                    f"{f.filename} exceeds max size of {self._max_pdf_bytes} bytes"
                )
            if not _is_valid_pdf(f):
                raise InvalidPDFError(f"{f.filename} is not a valid PDF")

        now = _now()
        batch_id = BatchId(uuid4())
        batch_dir = self._temp_storage_root / str(batch_id)
        batch_dir.mkdir(parents=True, exist_ok=True)

        # --- 2. Dedup + new-job construction -------------------------- #
        job_summaries: list[JobSummary] = []
        new_jobs: list[Job] = []

        for f in files:
            file_hash = FileHash(_sha256(f.content))
            existing = await self._repository.get_job_by_file_hash(file_hash)
            if existing is not None and existing.status == JobStatus.COMPLETED:
                # Cache hit — surface the prior result as a CACHED summary.
                job_summaries.append(
                    JobSummary(
                        job_id=existing.job_id,
                        filename=f.filename,
                        status=JobStatus.CACHED,
                        file_hash=file_hash,
                        result=existing.result,
                    )
                )
                continue

            # New job — allocate id, save bytes, build entity.
            new_job_id = JobId(uuid4())
            file_path = batch_dir / f"{new_job_id}.pdf"
            file_path.write_bytes(f.content)

            job = Job(
                job_id=new_job_id,
                batch_id=batch_id,
                filename=f.filename,
                file_hash=file_hash,
                file_path=str(file_path),
                status=JobStatus.QUEUED,
                result=None,
                error_message=None,
                pipeline_trace=None,
                otel_trace_id=None,
                processing_time_ms=None,
                created_at=now,
                updated_at=now,
            )
            new_jobs.append(job)
            job_summaries.append(
                JobSummary(
                    job_id=new_job_id,
                    filename=f.filename,
                    status=JobStatus.QUEUED,
                    file_hash=file_hash,
                    result=None,
                )
            )

        # --- 3. Persist batch + jobs atomically ----------------------- #
        batch = Batch(
            batch_id=batch_id,
            status=JobStatus.QUEUED,
            total_jobs=len(new_jobs),
            callback_url=params.callback_url,
            deployment_id=params.deployment_id,
            node_id=params.node_id,
            created_by=auth_context.user_id,
            created_at=now,
            updated_at=now,
            mode=params.mode or ExtractionMode.AUTO,
        )
        await self._repository.create_batch(batch, new_jobs)

        # --- 4. Enqueue Celery tasks for new jobs --------------------- #
        for job in new_jobs:
            self._task_queue.enqueue_extraction(job.job_id)

        # --- 5. Build response ---------------------------------------- #
        return BatchSubmissionResult(
            batch_id=batch_id,
            jobs=job_summaries,
            total=len(job_summaries),
            poll_url=_build_poll_url(batch_id),
        )

    # ------------------------------------------------------------------
    # IJobService.get_batch
    # ------------------------------------------------------------------

    async def get_batch(
        self,
        batch_id: UUID,
        auth_context: AuthContext,
    ) -> BatchWithJobs | None:
        """Return a :class:`BatchWithJobs` DTO for the given batch.

        Delegates straight to the repository; the repository adapter is
        responsible for shaping the DTO (progress counters, job
        summaries, etc.).
        """
        # The canonical protocol types get_batch_with_jobs as
        # ``tuple[Batch, list[Job]] | None`` but our repository adapter
        # returns the richer BatchWithJobs DTO directly. Both shapes are
        # handled here so the service remains adapter-agnostic.
        result = await self._repository.get_batch_with_jobs(batch_id)  # type: ignore[func-returns-value]
        if result is None:
            return None
        if isinstance(result, BatchWithJobs):
            return result
        # Fallback: adapter returned the canonical tuple shape.
        batch, jobs = result  # type: ignore[misc]
        from zubot_ingestion.shared.types import BatchProgress

        completed = sum(1 for j in jobs if j.status == JobStatus.COMPLETED)
        queued = sum(1 for j in jobs if j.status == JobStatus.QUEUED)
        failed = sum(1 for j in jobs if j.status == JobStatus.FAILED)
        return BatchWithJobs(
            batch_id=batch.batch_id,
            status=batch.status,
            progress=BatchProgress(
                completed=completed,
                queued=queued,
                failed=failed,
                total=len(jobs),
            ),
            jobs=[
                JobSummary(
                    job_id=j.job_id,
                    filename=j.filename,
                    status=j.status,
                    file_hash=j.file_hash,
                    result=j.result,
                )
                for j in jobs
            ],
            callback_url=batch.callback_url,
            deployment_id=batch.deployment_id,
            node_id=batch.node_id,
            created_at=batch.created_at,
            updated_at=batch.updated_at,
        )

    # ------------------------------------------------------------------
    # IJobService.get_job
    # ------------------------------------------------------------------

    async def get_job(
        self,
        job_id: UUID,
        auth_context: AuthContext,
    ) -> JobDetail | None:
        """Return a :class:`JobDetail` for the given job id, or ``None``."""
        job = await self._repository.get_job(job_id)
        if job is None:
            return None
        return JobDetail(
            job_id=job.job_id,
            batch_id=job.batch_id,
            filename=job.filename,
            file_hash=job.file_hash,
            status=job.status,
            result=job.result,
            error_message=job.error_message,
            pipeline_trace=job.pipeline_trace,
            otel_trace_id=job.otel_trace_id,
            processing_time_ms=job.processing_time_ms,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    # ------------------------------------------------------------------
    # IJobService.get_job_preview
    # ------------------------------------------------------------------

    async def get_job_preview(
        self,
        job_id: UUID,
        auth_context: AuthContext,
    ) -> bytes | None:
        """Return a rendered preview image for the given job.

        The full preview implementation (image caching, rendering via
        :class:`IPDFProcessor`) is delivered by a later step (CAP-012).
        This stub returns ``None`` so the class can still satisfy the
        :class:`IJobService` protocol.
        """
        return None


# Static conformance check: importing this module at startup will fail
# if ``JobService`` drifts from ``IJobService`` (e.g. a method signature
# changes). We intentionally defer the ``isinstance`` check to runtime
# tests rather than perform it at import time, to keep module import
# side-effect free.

_: type[IJobService] = JobService  # noqa: F841 — static protocol check


__all__ = [
    "DEFAULT_TEMP_STORAGE_ROOT",
    "InvalidPDFError",
    "JobService",
    "MAX_PDF_BYTES",
    "OversizeFileError",
]
