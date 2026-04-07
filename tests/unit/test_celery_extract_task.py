"""Unit tests for the Celery extract_document_task body.

These tests exercise the async helper :func:`_run_extract_document_task`
directly rather than going through Celery's eager-mode shim. The goal is
to prove that the state mutations on the repository actually match
expectations AFTER the task runs — per the worker-agent guidance:

    "for any function that mutates persistent state, write at least one
    test that verifies the state AFTER mutation matches expectations."
"""

from __future__ import annotations

import asyncio
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
    """In-memory repository that records every update_job_status call."""

    def __init__(self, job: Job) -> None:
        self._job = job
        self.update_calls: list[dict[str, Any]] = []

    async def get_job(self, job_id: UUID) -> Job | None:
        if job_id == self._job.job_id:
            return self._job
        return None

    async def get_batch(self, batch_id: UUID) -> None:
        # FakeRepository is used by the celery task body which fetches the
        # parent batch to forward deployment_id/node_id to the orchestrator.
        # Returning None is the documented "tolerated missing batch" path:
        # the task will catch and log, then call run_pipeline with both
        # IDs as None. The FakeOrchestrator records the kwargs it receives
        # so callers can assert on them.
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


class FakeOrchestrator:
    """Returns a pre-canned PipelineResult when run_pipeline is called."""

    def __init__(self, pipeline_result: PipelineResult) -> None:
        self._result = pipeline_result
        self.calls: int = 0
        self.last_job: Job | None = None
        self.last_pdf_bytes: bytes | None = None
        self.last_deployment_id: int | None = None
        self.last_node_id: int | None = None

    async def run_pipeline(
        self,
        job: Job,
        pdf_bytes: bytes,
        *,
        deployment_id: int | None = None,
        node_id: int | None = None,
    ) -> PipelineResult:
        self.calls += 1
        self.last_job = job
        self.last_pdf_bytes = pdf_bytes
        self.last_deployment_id = deployment_id
        self.last_node_id = node_id
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
    """AUTO tier → status becomes COMPLETED and result payload is written."""
    job = _build_job()
    pdf_path = tmp_path / str(job.batch_id) / f"{job.job_id}.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    repo = FakeRepository(job)
    pipeline_result = _build_pipeline_result(
        score=0.85, tier=ConfidenceTier.AUTO
    )
    orchestrator = FakeOrchestrator(pipeline_result)

    with patch.object(celery_module, "TEMP_PDF_ROOT", tmp_path), patch(
        "zubot_ingestion.services.get_job_repository", return_value=repo
    ), patch(
        "zubot_ingestion.services.build_orchestrator", return_value=orchestrator
    ):
        result = await _run_extract_document_task(job.job_id)

    # Return value
    assert result["job_id"] == str(job.job_id)
    assert result["status"] == "completed"
    assert result["confidence_tier"] == "auto"
    assert result["confidence_score"] == pytest.approx(0.85)

    # Repository mutations — verify state AFTER the task runs
    assert len(repo.update_calls) == 2
    first, second = repo.update_calls

    # First call: mark PROCESSING before running pipeline
    assert first["status"] == JobStatus.PROCESSING
    assert first["result"] is None
    assert first["error_message"] is None

    # Second call: mark COMPLETED with the full result payload
    assert second["status"] == JobStatus.COMPLETED
    assert second["error_message"] is None
    payload = second["result"]
    assert payload is not None
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
    """REVIEW tier → status becomes REVIEW (not COMPLETED)."""
    job = _build_job()
    pdf_path = tmp_path / str(job.batch_id) / f"{job.job_id}.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    repo = FakeRepository(job)
    pipeline_result = _build_pipeline_result(
        score=0.30, tier=ConfidenceTier.REVIEW
    )
    orchestrator = FakeOrchestrator(pipeline_result)

    with patch.object(celery_module, "TEMP_PDF_ROOT", tmp_path), patch(
        "zubot_ingestion.services.get_job_repository", return_value=repo
    ), patch(
        "zubot_ingestion.services.build_orchestrator", return_value=orchestrator
    ):
        result = await _run_extract_document_task(job.job_id)

    assert result["status"] == "review"
    # Final status in repo is REVIEW
    final = repo.update_calls[-1]
    assert final["status"] == JobStatus.REVIEW


async def test_run_extract_document_task_persists_spot_tier_as_completed(
    tmp_path: Path,
) -> None:
    """SPOT tier → status becomes COMPLETED (auto-publish, flag for spot-check)."""
    job = _build_job()
    pdf_path = tmp_path / str(job.batch_id) / f"{job.job_id}.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    repo = FakeRepository(job)
    pipeline_result = _build_pipeline_result(
        score=0.65, tier=ConfidenceTier.SPOT
    )
    orchestrator = FakeOrchestrator(pipeline_result)

    with patch.object(celery_module, "TEMP_PDF_ROOT", tmp_path), patch(
        "zubot_ingestion.services.get_job_repository", return_value=repo
    ), patch(
        "zubot_ingestion.services.build_orchestrator", return_value=orchestrator
    ):
        result = await _run_extract_document_task(job.job_id)

    assert result["status"] == "completed"
    final = repo.update_calls[-1]
    assert final["status"] == JobStatus.COMPLETED


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


async def test_run_extract_document_task_raises_lookup_error_when_job_missing(
    tmp_path: Path,
) -> None:
    """Missing job → LookupError propagates, no update_job_status called."""
    job = _build_job()
    repo = FakeRepository(job)
    orchestrator = FakeOrchestrator(
        _build_pipeline_result(score=0.85, tier=ConfidenceTier.AUTO)
    )
    bogus_id = uuid4()

    with patch.object(celery_module, "TEMP_PDF_ROOT", tmp_path), patch(
        "zubot_ingestion.services.get_job_repository", return_value=repo
    ), patch(
        "zubot_ingestion.services.build_orchestrator", return_value=orchestrator
    ):
        with pytest.raises(LookupError, match="not found"):
            await _run_extract_document_task(bogus_id)

    assert orchestrator.calls == 0
    assert repo.update_calls == []


async def test_mark_job_failed_persists_failed_status_with_error_message() -> None:
    """_mark_job_failed must update the repo to FAILED with the error text."""
    job = _build_job()
    repo = FakeRepository(job)
    exc = ValueError("boom")

    await _mark_job_failed(repo, job.job_id, exc)

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

    with patch.object(celery_module, "TEMP_PDF_ROOT", tmp_path), patch(
        "zubot_ingestion.services.get_job_repository", return_value=repo
    ), patch(
        "zubot_ingestion.services.build_orchestrator", return_value=orchestrator
    ):
        with pytest.raises(FileNotFoundError):
            await _run_extract_document_task(job.job_id)

    # Orchestrator was never called because the file read fails first.
    assert orchestrator.calls == 0
