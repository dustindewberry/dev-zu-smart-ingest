"""IJobRepository implementation backed by PostgreSQL via SQLAlchemy 2.0 async.

This adapter implements every method declared in
zubot_ingestion.domain.protocols.IJobRepository. It maps between the
canonical domain dataclasses (Batch / Job / ReviewAction) and the ORM rows
(BatchORM / JobORM / ReviewActionORM) so the rest of the application
never sees SQLAlchemy types.

Field-name reconciliation: the canonical entities use ``batch_id`` /
``job_id`` / ``review_action_id`` as their identifier fields, while the
ORM tables use ``id`` (the natural SQL convention). The mappers in this
module bridge that gap. Likewise the canonical ``Job`` entity carries an
``overall_confidence`` float field, which is persisted in the ORM column
``confidence_score``.

Transactional guarantees:
- ``create_batch`` inserts the batch and all child jobs in a single
  transaction (session.flush + session.commit). On failure the
  transaction is rolled back.
- All other mutating methods commit immediately.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from zubot_ingestion.domain.entities import (
    Batch,
    Job,
    ReviewAction,
)
from zubot_ingestion.domain.enums import (
    ConfidenceTier,
    ExtractionMode,
    JobStatus,
)
from zubot_ingestion.infrastructure.database.models import (
    BatchORM,
    JobORM,
    ReviewActionORM,
)
from zubot_ingestion.shared.constants import CONFIDENCE_TIER_REVIEW
from zubot_ingestion.shared.types import (
    BatchId,
    BatchProgress,
    BatchWithJobs,
    FileHash,
    JobId,
    JobSummary,
    PaginatedResult,
)


# ---------- helpers ----------------------------------------------------------


def _str_or_none(value: int | None) -> str | None:
    return None if value is None else str(value)


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_batch(row: BatchORM) -> Batch:
    """Map a ``BatchORM`` row to the canonical ``Batch`` entity."""

    return Batch(
        batch_id=BatchId(row.id),
        status=JobStatus(row.status),
        total_jobs=row.total_jobs,
        callback_url=row.callback_url,
        deployment_id=_int_or_none(row.deployment_id),
        node_id=_int_or_none(row.node_id),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        mode=ExtractionMode(row.mode),
    )


def _to_job(row: JobORM) -> Job:
    """Map a ``JobORM`` row to the canonical ``Job`` entity."""

    return Job(
        job_id=JobId(row.id),
        batch_id=BatchId(row.batch_id),
        filename=row.filename,
        file_hash=FileHash(row.file_hash),
        file_path=row.file_path or "",
        status=JobStatus(row.status),
        result=row.result,
        error_message=row.error_message,
        pipeline_trace=row.pipeline_trace,
        otel_trace_id=row.otel_trace_id,
        processing_time_ms=row.processing_time_ms,
        created_at=row.created_at,
        updated_at=row.updated_at,
        confidence_tier=(
            ConfidenceTier(row.confidence_tier) if row.confidence_tier else None
        ),
        overall_confidence=(
            float(row.confidence_score) if row.confidence_score is not None else None
        ),
    )


def _to_review_action(row: ReviewActionORM) -> ReviewAction:
    return ReviewAction(
        review_action_id=str(row.id),
        job_id=JobId(row.job_id),
        action=row.action,
        reviewed_by=row.reviewed_by,
        reason=row.reason,
        corrections=row.corrections,
        created_at=row.created_at,
    )


def _batch_to_orm(batch: Batch) -> BatchORM:
    return BatchORM(
        id=batch.batch_id,
        deployment_id=_str_or_none(batch.deployment_id),
        node_id=_str_or_none(batch.node_id),
        callback_url=batch.callback_url,
        mode=batch.mode.value,
        created_by=batch.created_by,
        total_jobs=batch.total_jobs,
        status=batch.status.value,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
    )


def _job_to_orm(job: Job) -> JobORM:
    return JobORM(
        id=job.job_id,
        batch_id=job.batch_id,
        filename=job.filename,
        file_hash=job.file_hash,
        file_path=job.file_path or None,
        status=job.status.value,
        result=job.result,
        confidence_tier=(
            job.confidence_tier.value if job.confidence_tier else None
        ),
        confidence_score=(
            float(job.overall_confidence)
            if job.overall_confidence is not None
            and not math.isnan(job.overall_confidence)
            else None
        ),
        processing_time_ms=job.processing_time_ms,
        otel_trace_id=job.otel_trace_id,
        error_message=job.error_message,
        pipeline_trace=job.pipeline_trace,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _job_to_summary(job: Job) -> JobSummary:
    return JobSummary(
        job_id=job.job_id,
        filename=job.filename,
        status=job.status,
        file_hash=job.file_hash,
        result=job.result,
    )


# ---------- repository -------------------------------------------------------


class JobRepository:
    """PostgreSQL implementation of IJobRepository.

    Constructed with an existing AsyncSession (typically managed by the
    application's ``get_session()`` context manager). The class is designed
    to satisfy the structural :class:`IJobRepository` protocol from
    ``zubot_ingestion.domain.protocols``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- create / read ----------------------------------------------------

    async def create_batch(
        self,
        batch: Batch,
        jobs: list[Job],
    ) -> Batch:
        """Insert batch + jobs in a single transaction."""

        try:
            batch_orm = _batch_to_orm(batch)
            self._session.add(batch_orm)
            await self._session.flush()  # ensure batch row exists for FK
            for job in jobs:
                job_orm = _job_to_orm(job)
                # Force the FK to point at the just-inserted batch.
                job_orm.batch_id = batch_orm.id
                self._session.add(job_orm)
            await self._session.commit()
            await self._session.refresh(batch_orm)
            return _to_batch(batch_orm)
        except SQLAlchemyError:
            await self._session.rollback()
            raise

    async def get_batch(self, batch_id: UUID) -> Batch | None:
        stmt = select(BatchORM).where(BatchORM.id == batch_id)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_batch(row) if row is not None else None

    async def get_batch_with_jobs(
        self,
        batch_id: UUID,
    ) -> BatchWithJobs | None:
        """Retrieve batch with all associated jobs as a ``BatchWithJobs`` DTO."""

        stmt = select(BatchORM).where(BatchORM.id == batch_id)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None

        # jobs are eager-loaded via lazy="selectin"
        job_entities = [_to_job(j) for j in row.jobs]
        progress = _build_progress(job_entities)
        return BatchWithJobs(
            batch_id=BatchId(row.id),
            status=JobStatus(row.status),
            progress=progress,
            jobs=[_job_to_summary(j) for j in job_entities],
            callback_url=row.callback_url,
            deployment_id=_int_or_none(row.deployment_id),
            node_id=_int_or_none(row.node_id),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def get_job(self, job_id: UUID) -> Job | None:
        stmt = select(JobORM).where(JobORM.id == job_id)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_job(row) if row is not None else None

    async def list_jobs_by_batch(self, batch_id: UUID) -> list[Job]:
        stmt = (
            select(JobORM)
            .where(JobORM.batch_id == batch_id)
            .order_by(JobORM.created_at)
        )
        result = await self._session.execute(stmt)
        return [_to_job(r) for r in result.scalars().all()]

    # ---- update ----------------------------------------------------------

    async def update_job_status(
        self,
        job_id: UUID,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update job status and optionally result/error.

        Matches the canonical IJobRepository.update_job_status signature.
        """

        stmt = select(JobORM).where(JobORM.id == job_id)
        res = await self._session.execute(stmt)
        row = res.scalar_one_or_none()
        if row is None:
            return None
        row.status = status.value
        row.updated_at = datetime.now(timezone.utc)
        if error_message is not None:
            row.error_message = error_message
        if result is not None:
            row.result = result
        await self._session.commit()
        return None

    async def update_job_result(
        self,
        job_id: UUID,
        result: dict[str, Any],
        confidence_tier: ConfidenceTier,
        confidence_score: float,
        processing_time_ms: int,
        otel_trace_id: str | None = None,
        pipeline_trace: dict[str, Any] | None = None,
    ) -> Job | None:
        """Persist a completed extraction result and mark job COMPLETED.

        Implements the canonical ``IJobRepository.update_job_result`` protocol
        method used by the orchestrator after a successful pipeline run.
        """
        stmt = select(JobORM).where(JobORM.id == job_id)
        res = await self._session.execute(stmt)
        row = res.scalar_one_or_none()
        if row is None:
            return None
        row.result = result
        row.confidence_tier = confidence_tier.value
        row.confidence_score = confidence_score
        row.processing_time_ms = processing_time_ms
        row.otel_trace_id = otel_trace_id
        row.pipeline_trace = pipeline_trace
        row.status = JobStatus.COMPLETED.value
        row.updated_at = datetime.now(timezone.utc)
        await self._session.commit()
        await self._session.refresh(row)
        return _to_job(row)

    async def update_batch_status(
        self,
        batch_id: UUID,
        status: JobStatus,
    ) -> Batch | None:
        """Update batch status (uses unified JobStatus enum)."""

        stmt = select(BatchORM).where(BatchORM.id == batch_id)
        res = await self._session.execute(stmt)
        row = res.scalar_one_or_none()
        if row is None:
            return None
        row.status = status.value
        row.updated_at = datetime.now(timezone.utc)
        await self._session.commit()
        await self._session.refresh(row)
        return _to_batch(row)

    # ---- review queue ----------------------------------------------------

    async def get_pending_reviews(
        self,
        page: int,
        per_page: int,
    ) -> PaginatedResult[Job]:
        """Canonical-protocol implementation of pending-review pagination."""

        items, total = await self.list_pending_reviews(page=page, per_page=per_page)
        total_pages = (total + per_page - 1) // per_page if per_page > 0 else 0
        return PaginatedResult[Job](
            items=items,
            total=total,
            page=page,
            per_page=per_page,
            total_pages=total_pages,
        )

    async def list_pending_reviews(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[Job], int]:
        """Convenience method returning ``(items, total)``.

        Used by older callers and by the canonical ``get_pending_reviews``
        method, which wraps this in a ``PaginatedResult``.
        """

        page = max(1, page)
        per_page = max(1, per_page)
        offset = (page - 1) * per_page

        items_stmt = (
            select(JobORM)
            .where(JobORM.confidence_tier == CONFIDENCE_TIER_REVIEW)
            .where(JobORM.status != JobStatus.REJECTED.value)
            .order_by(JobORM.created_at.desc())
            .limit(per_page)
            .offset(offset)
        )
        count_stmt = (
            select(func.count())
            .select_from(JobORM)
            .where(JobORM.confidence_tier == CONFIDENCE_TIER_REVIEW)
            .where(JobORM.status != JobStatus.REJECTED.value)
        )

        items_res = await self._session.execute(items_stmt)
        count_res = await self._session.execute(count_stmt)
        items = [_to_job(r) for r in items_res.scalars().all()]
        total = int(count_res.scalar() or 0)
        return items, total

    async def create_review_action(
        self,
        review: ReviewAction,
    ) -> ReviewAction:
        # The canonical ReviewAction.review_action_id is a string (UUID hex
        # or any opaque string). Coerce to a UUID for storage; if the input
        # is not a valid UUID, mint a fresh one.
        try:
            ra_id = UUID(review.review_action_id) if review.review_action_id else uuid4()
        except (ValueError, AttributeError):
            ra_id = uuid4()

        row = ReviewActionORM(
            id=ra_id,
            job_id=review.job_id,
            action=review.action,
            corrections=review.corrections,
            reviewed_by=review.reviewed_by,
            reason=review.reason,
            created_at=review.created_at,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return _to_review_action(row)

    # ---- deduplication ---------------------------------------------------

    async def get_job_by_file_hash(self, file_hash: FileHash) -> Job | None:
        """Return the most recent COMPLETED job for a file_hash, if any.

        Implements the canonical IJobRepository.get_job_by_file_hash method.
        Used by JobService.submit_batch to deduplicate uploads.
        """

        stmt = (
            select(JobORM)
            .where(JobORM.file_hash == file_hash)
            .where(JobORM.status == JobStatus.COMPLETED.value)
            .order_by(JobORM.updated_at.desc().nulls_last())
            .limit(1)
        )
        res = await self._session.execute(stmt)
        row = res.scalar_one_or_none()
        return _to_job(row) if row is not None else None

    # Backwards-compatible alias retained for callers (and the original task
    # description) that referenced the legacy name.
    find_by_file_hash = get_job_by_file_hash


def _build_progress(jobs: list[Job]) -> BatchProgress:
    completed = sum(1 for j in jobs if j.status == JobStatus.COMPLETED)
    queued = sum(1 for j in jobs if j.status == JobStatus.QUEUED)
    failed = sum(1 for j in jobs if j.status == JobStatus.FAILED)
    return BatchProgress(
        completed=completed,
        queued=queued,
        failed=failed,
        total=len(jobs),
    )
