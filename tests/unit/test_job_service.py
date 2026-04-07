"""Unit tests for :class:`JobService` (CAP-009).

These tests exercise ``submit_batch``, ``get_batch``, and ``get_job`` with
in-memory fakes for the repository, task queue, and PDF processor so the
service logic is validated without touching Postgres, Celery, or
PyMuPDF.

The tests verify state AFTER mutation: every new job must be persisted,
every new job must have its bytes written to temp storage, and every new
job must be enqueued exactly once. Duplicate-detection is verified both
for the "duplicate is returned from cache" path and for the "not
duplicate because still QUEUED" path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from zubot_ingestion.domain.entities import Batch, Job, ReviewAction
from zubot_ingestion.domain.enums import ExtractionMode, JobStatus
from zubot_ingestion.services.job_service import (
    DEFAULT_TEMP_STORAGE_ROOT,
    InvalidPDFError,
    JobService,
    MAX_PDF_BYTES,
    OversizeFileError,
)
from zubot_ingestion.shared.types import (
    AuthContext,
    BatchId,
    FileHash,
    JobId,
    SubmissionParams,
    UploadedFile,
)


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class FakeRepository:
    """Simple in-memory IJobRepository fake."""

    def __init__(self) -> None:
        self.batches: dict[UUID, Batch] = {}
        self.jobs: dict[UUID, Job] = {}
        self.by_hash: dict[str, Job] = {}
        self.create_batch_calls: list[tuple[Batch, list[Job]]] = []

    async def create_batch(self, batch: Batch, jobs: list[Job]) -> Batch:
        self.batches[batch.batch_id] = batch
        for j in jobs:
            self.jobs[j.job_id] = j
        self.create_batch_calls.append((batch, list(jobs)))
        return batch

    async def get_batch(self, batch_id: UUID) -> Batch | None:
        return self.batches.get(batch_id)

    async def get_batch_with_jobs(self, batch_id: UUID):  # type: ignore[no-untyped-def]
        batch = self.batches.get(batch_id)
        if batch is None:
            return None
        jobs = [j for j in self.jobs.values() if j.batch_id == batch_id]
        return (batch, jobs)

    async def get_job(self, job_id: UUID) -> Job | None:
        return self.jobs.get(job_id)

    async def get_job_by_file_hash(self, file_hash: str) -> Job | None:
        return self.by_hash.get(file_hash)

    async def update_job_status(
        self,
        job_id: UUID,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        return None

    async def get_pending_reviews(self, page: int, per_page: int):  # type: ignore[no-untyped-def]
        return None

    async def create_review_action(self, review_action: ReviewAction) -> ReviewAction:
        return review_action

    # Test-only helper
    def seed_completed(self, file_hash: str, filename: str = "old.pdf") -> Job:
        job_id = JobId(uuid4())
        batch_id = BatchId(uuid4())
        now = datetime.now(timezone.utc)
        job = Job(
            job_id=job_id,
            batch_id=batch_id,
            filename=filename,
            file_hash=FileHash(file_hash),
            file_path=f"/tmp/old/{job_id}.pdf",
            status=JobStatus.COMPLETED,
            result={"drawing_number": "L-001"},
            error_message=None,
            pipeline_trace=None,
            otel_trace_id=None,
            processing_time_ms=123,
            created_at=now,
            updated_at=now,
        )
        self.jobs[job_id] = job
        self.by_hash[file_hash] = job
        return job

    def seed_queued(self, file_hash: str) -> Job:
        job_id = JobId(uuid4())
        batch_id = BatchId(uuid4())
        now = datetime.now(timezone.utc)
        job = Job(
            job_id=job_id,
            batch_id=batch_id,
            filename="queued.pdf",
            file_hash=FileHash(file_hash),
            file_path=f"/tmp/queued/{job_id}.pdf",
            status=JobStatus.QUEUED,
            result=None,
            error_message=None,
            pipeline_trace=None,
            otel_trace_id=None,
            processing_time_ms=None,
            created_at=now,
            updated_at=now,
        )
        self.jobs[job_id] = job
        self.by_hash[file_hash] = job
        return job


class FakeTaskQueue:
    """In-memory ITaskQueue fake that records every enqueue call."""

    def __init__(self) -> None:
        self.enqueued: list[UUID] = []

    def enqueue_extraction(self, job_id: UUID) -> str:
        self.enqueued.append(job_id)
        return f"task-{job_id}"

    def get_task_status(self, task_id: str):  # type: ignore[no-untyped-def]
        return None


class FakePDFProcessor:
    """No-op IPDFProcessor fake. The service doesn't actually invoke it
    during ``submit_batch`` (validation is done on raw bytes), so the
    fake only needs to exist."""

    def load(self, pdf_bytes: bytes):  # type: ignore[no-untyped-def]
        return None

    def extract_text(self, pdf_bytes: bytes) -> str:
        return ""

    def render_page(self, pdf_bytes: bytes, page_number: int, dpi: int = 200, scale: float = 2.0):  # type: ignore[no-untyped-def]
        return None

    def render_pages(self, pdf_bytes: bytes, page_numbers, dpi: int = 200, scale: float = 2.0):  # type: ignore[no-untyped-def]
        return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_context() -> AuthContext:
    return AuthContext(
        user_id="alice",
        auth_method="api_key",
        deployment_id=1,
        node_id=2,
    )


@pytest.fixture
def tmp_storage(tmp_path):
    return tmp_path / "zubot-ingestion"


@pytest.fixture
def service(tmp_storage):
    repo = FakeRepository()
    tq = FakeTaskQueue()
    pdf = FakePDFProcessor()
    svc = JobService(
        repository=repo,
        task_queue=tq,
        pdf_processor=pdf,
        temp_storage_root=tmp_storage,
    )
    return svc, repo, tq, pdf


def _pdf_bytes(extra: bytes = b"") -> bytes:
    """Return a minimal byte string that passes the PDF magic-byte check."""
    return b"%PDF-1.4\n" + extra + b"\n%%EOF\n"


def _upload(filename: str, content: bytes) -> UploadedFile:
    return UploadedFile(
        filename=filename,
        content=content,
        content_type="application/pdf",
    )


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_batch_rejects_empty_file_list(service, auth_context):
    svc, *_ = service
    with pytest.raises(InvalidPDFError):
        await svc.submit_batch(
            files=[],
            params=SubmissionParams(),
            auth_context=auth_context,
        )


@pytest.mark.asyncio
async def test_submit_batch_rejects_non_pdf_extension(service, auth_context):
    svc, *_ = service
    with pytest.raises(InvalidPDFError):
        await svc.submit_batch(
            files=[_upload("not-a-pdf.txt", _pdf_bytes())],
            params=SubmissionParams(),
            auth_context=auth_context,
        )


@pytest.mark.asyncio
async def test_submit_batch_rejects_file_with_wrong_magic_bytes(
    service, auth_context
):
    svc, *_ = service
    with pytest.raises(InvalidPDFError):
        await svc.submit_batch(
            files=[_upload("fake.pdf", b"not a pdf at all")],
            params=SubmissionParams(),
            auth_context=auth_context,
        )


@pytest.mark.asyncio
async def test_submit_batch_rejects_oversize_file(tmp_storage, auth_context):
    svc = JobService(
        repository=FakeRepository(),
        task_queue=FakeTaskQueue(),
        pdf_processor=FakePDFProcessor(),
        temp_storage_root=tmp_storage,
        max_pdf_bytes=1024,  # tiny cap
    )
    content = _pdf_bytes(b"x" * 2048)
    with pytest.raises(OversizeFileError):
        await svc.submit_batch(
            files=[_upload("big.pdf", content)],
            params=SubmissionParams(),
            auth_context=auth_context,
        )


# ---------------------------------------------------------------------------
# Success-path tests — verify state AFTER mutation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_batch_persists_new_jobs_and_enqueues_each_once(
    service, auth_context, tmp_storage
):
    svc, repo, tq, _pdf = service
    files = [
        _upload("one.pdf", _pdf_bytes(b"one")),
        _upload("two.pdf", _pdf_bytes(b"two")),
    ]
    result = await svc.submit_batch(
        files=files,
        params=SubmissionParams(),
        auth_context=auth_context,
    )

    # Response shape
    assert result.total == 2
    assert len(result.jobs) == 2
    assert result.poll_url == f"/batches/{result.batch_id}"

    # Repository persisted a batch and two jobs
    assert len(repo.create_batch_calls) == 1
    stored_batch, stored_jobs = repo.create_batch_calls[0]
    assert stored_batch.batch_id == result.batch_id
    assert stored_batch.created_by == "alice"
    # Default SubmissionParams carries no deployment/node id
    assert stored_batch.deployment_id is None
    assert stored_batch.node_id is None
    assert stored_batch.status == JobStatus.QUEUED
    assert stored_batch.mode == ExtractionMode.AUTO
    assert len(stored_jobs) == 2

    # Task queue received exactly one enqueue per new job
    stored_job_ids = {j.job_id for j in stored_jobs}
    assert set(tq.enqueued) == stored_job_ids
    assert len(tq.enqueued) == 2

    # Bytes written to temp storage
    batch_dir = tmp_storage / str(result.batch_id)
    assert batch_dir.exists()
    for j in stored_jobs:
        on_disk = batch_dir / f"{j.job_id}.pdf"
        assert on_disk.exists()
        assert on_disk.read_bytes().startswith(b"%PDF")


@pytest.mark.asyncio
async def test_submit_batch_deduplicates_completed_files(
    service, auth_context, tmp_storage
):
    svc, repo, tq, _pdf = service

    # Pre-seed a completed job for a specific file hash.
    content = _pdf_bytes(b"unique-file-a")
    import hashlib

    file_hash = hashlib.sha256(content).hexdigest()
    existing = repo.seed_completed(file_hash=file_hash, filename="old.pdf")

    result = await svc.submit_batch(
        files=[_upload("new-name.pdf", content)],
        params=SubmissionParams(),
        auth_context=auth_context,
    )

    # Exactly one summary, flagged CACHED and pointing at the old job
    assert result.total == 1
    assert len(result.jobs) == 1
    cached = result.jobs[0]
    assert cached.status == JobStatus.CACHED
    assert cached.job_id == existing.job_id
    assert cached.result == {"drawing_number": "L-001"}

    # No new job persisted, no task enqueued, no bytes written
    assert repo.create_batch_calls[0][1] == []  # empty new-job list
    assert tq.enqueued == []
    batch_dir = tmp_storage / str(result.batch_id)
    # batch dir is created but contains no job pdfs
    assert batch_dir.exists()
    assert list(batch_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_submit_batch_queued_job_is_not_deduplicated(
    service, auth_context, tmp_storage
):
    """A QUEUED (not COMPLETED) existing job must not trigger dedup."""
    svc, repo, tq, _pdf = service
    content = _pdf_bytes(b"queued-file")
    import hashlib

    file_hash = hashlib.sha256(content).hexdigest()
    repo.seed_queued(file_hash=file_hash)

    result = await svc.submit_batch(
        files=[_upload("new.pdf", content)],
        params=SubmissionParams(),
        auth_context=auth_context,
    )

    # A NEW job was created and enqueued (not deduplicated).
    assert result.jobs[0].status == JobStatus.QUEUED
    assert len(repo.create_batch_calls[0][1]) == 1
    assert len(tq.enqueued) == 1


@pytest.mark.asyncio
async def test_submit_batch_mixed_duplicate_and_new(
    service, auth_context, tmp_storage
):
    svc, repo, tq, _pdf = service
    dup_content = _pdf_bytes(b"already-ingested")
    new_content = _pdf_bytes(b"brand-new")
    import hashlib

    dup_hash = hashlib.sha256(dup_content).hexdigest()
    repo.seed_completed(file_hash=dup_hash, filename="orig.pdf")

    result = await svc.submit_batch(
        files=[
            _upload("dup.pdf", dup_content),
            _upload("new.pdf", new_content),
        ],
        params=SubmissionParams(),
        auth_context=auth_context,
    )

    assert result.total == 2
    statuses = [j.status for j in result.jobs]
    assert JobStatus.CACHED in statuses
    assert JobStatus.QUEUED in statuses
    # Only the new file was persisted and enqueued
    assert len(repo.create_batch_calls[0][1]) == 1
    assert len(tq.enqueued) == 1


@pytest.mark.asyncio
async def test_submit_batch_mode_is_passed_through(service, auth_context):
    svc, repo, _tq, _pdf = service
    params = SubmissionParams(
        mode=ExtractionMode.DRAWING,
        callback_url="https://example.test/cb",
        deployment_id=42,
        node_id=7,
    )
    await svc.submit_batch(
        files=[_upload("x.pdf", _pdf_bytes())],
        params=params,
        auth_context=auth_context,
    )
    stored_batch, _ = repo.create_batch_calls[0]
    assert stored_batch.mode == ExtractionMode.DRAWING
    assert stored_batch.callback_url == "https://example.test/cb"
    assert stored_batch.deployment_id == 42
    assert stored_batch.node_id == 7


@pytest.mark.asyncio
async def test_get_job_returns_job_detail(service, auth_context):
    svc, repo, _tq, _pdf = service
    content = _pdf_bytes()
    result = await svc.submit_batch(
        files=[_upload("a.pdf", content)],
        params=SubmissionParams(),
        auth_context=auth_context,
    )
    new_job_id = result.jobs[0].job_id
    detail = await svc.get_job(new_job_id, auth_context)
    assert detail is not None
    assert detail.job_id == new_job_id
    assert detail.filename == "a.pdf"
    assert detail.status == JobStatus.QUEUED


@pytest.mark.asyncio
async def test_get_job_returns_none_for_unknown(service, auth_context):
    svc, *_ = service
    assert await svc.get_job(uuid4(), auth_context) is None


@pytest.mark.asyncio
async def test_get_batch_returns_batch_with_jobs_dto(service, auth_context):
    svc, *_ = service
    result = await svc.submit_batch(
        files=[
            _upload("one.pdf", _pdf_bytes(b"a")),
            _upload("two.pdf", _pdf_bytes(b"b")),
        ],
        params=SubmissionParams(),
        auth_context=auth_context,
    )
    batch = await svc.get_batch(result.batch_id, auth_context)
    assert batch is not None
    assert batch.batch_id == result.batch_id
    assert batch.progress.total == 2
    # All jobs start QUEUED so "queued" counter matches total
    assert batch.progress.queued == 2
    assert batch.progress.completed == 0
    assert batch.progress.failed == 0


@pytest.mark.asyncio
async def test_get_batch_returns_none_for_unknown(service, auth_context):
    svc, *_ = service
    assert await svc.get_batch(uuid4(), auth_context) is None


def test_default_storage_root_constant():
    from pathlib import Path

    assert DEFAULT_TEMP_STORAGE_ROOT == Path("/tmp/zubot-ingestion")


def test_max_pdf_bytes_constant():
    assert MAX_PDF_BYTES == 100 * 1024 * 1024
