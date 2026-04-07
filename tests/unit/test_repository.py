"""Unit tests for the JobRepository (CAP-004).

These tests verify post-mutation state matches expectations for every
mutating method on JobRepository, using an in-memory aiosqlite database.
The portable UUIDType / JSONBType TypeDecorators in models.py let the
PostgreSQL schema run unmodified under SQLite.

The tests target the *canonical* entity contract from
``zubot_ingestion.domain.entities`` and ``zubot_ingestion.domain.enums``,
which uses ``batch_id`` / ``job_id`` field names and a unified
``JobStatus`` enum (no separate BatchStatus or ReviewActionType).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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
from zubot_ingestion.domain.protocols import IJobRepository
from zubot_ingestion.infrastructure.database.models import Base
from zubot_ingestion.infrastructure.database.repository import JobRepository
from zubot_ingestion.shared.types import BatchId, FileHash, JobId


# ---------- fixtures ---------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncSession:  # type: ignore[misc]
    """In-memory SQLite session with the production schema."""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


def _make_batch(total_jobs: int = 1) -> Batch:
    now = datetime.now(timezone.utc)
    return Batch(
        batch_id=BatchId(uuid4()),
        status=JobStatus.QUEUED,
        total_jobs=total_jobs,
        callback_url="https://example.test/cb",
        deployment_id=1,
        node_id=2,
        created_by="alice",
        created_at=now,
        updated_at=now,
        mode=ExtractionMode.AUTO,
    )


def _make_job(
    batch_id,
    file_hash: str = "h" * 64,
    filename: str = "f.pdf",
) -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        job_id=JobId(uuid4()),
        batch_id=batch_id,
        filename=filename,
        file_hash=FileHash(file_hash),
        file_path=f"/tmp/zubot/{filename}",
        status=JobStatus.QUEUED,
        result=None,
        error_message=None,
        pipeline_trace=None,
        otel_trace_id=None,
        processing_time_ms=None,
        created_at=now,
        updated_at=now,
    )


# ---------- tests ------------------------------------------------------------


@pytest.mark.asyncio
async def test_repository_satisfies_protocol(session):
    repo = JobRepository(session)
    assert isinstance(repo, IJobRepository)


@pytest.mark.asyncio
async def test_create_batch_persists_batch_and_jobs_atomically(session):
    repo = JobRepository(session)
    batch = _make_batch(total_jobs=2)
    j1 = _make_job(batch.batch_id, file_hash="a" * 64, filename="one.pdf")
    j2 = _make_job(batch.batch_id, file_hash="b" * 64, filename="two.pdf")

    await repo.create_batch(batch, [j1, j2])

    fetched = await repo.get_batch_with_jobs(batch.batch_id)
    assert fetched is not None
    assert fetched.batch_id == batch.batch_id
    assert fetched.progress.total == 2
    assert fetched.status == JobStatus.QUEUED
    # AFTER create_batch, both jobs must be retrievable
    assert len(fetched.jobs) == 2
    filenames = {j.filename for j in fetched.jobs}
    assert filenames == {"one.pdf", "two.pdf"}


@pytest.mark.asyncio
async def test_get_batch_returns_none_when_missing(session):
    repo = JobRepository(session)
    assert await repo.get_batch(uuid4()) is None
    assert await repo.get_batch_with_jobs(uuid4()) is None


@pytest.mark.asyncio
async def test_get_batch_returns_batch_after_create(session):
    repo = JobRepository(session)
    batch = _make_batch()
    job = _make_job(batch.batch_id)
    await repo.create_batch(batch, [job])

    fetched = await repo.get_batch(batch.batch_id)
    assert fetched is not None
    assert fetched.batch_id == batch.batch_id
    assert fetched.created_by == "alice"
    assert fetched.deployment_id == 1
    assert fetched.node_id == 2


@pytest.mark.asyncio
async def test_get_job_returns_none_when_missing(session):
    repo = JobRepository(session)
    assert await repo.get_job(uuid4()) is None


@pytest.mark.asyncio
async def test_get_job_returns_job_after_create(session):
    repo = JobRepository(session)
    batch = _make_batch()
    job = _make_job(batch.batch_id)
    await repo.create_batch(batch, [job])

    fetched = await repo.get_job(job.job_id)
    assert fetched is not None
    assert fetched.job_id == job.job_id
    assert fetched.filename == "f.pdf"
    assert fetched.status == JobStatus.QUEUED


@pytest.mark.asyncio
async def test_update_job_status_persists_new_status(session):
    repo = JobRepository(session)
    batch = _make_batch()
    job = _make_job(batch.batch_id)
    await repo.create_batch(batch, [job])

    await repo.update_job_status(job.job_id, JobStatus.PROCESSING)

    # Re-fetch and verify state AFTER mutation
    refetched = await repo.get_job(job.job_id)
    assert refetched is not None
    assert refetched.status == JobStatus.PROCESSING
    assert refetched.error_message is None


@pytest.mark.asyncio
async def test_update_job_status_failed_records_error(session):
    repo = JobRepository(session)
    batch = _make_batch()
    job = _make_job(batch.batch_id)
    await repo.create_batch(batch, [job])

    await repo.update_job_status(
        job.job_id, JobStatus.FAILED, error_message="boom"
    )
    refetched = await repo.get_job(job.job_id)
    assert refetched is not None
    assert refetched.status == JobStatus.FAILED
    assert refetched.error_message == "boom"


@pytest.mark.asyncio
async def test_update_job_status_with_result_payload(session):
    repo = JobRepository(session)
    batch = _make_batch()
    job = _make_job(batch.batch_id)
    await repo.create_batch(batch, [job])

    payload = {"drawing_number": "L-001"}
    await repo.update_job_status(
        job.job_id, JobStatus.COMPLETED, result=payload
    )
    refetched = await repo.get_job(job.job_id)
    assert refetched is not None
    assert refetched.status == JobStatus.COMPLETED
    assert refetched.result == payload


@pytest.mark.asyncio
async def test_update_job_result_persists_all_fields(session):
    repo = JobRepository(session)
    batch = _make_batch()
    job = _make_job(batch.batch_id)
    await repo.create_batch(batch, [job])

    result_payload = {"drawing_number": "L-001", "title": "Plan"}
    pipeline_trace = {"stage1": {"duration_ms": 42}}
    await repo.update_job_result(
        job.job_id,
        result=result_payload,
        confidence_tier=ConfidenceTier.AUTO,
        confidence_score=0.91,
        processing_time_ms=12345,
        otel_trace_id="trace-xyz",
        pipeline_trace=pipeline_trace,
    )

    refetched = await repo.get_job(job.job_id)
    assert refetched is not None
    assert refetched.result == result_payload
    assert refetched.pipeline_trace == pipeline_trace
    assert refetched.confidence_tier == ConfidenceTier.AUTO
    # confidence_score is stored as Numeric(4,3) — within float tolerance
    assert refetched.overall_confidence == pytest.approx(0.91, abs=1e-3)
    assert refetched.processing_time_ms == 12345
    assert refetched.otel_trace_id == "trace-xyz"
    # status must be COMPLETED after update_job_result
    assert refetched.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_update_job_result_returns_none_for_missing(session):
    repo = JobRepository(session)
    out = await repo.update_job_result(
        uuid4(),
        result={},
        confidence_tier=ConfidenceTier.AUTO,
        confidence_score=1.0,
        processing_time_ms=0,
    )
    assert out is None


@pytest.mark.asyncio
async def test_update_batch_status_persists_change(session):
    repo = JobRepository(session)
    batch = _make_batch()
    job = _make_job(batch.batch_id)
    await repo.create_batch(batch, [job])

    updated = await repo.update_batch_status(batch.batch_id, JobStatus.COMPLETED)
    assert updated is not None
    assert updated.status == JobStatus.COMPLETED

    refetched = await repo.get_batch(batch.batch_id)
    assert refetched is not None
    assert refetched.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_get_job_by_file_hash_returns_completed_job(session):
    repo = JobRepository(session)
    batch = _make_batch()
    job = _make_job(batch.batch_id, file_hash="dedup_hash" + "0" * 54)
    await repo.create_batch(batch, [job])
    await repo.update_job_result(
        job.job_id,
        result={"x": 1},
        confidence_tier=ConfidenceTier.AUTO,
        confidence_score=0.95,
        processing_time_ms=100,
    )

    dup = await repo.get_job_by_file_hash("dedup_hash" + "0" * 54)
    assert dup is not None
    assert dup.job_id == job.job_id


@pytest.mark.asyncio
async def test_get_job_by_file_hash_skips_non_completed(session):
    repo = JobRepository(session)
    batch = _make_batch()
    job = _make_job(batch.batch_id, file_hash="pending" + "0" * 57)
    await repo.create_batch(batch, [job])
    # Job is still QUEUED — should not be returned for dedup
    assert await repo.get_job_by_file_hash("pending" + "0" * 57) is None


@pytest.mark.asyncio
async def test_get_job_by_file_hash_returns_none_for_unknown(session):
    repo = JobRepository(session)
    assert await repo.get_job_by_file_hash("nonexistent" + "0" * 53) is None


@pytest.mark.asyncio
async def test_find_by_file_hash_alias_works(session):
    """Backwards-compatible alias must mirror the canonical method."""
    repo = JobRepository(session)
    batch = _make_batch()
    job = _make_job(batch.batch_id, file_hash="alias_hash" + "0" * 54)
    await repo.create_batch(batch, [job])
    await repo.update_job_result(
        job.job_id,
        result={},
        confidence_tier=ConfidenceTier.AUTO,
        confidence_score=0.9,
        processing_time_ms=10,
    )
    via_alias = await repo.find_by_file_hash("alias_hash" + "0" * 54)
    via_canonical = await repo.get_job_by_file_hash("alias_hash" + "0" * 54)
    assert via_alias is not None
    assert via_canonical is not None
    assert via_alias.job_id == via_canonical.job_id


@pytest.mark.asyncio
async def test_list_pending_reviews_returns_only_review_tier(session):
    repo = JobRepository(session)
    batch = _make_batch(total_jobs=3)
    j_review = _make_job(batch.batch_id, file_hash="r" * 64, filename="review.pdf")
    j_auto = _make_job(batch.batch_id, file_hash="a" * 64, filename="auto.pdf")
    j_queued = _make_job(batch.batch_id, file_hash="q" * 64, filename="queued.pdf")
    await repo.create_batch(batch, [j_review, j_auto, j_queued])

    await repo.update_job_result(
        j_review.job_id,
        result={},
        confidence_tier=ConfidenceTier.REVIEW,
        confidence_score=0.3,
        processing_time_ms=1,
    )
    await repo.update_job_result(
        j_auto.job_id,
        result={},
        confidence_tier=ConfidenceTier.AUTO,
        confidence_score=0.95,
        processing_time_ms=1,
    )

    items, total = await repo.list_pending_reviews(page=1, per_page=10)
    assert total == 1
    assert len(items) == 1
    assert items[0].job_id == j_review.job_id
    assert items[0].confidence_tier == ConfidenceTier.REVIEW


@pytest.mark.asyncio
async def test_get_pending_reviews_returns_paginated_result(session):
    repo = JobRepository(session)
    batch = _make_batch(total_jobs=3)
    jobs = [
        _make_job(batch.batch_id, file_hash=f"{i:064d}", filename=f"r{i}.pdf")
        for i in range(3)
    ]
    await repo.create_batch(batch, jobs)
    for j in jobs:
        await repo.update_job_result(
            j.job_id,
            result={},
            confidence_tier=ConfidenceTier.REVIEW,
            confidence_score=0.1,
            processing_time_ms=1,
        )

    page = await repo.get_pending_reviews(page=1, per_page=2)
    assert page.total == 3
    assert page.per_page == 2
    assert page.page == 1
    assert page.total_pages == 2
    assert len(page.items) == 2


@pytest.mark.asyncio
async def test_list_pending_reviews_excludes_rejected(session):
    repo = JobRepository(session)
    batch = _make_batch()
    job = _make_job(batch.batch_id)
    await repo.create_batch(batch, [job])
    await repo.update_job_result(
        job.job_id,
        result={},
        confidence_tier=ConfidenceTier.REVIEW,
        confidence_score=0.2,
        processing_time_ms=1,
    )
    await repo.update_job_status(job.job_id, JobStatus.REJECTED)

    items, total = await repo.list_pending_reviews()
    assert total == 0
    assert items == []


@pytest.mark.asyncio
async def test_create_review_action_persists(session):
    repo = JobRepository(session)
    batch = _make_batch()
    job = _make_job(batch.batch_id)
    await repo.create_batch(batch, [job])

    review = ReviewAction(
        review_action_id=str(uuid4()),
        job_id=job.job_id,
        action="approve",
        reviewed_by="reviewer-1",
        reason=None,
        corrections={"title": "Corrected"},
        created_at=datetime.now(timezone.utc),
    )
    saved = await repo.create_review_action(review)
    assert saved.action == "approve"
    assert saved.corrections == {"title": "Corrected"}


@pytest.mark.asyncio
async def test_list_jobs_by_batch_returns_all(session):
    repo = JobRepository(session)
    batch = _make_batch(total_jobs=3)
    jobs = [
        _make_job(batch.batch_id, file_hash=f"{i:064d}", filename=f"j{i}.pdf")
        for i in range(3)
    ]
    await repo.create_batch(batch, jobs)

    listed = await repo.list_jobs_by_batch(batch.batch_id)
    assert len(listed) == 3
    assert {j.job_id for j in listed} == {j.job_id for j in jobs}


@pytest.mark.asyncio
async def test_list_jobs_by_batch_returns_empty_for_unknown(session):
    repo = JobRepository(session)
    assert await repo.list_jobs_by_batch(uuid4()) == []
