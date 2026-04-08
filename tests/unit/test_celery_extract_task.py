"""Unit tests for the Celery extract_document_task body.

These tests exercise the async helper :func:`_run_extract_document_task`
directly rather than going through Celery's eager-mode shim. The goal is
to prove that the state mutations on the repository actually match
expectations AFTER the task runs — per the worker-agent guidance:

    "for any function that mutates persistent state, write at least one
    test that verifies the state AFTER mutation matches expectations."

NOTE: task-5 converted ``get_job_repository`` from a bare factory into an
``@asynccontextmanager``, and task-6 switched the Celery task COMPLETED
success path from ``update_job_status(result=...)`` to
``update_job_result(...)``. The tests below are written against those new
contracts.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from unittest.mock import patch

import pytest

from zubot_ingestion.domain.entities import (
    ConfidenceAssessment,
    ExtractionResult,
    Job,
    PipelineResult,
    SidecarDocument,
)
from zubot_ingestion.domain.enums import (
    ConfidenceTier,
    DocumentType,
    JobStatus,
)
from zubot_ingestion.services import celery_app as celery_module
from zubot_ingestion.services.celery_app import (
    _mark_job_failed,
    _run_extract_document_task,
)
from zubot_ingestion.shared.types import BatchId, FileHash, JobId


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeRepository:
    """In-memory repository that records every update call.

    Supports both ``update_job_status`` (used for the PROCESSING marker
    and the REVIEW terminal path) and ``update_job_result`` (used on the
    COMPLETED success path per the task-6 indexed-column fix).
    """

    def __init__(self, job: Job) -> None:
        self._job = job
        self.update_calls: list[dict[str, Any]] = []
        self.update_result_calls: list[dict[str, Any]] = []

    async def get_job(self, job_id: UUID) -> Job | None:
        if job_id == self._job.job_id:
            return self._job
        return None

    async def get_batch(self, batch_id: UUID) -> None:
        # Returning None is the documented "tolerated missing batch" path:
        # the task will catch and log, then call run_pipeline with both
        # deployment_id/node_id as None.
        return None

    async def update_job_status(
        self,
        job_id: UUID,
        *,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        self.update_calls.append(
            {
                "job_id": job_id,
                "status": status,
                "result": result,
                "error_message": error_message,
            }
        )

    async def update_job_result(
        self,
        job_id: UUID,
        *,
        result: dict[str, Any],
        confidence_tier: ConfidenceTier,
        confidence_score: float,
        processing_time_ms: int,
        otel_trace_id: str | None,
        pipeline_trace: dict[str, Any] | None,
    ) -> None:
        """Record a call to the indexed-column success-path write.

        Task-6 switched the COMPLETED success path from
        ``update_job_status(result=...)`` (which left the indexed columns
        NULL) to this method (which populates confidence_tier /
        confidence_score / processing_time_ms / otel_trace_id /
        pipeline_trace as first-class columns so the review-queue
        pagination and Prometheus tier metrics are no longer blind).
        """
        self.update_result_calls.append(
            {
                "job_id": job_id,
                "result": result,
                "confidence_tier": confidence_tier,
                "confidence_score": confidence_score,
                "processing_time_ms": processing_time_ms,
                "otel_trace_id": otel_trace_id,
                "pipeline_trace": pipeline_trace,
            }
        )


def _patch_repo_factory(repo: FakeRepository):
    """Build a patch that replaces ``get_job_repository`` with an async CM.

    Task-5 converted ``get_job_repository`` from a synchronous factory
    into an ``@asynccontextmanager`` that yields a repo bound to a fresh
    async session. This helper builds a ``patch`` whose ``side_effect``
    produces a new async context manager each time the factory is
    called, so ``async with get_job_repository() as repo:`` blocks in
    ``celery_app.py`` correctly yield the test double.
    """

    @asynccontextmanager
    async def _cm():
        yield repo

    return patch(
        "zubot_ingestion.services.get_job_repository",
        side_effect=lambda: _cm(),
    )


class FakeOrchestrator:
    """Returns a pre-canned PipelineResult when run_pipeline is called."""

    def __init__(self, pipeline_result: PipelineResult) -> None:
        self._result = pipeline_result
        self.calls: int = 0
        self.last_job: Job | None = None
        self.last_pdf_bytes: bytes | None = None
        self.last_deployment_id: int | None = None
        self.last_node_id: int | None = None
        self.last_callback_url: str | None = None

    async def run_pipeline(
        self,
        job: Job,
        pdf_bytes: bytes,
        *,
        deployment_id: int | None = None,
        node_id: int | None = None,
        callback_url: str | None = None,
    ) -> PipelineResult:
        self.calls += 1
        self.last_job = job
        self.last_pdf_bytes = pdf_bytes
        self.last_deployment_id = deployment_id
        self.last_node_id = node_id
        self.last_callback_url = callback_url
        return self._result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_job(batch_id: UUID | None = None) -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        job_id=JobId(uuid4()),
        batch_id=BatchId(batch_id or uuid4()),
        filename="test.pdf",
        file_hash=FileHash("a" * 64),
        file_path="/tmp/test.pdf",
        status=JobStatus.QUEUED,
        result=None,
        error_message=None,
        pipeline_trace=None,
        otel_trace_id=None,
        processing_time_ms=None,
        created_at=now,
        updated_at=now,
    )


def _build_pipeline_result(
    *,
    score: float,
    tier: ConfidenceTier,
    document_type: DocumentType | None = DocumentType.TECHNICAL_DRAWING,
) -> PipelineResult:
    extraction = ExtractionResult(
        drawing_number="X-001",
        drawing_number_confidence=0.9,
        title="Test Title",
        title_confidence=0.8,
        document_type=document_type,
        document_type_confidence=0.85,
        sources_used=["drawing_number", "title", "document_type"],
        confidence_score=score,
        confidence_tier=tier,
    )
    assessment = ConfidenceAssessment(
        overall_confidence=score,
        tier=tier,
        breakdown={
            "drawing_number": 0.36,
            "title": 0.24,
            "document_type": 0.255,
            "validation_penalty": 0.0,
        },
        validation_adjustment=0.0,
    )
    sidecar = SidecarDocument(
        metadata_attributes={
            "source_filename": "test.pdf",
            "document_type": "technical_drawing",
            "extraction_confidence": score,
        },
        companion_text=None,
        source_filename="test.pdf",
        file_hash=FileHash("a" * 64),
    )
    return PipelineResult(
        extraction_result=extraction,
        companion_result=None,
        sidecar=sidecar,
        confidence_assessment=assessment,
        errors=[],
        pipeline_trace={"stages": {"pdf_load": {"duration_ms": 1, "ok": True}}},
        otel_trace_id=None,
    )


# ---------------------------------------------------------------------------
# Happy-path tests — verify state AFTER mutation
# ---------------------------------------------------------------------------


async def test_run_extract_document_task_persists_completed_for_auto_tier(
    tmp_path: Path,
) -> None:
    """AUTO tier → status becomes COMPLETED via ``update_job_result``.

    After task-6, the terminal success write populates the indexed
    columns through ``update_job_result`` instead of stuffing everything
    into the JSONB blob via ``update_job_status(result=...)``.
    """
    job = _build_job()
    pdf_path = tmp_path / str(job.batch_id) / f"{job.job_id}.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    repo = FakeRepository(job)
    pipeline_result = _build_pipeline_result(
        score=0.85, tier=ConfidenceTier.AUTO
    )
    orchestrator = FakeOrchestrator(pipeline_result)

    with patch.object(celery_module, "TEMP_PDF_ROOT", tmp_path), _patch_repo_factory(
        repo
    ), patch(
        "zubot_ingestion.services.build_orchestrator", return_value=orchestrator
    ):
        result = await _run_extract_document_task(job.job_id)

    # Return value
    assert result["job_id"] == str(job.job_id)
    assert result["status"] == "completed"
    assert result["confidence_tier"] == "auto"
    assert result["confidence_score"] == pytest.approx(0.85)

    # Exactly one status write — the PROCESSING marker. The terminal
    # success write now goes through ``update_job_result`` instead.
    assert len(repo.update_calls) == 1
    first = repo.update_calls[0]
    assert first["status"] == JobStatus.PROCESSING
    assert first["result"] is None
    assert first["error_message"] is None

    # Exactly one update_job_result call — the terminal COMPLETED write.
    # Must populate all five indexed columns from the pipeline result.
    assert len(repo.update_result_calls) == 1
    terminal = repo.update_result_calls[0]
    assert terminal["confidence_tier"] == ConfidenceTier.AUTO
    assert terminal["confidence_score"] == pytest.approx(0.85)
    assert terminal["processing_time_ms"] is not None
    assert terminal["processing_time_ms"] >= 0
    assert terminal["otel_trace_id"] is None
    assert terminal["pipeline_trace"] is not None

    payload = terminal["result"]
    assert payload["drawing_number"] == "X-001"
    assert payload["title"] == "Test Title"
    assert payload["document_type"] == "technical_drawing"
    assert payload["confidence_score"] == pytest.approx(0.85)
    assert payload["confidence_tier"] == "auto"
    assert "sidecar" in payload
    assert "pipeline_trace" in payload

    # Orchestrator received the right job and the PDF bytes from disk
    assert orchestrator.calls == 1
    assert orchestrator.last_job is job
    assert orchestrator.last_pdf_bytes == b"%PDF-1.4 fake"


async def test_run_extract_document_task_persists_review_tier_as_review_status(
    tmp_path: Path,
) -> None:
    """REVIEW tier → status becomes REVIEW via ``update_job_status``.

    The REVIEW path deliberately stays on ``update_job_status`` because
    ``update_job_result`` hard-codes status=COMPLETED in the repository.
    """
    job = _build_job()
    pdf_path = tmp_path / str(job.batch_id) / f"{job.job_id}.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    repo = FakeRepository(job)
    pipeline_result = _build_pipeline_result(
        score=0.30, tier=ConfidenceTier.REVIEW
    )
    orchestrator = FakeOrchestrator(pipeline_result)

    with patch.object(celery_module, "TEMP_PDF_ROOT", tmp_path), _patch_repo_factory(
        repo
    ), patch(
        "zubot_ingestion.services.build_orchestrator", return_value=orchestrator
    ):
        result = await _run_extract_document_task(job.job_id)

    assert result["status"] == "review"
    # Final status in repo is REVIEW — written via update_job_status
    final = repo.update_calls[-1]
    assert final["status"] == JobStatus.REVIEW
    # update_job_result must NOT be called on the REVIEW path
    assert repo.update_result_calls == []


async def test_run_extract_document_task_persists_spot_tier_as_completed(
    tmp_path: Path,
) -> None:
    """SPOT tier → COMPLETED via ``update_job_result`` (auto-publish with spot-check flag)."""
    job = _build_job()
    pdf_path = tmp_path / str(job.batch_id) / f"{job.job_id}.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    repo = FakeRepository(job)
    pipeline_result = _build_pipeline_result(
        score=0.65, tier=ConfidenceTier.SPOT
    )
    orchestrator = FakeOrchestrator(pipeline_result)

    with patch.object(celery_module, "TEMP_PDF_ROOT", tmp_path), _patch_repo_factory(
        repo
    ), patch(
        "zubot_ingestion.services.build_orchestrator", return_value=orchestrator
    ):
        result = await _run_extract_document_task(job.job_id)

    assert result["status"] == "completed"
    # Only the PROCESSING marker goes through update_job_status now.
    assert len(repo.update_calls) == 1
    assert repo.update_calls[0]["status"] == JobStatus.PROCESSING
    # Terminal write lands on update_job_result.
    assert len(repo.update_result_calls) == 1
    assert repo.update_result_calls[0]["confidence_tier"] == ConfidenceTier.SPOT


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


async def test_run_extract_document_task_raises_lookup_error_when_job_missing(
    tmp_path: Path,
) -> None:
    """Missing job → LookupError propagates, no mutation happens."""
    job = _build_job()
    repo = FakeRepository(job)
    orchestrator = FakeOrchestrator(
        _build_pipeline_result(score=0.85, tier=ConfidenceTier.AUTO)
    )
    bogus_id = uuid4()

    with patch.object(celery_module, "TEMP_PDF_ROOT", tmp_path), _patch_repo_factory(
        repo
    ), patch(
        "zubot_ingestion.services.build_orchestrator", return_value=orchestrator
    ):
        with pytest.raises(LookupError, match="not found"):
            await _run_extract_document_task(bogus_id)

    assert orchestrator.calls == 0
    assert repo.update_calls == []
    assert repo.update_result_calls == []


async def test_mark_job_failed_persists_failed_status_with_error_message() -> None:
    """_mark_job_failed must update the repo to FAILED with the error text.

    Task-6 changed the helper signature from ``(repo, job_id, exc)`` to
    ``(job_id, exc)`` — the helper now opens its own ``async with
    get_job_repository() as repo:`` block internally so the cleanup path
    is self-contained and never shares a session with the main pipeline
    body.
    """
    job = _build_job()
    repo = FakeRepository(job)
    exc = ValueError("boom")

    with _patch_repo_factory(repo):
        await _mark_job_failed(job.job_id, exc)

    assert len(repo.update_calls) == 1
    call = repo.update_calls[0]
    assert call["status"] == JobStatus.FAILED
    assert call["result"] is None
    assert call["error_message"] == "ValueError: boom"


async def test_run_extract_document_task_missing_pdf_file_raises(
    tmp_path: Path,
) -> None:
    """If the temp PDF file is missing the task raises FileNotFoundError."""
    job = _build_job()
    # Intentionally do NOT create the PDF file at the expected path.
    repo = FakeRepository(job)
    orchestrator = FakeOrchestrator(
        _build_pipeline_result(score=0.85, tier=ConfidenceTier.AUTO)
    )

    with patch.object(celery_module, "TEMP_PDF_ROOT", tmp_path), _patch_repo_factory(
        repo
    ), patch(
        "zubot_ingestion.services.build_orchestrator", return_value=orchestrator
    ):
        with pytest.raises(FileNotFoundError):
            await _run_extract_document_task(job.job_id)

    # Orchestrator was never called because the file read fails first.
    assert orchestrator.calls == 0
    # The PROCESSING marker was written but no terminal write happened.
    assert repo.update_result_calls == []
