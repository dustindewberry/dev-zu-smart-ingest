"""IJobRepository implementation backed by PostgreSQL via SQLAlchemy 2.0 async.

This adapter implements every method declared in
zubot_ingestion.domain.protocols.IJobRepository. It maps between the
domain dataclasses (Batch / Job / ReviewAction) and the ORM rows
(BatchORM / JobORM / ReviewActionORM) so the rest of the application
never sees SQLAlchemy types.

Transactional guarantees:
- ``create_batch`` inserts the batch and all child jobs in a single
  transaction (session.flush + session.commit). On failure the
  transaction is rolled back.
- All other mutating methods commit immediately.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from zubot_ingestion.domain.entities import (
    Batch,
    BatchWithJobs,
    Job,
    ReviewAction,
)
from zubot_ingestion.domain.enums import (
    BatchStatus,
    ConfidenceTier,
    ExtractionMode,
    JobStatus,
    ReviewActionType,
)
from zubot_ingestion.shared.constants import CONFIDENCE_TIER_REVIEW
from zubot_ingestion.infrastructure.database.models import (
    BatchORM,
    JobORM,
    ReviewActionORM,
)


# ---------- helpers ----------------------------------------------------------


def _to_batch(row: BatchORM) -> Batch:
    return Batch(
        id=row.id,
        deployment_id=row.deployment_id,
        node_id=row.node_id,
        callback_url=row.callback_url,
        mode=ExtractionMode(row.mode),
        created_by=row.created_by,
        total_jobs=row.total_jobs,
        status=BatchStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_job(row: JobORM) -> Job:
    return Job(
        id=row.id,
        batch_id=row.batch_id,
        filename=row.filename,
        file_hash=row.file_hash,
        file_size=row.file_size,
        status=JobStatus(row.status),
        mode=ExtractionMode(row.mode),
        result=row.result,
        confidence_tier=(
            ConfidenceTier(row.confidence_tier) if row.confidence_tier else None
        ),
        confidence_score=(
            float(row.confidence_score) if row.confidence_score is not None else None
        ),
        processing_time_ms=row.processing_time_ms,
        otel_trace_id=row.otel_trace_id,
        error_message=row.error_message,
        pipeline_trace=row.pipeline_trace,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _to_review_action(row: ReviewActionORM) -> ReviewAction:
    return ReviewAction(
        id=row.id,
        job_id=row.job_id,
        action=ReviewActionType(row.action),
        corrections=row.corrections,
        reviewed_by=row.reviewed_by,
        reason=row.reason,
        created_at=row.created_at,
    )


def _batch_to_orm(batch: Batch) -> BatchORM:
    return BatchORM(
        id=batch.id,
        deployment_id=batch.deployment_id,
        node_id=batch.node_id,
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
        id=job.id,
        batch_id=job.batch_id,
        filename=job.filename,
        file_hash=job.file_hash,
        file_size=job.file_size,
        status=job.status.value,
        mode=job.mode.value,
        result=job.result,
        confidence_tier=(
            job.confidence_tier.value if job.confidence_tier else None
        ),
        confidence_score=job.confidence_score,
        processing_time_ms=job.processing_time_ms,
        otel_trace_id=job.otel_trace_id,
        error_message=job.error_message,
        pipeline_trace=job.pipeline_trace,
        created_at=job.created_at,
        updated_at=job.updated_at,
        completed_at=job.completed_at,
    )


# ---------- repository -------------------------------------------------------


class JobRepository:
    """PostgreSQL implementation of IJobRepository.

    Constructed with an existing AsyncSession (typically managed by the
    application's get_session() context manager). Each public method
    performs its work in the caller's session and commits immediately so
    callers do not need to manage transactions themselves — except for
    ``create_batch``, which performs an explicit flush + commit pair to
    guarantee batch + jobs land atomically.
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

    async def get_batch(self, batch_id: UUID) -> BatchWithJobs | None:
        stmt = select(BatchORM).where(BatchORM.id == batch_id)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        # jobs are eager-loaded via lazy="selectin"
        jobs = [_to_job(j) for j in row.jobs]
        return BatchWithJobs(batch=_to_batch(row), jobs=jobs)

    async def get_job(self, job_id: UUID) -> Job | None:
        stmt = select(JobORM).where(JobORM.id == job_id)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_job(row) if row is not None else None

    async def list_jobs_by_batch(self, batch_id: UUID) -> list[Job]:
        stmt = select(JobORM).where(JobORM.batch_id == batch_id).order_by(
            JobORM.created_at
        )
        result = await self._session.execute(stmt)
        return [_to_job(r) for r in result.scalars().all()]

    # ---- update ----------------------------------------------------------

    async def update_job_status(
        self,
        job_id: UUID,
        status: JobStatus,
        error_message: str | None = None,
    ) -> Job | None:
        stmt = select(JobORM).where(JobORM.id == job_id)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.status = status.value
        row.updated_at = datetime.now(timezone.utc)
        if error_message is not None:
            row.error_message = error_message
        if status == JobStatus.COMPLETED:
            row.completed_at = datetime.now(timezone.utc)
        await self._session.commit()
        await self._session.refresh(row)
        return _to_job(row)

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
        now = datetime.now(timezone.utc)
        row.updated_at = now
        row.completed_at = now
        await self._session.commit()
        await self._session.refresh(row)
        return _to_job(row)

    async def update_batch_status(
        self,
        batch_id: UUID,
        status: BatchStatus,
    ) -> Batch | None:
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

    async def list_pending_reviews(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[Job], int]:
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
        row = ReviewActionORM(
            id=review.id,
            job_id=review.job_id,
            action=review.action.value,
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

    async def find_by_file_hash(self, file_hash: str) -> Job | None:
        """Return the most recent COMPLETED job for a file_hash, if any."""

        stmt = (
            select(JobORM)
            .where(JobORM.file_hash == file_hash)
            .where(JobORM.status == JobStatus.COMPLETED.value)
            .order_by(JobORM.completed_at.desc().nulls_last())
            .limit(1)
        )
        res = await self._session.execute(stmt)
        row = res.scalar_one_or_none()
        return _to_job(row) if row is not None else None
