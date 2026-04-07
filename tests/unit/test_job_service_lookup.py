"""Unit tests for JobService.get_batch and JobService.get_job (CAP-010 / CAP-011).

Exercises the service against in-memory fake collaborators so we can
verify the progress aggregation, batch-status derivation, and the
NotFoundError contract without spinning up Postgres / Celery.

These tests complement the integration tests in
``tests/integration/test_batches_route.py`` and ``test_jobs_route.py``,
which exercise the same code paths via the FastAPI route layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from zubot_ingestion.domain.entities import Batch, Job
from zubot_ingestion.domain.enums import ExtractionMode, JobStatus
from zubot_ingestion.services.job_service import (
    JobService,
    NotFoundError,
    _aggregate_batch_status,
    _compute_progress,
)
from zubot_ingestion.shared.types import (
    AuthContext,
    BatchId,
    BatchWithJobs,
    FileHash,
    JobDetail,
    JobId,
    JobSummary,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_auth() -> AuthContext:
    return AuthContext(
        user_id="tester",
        auth_method="api_key",
        deployment_id=None,
        node_id=None,
    )


def _make_job(
    job_id: UUID,
    batch_id: UUID,
    status: JobStatus = JobStatus.QUEUED,
    filename: str = "doc.pdf",
    result: dict[str, Any] | None = None,
    pipeline_trace: dict[str, Any] | None = None,
    otel_trace_id: str | None = None,
    processing_time_ms: int | None = None,
    error_message: str | None = None,
) -> Job:
    return Job(
        job_id=JobId(job_id),
        batch_id=BatchId(batch_id),
        filename=filename,
        file_hash=FileHash("c" * 64),
        file_path=f"/tmp/{job_id}.pdf",
        status=status,
        result=result,
        error_message=error_message,
        pipeline_trace=pipeline_trace,
        otel_trace_id=otel_trace_id,
        processing_time_ms=processing_time_ms,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _make_batch(batch_id: UUID, status: JobStatus = JobStatus.QUEUED) -> Batch:
    return Batch(
        batch_id=BatchId(batch_id),
        status=status,
        total_jobs=0,
        callback_url=None,
        deployment_id=None,
        node_id=None,
        created_by="tester",
        created_at=_NOW,
        updated_at=_NOW,
        mode=ExtractionMode.AUTO,
    )


class FakeRepository:
    """Minimal in-memory IJobRepository.

    The service uses ``get_batch_with_jobs`` and ``get_job``; this fake
    returns the canonical ``tuple[Batch, list[Job]] | None`` shape so we
    can verify the service correctly derives the BatchWithJobs DTO.
    """

    def __init__(self) -> None:
        self.batches: dict[UUID, Batch] = {}
        self.jobs_by_batch: dict[UUID, list[Job]] = {}
        self.jobs_by_id: dict[UUID, Job] = {}

    def add_batch(self, batch: Batch, jobs: list[Job]) -> None:
        self.batches[batch.batch_id] = batch
        self.jobs_by_batch[batch.batch_id] = list(jobs)
        for j in jobs:
            self.jobs_by_id[j.job_id] = j

    async def get_batch_with_jobs(self, batch_id: UUID):  # type: ignore[no-untyped-def]
        batch = self.batches.get(batch_id)
        if batch is None:
            return None
        return batch, self.jobs_by_batch.get(batch_id, [])

    async def get_job(self, job_id: UUID):  # type: ignore[no-untyped-def]
        return self.jobs_by_id.get(job_id)

    # Unused stubs to satisfy the protocol shape.
    async def create_batch(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def get_batch(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def get_job_by_file_hash(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def update_job_status(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        return None


class _NullTaskQueue:
    def enqueue_extraction(self, job_id: UUID) -> str:  # pragma: no cover
        return "task"

    def get_task_status(self, task_id: str):  # pragma: no cover
        raise NotImplementedError


class _NullPDFProcessor:
    def load(self, pdf_bytes: bytes):  # pragma: no cover
        raise NotImplementedError

    def extract_text(self, pdf_bytes: bytes) -> str:  # pragma: no cover
        return ""

    def render_page(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    def render_pages(self, *args: Any, **kwargs: Any):  # pragma: no cover
        return []


def _make_service(repo: FakeRepository) -> JobService:
    return JobService(
        repository=repo,  # type: ignore[arg-type]
        task_queue=_NullTaskQueue(),  # type: ignore[arg-type]
        pdf_processor=_NullPDFProcessor(),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# _compute_progress unit tests
# ---------------------------------------------------------------------------


class TestComputeProgress:
    def test_empty_list_zeroes_all_counters(self) -> None:
        progress = _compute_progress([])
        assert progress.completed == 0
        assert progress.queued == 0
        assert progress.failed == 0
        assert progress.in_progress == 0
        assert progress.total == 0

    def test_all_buckets_counted_independently(self) -> None:
        statuses = [
            JobStatus.QUEUED,
            JobStatus.QUEUED,
            JobStatus.PROCESSING,
            JobStatus.PROCESSING,
            JobStatus.PROCESSING,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
        ]
        progress = _compute_progress(statuses)
        assert progress.queued == 2
        assert progress.in_progress == 3
        assert progress.completed == 1
        assert progress.failed == 1
        assert progress.total == 7

    def test_cached_jobs_count_as_completed(self) -> None:
        statuses = [JobStatus.CACHED, JobStatus.CACHED, JobStatus.COMPLETED]
        progress = _compute_progress(statuses)
        assert progress.completed == 3
        assert progress.total == 3


# ---------------------------------------------------------------------------
# _aggregate_batch_status unit tests
# ---------------------------------------------------------------------------


class TestAggregateBatchStatus:
    def test_empty_batch_is_queued(self) -> None:
        assert _aggregate_batch_status([]) == JobStatus.QUEUED

    def test_all_queued_is_queued(self) -> None:
        assert _aggregate_batch_status([JobStatus.QUEUED, JobStatus.QUEUED]) == JobStatus.QUEUED

    def test_any_processing_is_processing(self) -> None:
        statuses = [JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.COMPLETED]
        assert _aggregate_batch_status(statuses) == JobStatus.PROCESSING

    def test_processing_dominates_failed(self) -> None:
        statuses = [JobStatus.PROCESSING, JobStatus.FAILED]
        assert _aggregate_batch_status(statuses) == JobStatus.PROCESSING

    def test_mixed_queued_and_completed_is_processing(self) -> None:
        statuses = [JobStatus.QUEUED, JobStatus.COMPLETED]
        assert _aggregate_batch_status(statuses) == JobStatus.PROCESSING

    def test_failed_with_no_inflight_is_failed(self) -> None:
        statuses = [JobStatus.COMPLETED, JobStatus.FAILED]
        assert _aggregate_batch_status(statuses) == JobStatus.FAILED

    def test_all_completed_is_completed(self) -> None:
        statuses = [JobStatus.COMPLETED, JobStatus.COMPLETED]
        assert _aggregate_batch_status(statuses) == JobStatus.COMPLETED

    def test_completed_plus_cached_is_completed(self) -> None:
        statuses = [JobStatus.COMPLETED, JobStatus.CACHED]
        assert _aggregate_batch_status(statuses) == JobStatus.COMPLETED


# ---------------------------------------------------------------------------
# JobService.get_batch tests
# ---------------------------------------------------------------------------


class TestGetBatch:
    @pytest.mark.asyncio
    async def test_returns_batch_with_aggregated_progress_and_status(self) -> None:
        repo = FakeRepository()
        batch_id = uuid4()
        batch = _make_batch(batch_id, status=JobStatus.QUEUED)
        jobs = [
            _make_job(uuid4(), batch_id, status=JobStatus.QUEUED, filename="a.pdf"),
            _make_job(uuid4(), batch_id, status=JobStatus.PROCESSING, filename="b.pdf"),
            _make_job(uuid4(), batch_id, status=JobStatus.COMPLETED, filename="c.pdf"),
        ]
        repo.add_batch(batch, jobs)

        service = _make_service(repo)
        result = await service.get_batch(batch_id, _make_auth())

        assert isinstance(result, BatchWithJobs)
        assert result.batch_id == batch_id
        # Recomputed status (PROCESSING) should override the persisted
        # batch.status (QUEUED) — this is the "single source of truth"
        # behaviour CAP-010 mandates.
        assert result.status == JobStatus.PROCESSING
        assert result.progress.queued == 1
        assert result.progress.in_progress == 1
        assert result.progress.completed == 1
        assert result.progress.failed == 0
        assert result.progress.total == 3
        assert {j.filename for j in result.jobs} == {"a.pdf", "b.pdf", "c.pdf"}

    @pytest.mark.asyncio
    async def test_all_completed_yields_completed_status(self) -> None:
        repo = FakeRepository()
        batch_id = uuid4()
        batch = _make_batch(batch_id, status=JobStatus.QUEUED)
        jobs = [
            _make_job(uuid4(), batch_id, status=JobStatus.COMPLETED),
            _make_job(uuid4(), batch_id, status=JobStatus.COMPLETED),
        ]
        repo.add_batch(batch, jobs)

        result = await _make_service(repo).get_batch(batch_id, _make_auth())
        assert result.status == JobStatus.COMPLETED
        assert result.progress.completed == 2
        assert result.progress.total == 2

    @pytest.mark.asyncio
    async def test_failed_with_no_inflight_yields_failed_status(self) -> None:
        repo = FakeRepository()
        batch_id = uuid4()
        batch = _make_batch(batch_id, status=JobStatus.PROCESSING)
        jobs = [
            _make_job(uuid4(), batch_id, status=JobStatus.COMPLETED),
            _make_job(uuid4(), batch_id, status=JobStatus.FAILED),
        ]
        repo.add_batch(batch, jobs)

        result = await _make_service(repo).get_batch(batch_id, _make_auth())
        assert result.status == JobStatus.FAILED
        assert result.progress.failed == 1
        assert result.progress.completed == 1

    @pytest.mark.asyncio
    async def test_empty_batch_yields_queued_status(self) -> None:
        repo = FakeRepository()
        batch_id = uuid4()
        batch = _make_batch(batch_id)
        repo.add_batch(batch, [])

        result = await _make_service(repo).get_batch(batch_id, _make_auth())
        assert result.status == JobStatus.QUEUED
        assert result.progress.total == 0
        assert result.jobs == []

    @pytest.mark.asyncio
    async def test_missing_batch_raises_not_found(self) -> None:
        repo = FakeRepository()
        service = _make_service(repo)
        with pytest.raises(NotFoundError) as exc_info:
            await service.get_batch(uuid4(), _make_auth())
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_handles_repository_returning_batch_with_jobs_dto(self) -> None:
        """If the SQLAlchemy adapter returns a BatchWithJobs directly, the
        service must still recompute progress + status from the inner
        job summaries instead of trusting the persisted ``status`` field.
        """

        class _DTOAdapter:
            async def get_batch_with_jobs(self, batch_id: UUID):  # type: ignore[no-untyped-def]
                # Persisted as QUEUED but contains a PROCESSING job — the
                # service should recompute and return PROCESSING.
                return BatchWithJobs(
                    batch_id=BatchId(batch_id),
                    status=JobStatus.QUEUED,
                    progress=__import__("zubot_ingestion.shared.types", fromlist=["BatchProgress"]).BatchProgress(
                        completed=0, queued=0, failed=0, total=1, in_progress=0
                    ),
                    jobs=[
                        JobSummary(
                            job_id=JobId(uuid4()),
                            filename="x.pdf",
                            status=JobStatus.PROCESSING,
                            file_hash=FileHash("d" * 64),
                            result=None,
                        )
                    ],
                    callback_url=None,
                    deployment_id=None,
                    node_id=None,
                    created_at=_NOW,
                    updated_at=_NOW,
                )

            async def get_job(self, job_id: UUID):  # pragma: no cover
                return None

        service = JobService(
            repository=_DTOAdapter(),  # type: ignore[arg-type]
            task_queue=_NullTaskQueue(),  # type: ignore[arg-type]
            pdf_processor=_NullPDFProcessor(),  # type: ignore[arg-type]
        )
        result = await service.get_batch(uuid4(), _make_auth())
        assert result.status == JobStatus.PROCESSING
        assert result.progress.in_progress == 1
        assert result.progress.total == 1


# ---------------------------------------------------------------------------
# JobService.get_job tests
# ---------------------------------------------------------------------------


class TestGetJob:
    @pytest.mark.asyncio
    async def test_returns_full_job_detail(self) -> None:
        repo = FakeRepository()
        batch_id = uuid4()
        job_id = uuid4()
        batch = _make_batch(batch_id)
        job = _make_job(
            job_id,
            batch_id,
            status=JobStatus.COMPLETED,
            result={"drawing_number": "L-001"},
            pipeline_trace={"stage1": {"ms": 412}},
            otel_trace_id="trace-xyz",
            processing_time_ms=987,
        )
        repo.add_batch(batch, [job])

        service = _make_service(repo)
        result = await service.get_job(job_id, _make_auth())

        assert isinstance(result, JobDetail)
        assert result.job_id == job_id
        assert result.batch_id == batch_id
        assert result.status == JobStatus.COMPLETED
        assert result.result == {"drawing_number": "L-001"}
        assert result.pipeline_trace == {"stage1": {"ms": 412}}
        assert result.otel_trace_id == "trace-xyz"
        assert result.processing_time_ms == 987

    @pytest.mark.asyncio
    async def test_missing_job_raises_not_found(self) -> None:
        repo = FakeRepository()
        service = _make_service(repo)
        with pytest.raises(NotFoundError) as exc_info:
            await service.get_job(uuid4(), _make_auth())
        assert "not found" in str(exc_info.value).lower()
