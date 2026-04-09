"""Regression tests for Bugs 1, 2, and 3 on the Celery extraction task.

These tests lock in the three cross-cutting integration fixes that
cluster in :mod:`zubot_ingestion.services.celery_app` and
:mod:`zubot_ingestion.services.__init__`:

Bug 1 — JobRepository factory composition-root drift
    :func:`zubot_ingestion.services.get_job_repository` must be an
    ``@asynccontextmanager`` that yields a :class:`JobRepository`
    constructed from an async session. The old form called
    ``JobRepository(database_url=...)`` directly and raised ``TypeError``
    at the first invocation because the concrete constructor takes a
    :class:`AsyncSession`.

Bug 2 — Celery retry flapping
    ``extract_document_task`` must NOT call ``_mark_job_failed`` on
    every retry attempt. The cleanup is gated on
    ``self.request.retries >= self.max_retries`` so FAILED is written
    exactly once at terminal failure. This prevents the
    FAILED↔PROCESSING oscillation that used to occur when the
    pre-pipeline PROCESSING marker raced against the manual FAILED
    write on every retry.

Bug 3 — update_job_status vs update_job_result
    The success path must use ``update_job_result`` so the dedicated
    indexed columns (``confidence_score``, ``confidence_tier``,
    ``processing_time_ms``, ``otel_trace_id``, ``pipeline_trace``) are
    populated alongside the JSONB ``result`` blob. ``update_job_status``
    only writes the blob, leaving pagination filters and metrics blind.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from zubot_ingestion.domain.entities import (
    ConfidenceAssessment,
    ExtractionResult,
    Job,
    PipelineResult,
    SidecarDocument,
)
from zubot_ingestion.domain.enums import ConfidenceTier, DocumentType, JobStatus
from zubot_ingestion.services import celery_app as celery_module
from zubot_ingestion.services.celery_app import (
    _mark_job_failed,
    _run_extract_document_task,
    extract_document_task,
)
from zubot_ingestion.shared.types import BatchId, FileHash, JobId


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class RecordingRepository:
    """In-memory repository that records every mutation in order.

    Unlike the simpler FakeRepository in ``test_celery_extract_task.py``,
    this version exposes a ``timeline`` list that captures every status
    transition in the exact order it was written. Tests can assert on
    the timeline to prove that no FAILED↔PROCESSING oscillation ever
    occurred.
    """

    def __init__(self, job: Job) -> None:
        self._job = job
        self.timeline: list[str] = []  # ordered list of JobStatus values
        self.update_status_calls: list[dict[str, Any]] = []
        self.update_result_calls: list[dict[str, Any]] = []

    async def get_job(self, job_id: UUID) -> Job | None:
        if job_id == self._job.job_id:
            return self._job
        return None

    async def get_batch(self, batch_id: UUID) -> None:
        return None

    async def update_job_status(
        self,
        job_id: UUID,
        *,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        self.timeline.append(status.value)
        self.update_status_calls.append(
            {
                "job_id": job_id,
                "status": status,
                "result": result,
                "error_message": error_message,
            }
        )

    async def update_job_result(
        self,
        *,
        job_id: UUID,
        result: dict[str, Any],
        confidence_tier: ConfidenceTier,
        confidence_score: float,
        processing_time_ms: int,
        otel_trace_id: str | None = None,
        pipeline_trace: dict[str, Any] | None = None,
    ) -> None:
        # The real repository marks the row COMPLETED as a side-effect,
        # so the fake records the same transition in the timeline to
        # keep the FAILED↔PROCESSING oscillation assertion meaningful.
        self.timeline.append(JobStatus.COMPLETED.value)
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


class RaisingOrchestrator:
    """Orchestrator stub that raises on every call — used for retry paths."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    async def run_pipeline(
        self,
        job: Job,
        pdf_bytes: bytes,
        *,
        deployment_id: int | None = None,
        node_id: int | None = None,
    ) -> PipelineResult:
        self.calls += 1
        raise self._exc


class SuccessOrchestrator:
    """Orchestrator stub that returns a pre-canned PipelineResult."""

    def __init__(self, pipeline_result: PipelineResult) -> None:
        self._result = pipeline_result
        self.calls = 0

    async def run_pipeline(
        self,
        job: Job,
        pdf_bytes: bytes,
        *,
        deployment_id: int | None = None,
        node_id: int | None = None,
    ) -> PipelineResult:
        self.calls += 1
        return self._result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_job() -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        job_id=JobId(uuid4()),
        batch_id=BatchId(uuid4()),
        filename="test.pdf",
        file_hash=FileHash("f" * 64),
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
    score: float = 0.91,
    tier: ConfidenceTier = ConfidenceTier.AUTO,
) -> PipelineResult:
    extraction = ExtractionResult(
        drawing_number="DN-7",
        drawing_number_confidence=0.95,
        title="T",
        title_confidence=0.9,
        document_type=DocumentType.TECHNICAL_DRAWING,
        document_type_confidence=0.9,
        sources_used=["drawing_number", "title", "document_type"],
        confidence_score=score,
        confidence_tier=tier,
    )
    assessment = ConfidenceAssessment(
        overall_confidence=score,
        tier=tier,
        breakdown={"x": score},
        validation_adjustment=0.0,
    )
    sidecar = SidecarDocument(
        metadata_attributes={"source_filename": "test.pdf"},
        companion_text=None,
        source_filename="test.pdf",
        file_hash=FileHash("f" * 64),
    )
    return PipelineResult(
        extraction_result=extraction,
        companion_result=None,
        sidecar=sidecar,
        confidence_assessment=assessment,
        errors=[],
        pipeline_trace={"stages": {"pdf_load": {"duration_ms": 1, "ok": True}}},
        otel_trace_id="trace-abc",
    )


def _patch_get_job_repository(repo: RecordingRepository) -> Any:
    """Patch ``zubot_ingestion.services.get_job_repository``.

    The production factory is an ``@asynccontextmanager``. The patch
    must return a callable that also produces an async context manager
    so call sites using ``async with get_job_repository() as repo:``
    resolve correctly.
    """

    @asynccontextmanager
    async def _fake_factory() -> AsyncIterator[RecordingRepository]:
        yield repo

    return patch(
        "zubot_ingestion.services.get_job_repository",
        side_effect=lambda: _fake_factory(),
    )


def _push_task_request(retries: int, max_retries: int = 3) -> None:
    """Push a fake request onto the real Celery task so ``self.request.retries``
    returns ``retries`` during the next call.

    Celery's :class:`Task` class exposes ``push_request`` / ``pop_request``
    for exactly this testing scenario. We also temporarily override
    ``max_retries`` on the task instance so tests can parameterise the
    terminal-attempt gate.
    """
    extract_document_task.push_request(retries=retries)
    extract_document_task.max_retries = max_retries


def _pop_task_request() -> None:
    """Pop the fake request pushed by :func:`_push_task_request`."""
    extract_document_task.pop_request()
    # Restore the task-level default.
    extract_document_task.max_retries = 3


# ---------------------------------------------------------------------------
# (1) Factory yields a JobRepository usable as context manager
# ---------------------------------------------------------------------------


async def test_get_job_repository_is_async_context_manager() -> None:
    """Bug 1: the factory must yield via ``async with``.

    This is a smoke test against the real factory function. We patch
    :func:`get_session` so no real database connection is required. The
    test asserts:

    * calling ``get_job_repository()`` returns an object that supports
      the async-context-manager protocol
    * the yielded value is a :class:`JobRepository` bound to the fake
      session
    * using the factory does NOT raise the historical
      ``TypeError: JobRepository.__init__() got an unexpected keyword
      argument 'database_url'``
    """
    from zubot_ingestion.infrastructure.database.repository import JobRepository
    from zubot_ingestion.services import get_job_repository

    class FakeSession:
        pass

    fake_session = FakeSession()

    @asynccontextmanager
    async def _fake_get_session() -> AsyncIterator[FakeSession]:
        yield fake_session

    with patch(
        "zubot_ingestion.infrastructure.database.session.get_session",
        side_effect=lambda: _fake_get_session(),
    ):
        cm = get_job_repository()
        # Must support __aenter__ / __aexit__
        assert hasattr(cm, "__aenter__") and hasattr(cm, "__aexit__")
        async with cm as repo:
            assert isinstance(repo, JobRepository)
            # The repository must be wired to the session we provided
            assert repo._session is fake_session


# ---------------------------------------------------------------------------
# (2) Retry-not-final does NOT call _mark_job_failed
# ---------------------------------------------------------------------------


def test_retry_non_terminal_does_not_mark_failed(tmp_path: Path) -> None:
    """Bug 2: on a retry attempt that is NOT the final one, the manual
    cleanup must NOT write FAILED to the repository.

    The test sets ``self.request.retries = 0`` via Celery's
    ``push_request`` test helper and keeps ``max_retries = 3``. The
    orchestrator raises so the ``except`` path is exercised, then the
    test asserts that ``_mark_job_failed`` was never invoked.
    """
    job = _build_job()
    pdf_path = tmp_path / str(job.batch_id) / f"{job.job_id}.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    repo = RecordingRepository(job)
    orchestrator = RaisingOrchestrator(RuntimeError("transient flake"))

    call_counter = {"count": 0}

    async def _spy_mark_failed(job_id: UUID, exc: BaseException) -> None:
        call_counter["count"] += 1

    _push_task_request(retries=0, max_retries=3)
    try:
        with patch.object(celery_module, "TEMP_PDF_ROOT", tmp_path), \
                _patch_get_job_repository(repo), patch(
            "zubot_ingestion.services.build_orchestrator",
            return_value=orchestrator,
        ), patch.object(celery_module, "_mark_job_failed", _spy_mark_failed):
            with pytest.raises(RuntimeError, match="transient flake"):
                extract_document_task.run(str(job.job_id))
    finally:
        _pop_task_request()

    # The manual cleanup must NOT have run.
    assert call_counter["count"] == 0

    # And the only status transition in the timeline is the pre-pipeline
    # PROCESSING marker — no FAILED was written.
    assert JobStatus.FAILED.value not in repo.timeline


# ---------------------------------------------------------------------------
# (3) Retry-final DOES call _mark_job_failed exactly once
# ---------------------------------------------------------------------------


def test_retry_terminal_calls_mark_failed_exactly_once(tmp_path: Path) -> None:
    """Bug 2: on the terminal retry (retries == max_retries), the
    manual cleanup MUST run and write FAILED exactly once.
    """
    job = _build_job()
    pdf_path = tmp_path / str(job.batch_id) / f"{job.job_id}.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    repo = RecordingRepository(job)
    orchestrator = RaisingOrchestrator(RuntimeError("permanent failure"))

    call_counter = {"count": 0}

    async def _spy_mark_failed(job_id: UUID, exc: BaseException) -> None:
        call_counter["count"] += 1
        # Replicate the real helper's behaviour so the timeline
        # assertion in test (5) remains meaningful.
        await repo.update_job_status(
            job_id,
            status=JobStatus.FAILED,
            result=None,
            error_message=f"{type(exc).__name__}: {exc}",
        )

    _push_task_request(retries=3, max_retries=3)
    try:
        with patch.object(celery_module, "TEMP_PDF_ROOT", tmp_path), \
                _patch_get_job_repository(repo), patch(
            "zubot_ingestion.services.build_orchestrator",
            return_value=orchestrator,
        ), patch.object(celery_module, "_mark_job_failed", _spy_mark_failed):
            with pytest.raises(RuntimeError, match="permanent failure"):
                extract_document_task.run(str(job.job_id))
    finally:
        _pop_task_request()

    assert call_counter["count"] == 1
    assert repo.timeline.count(JobStatus.FAILED.value) == 1


# ---------------------------------------------------------------------------
# (4) Success path calls update_job_result with all five indexed fields
# ---------------------------------------------------------------------------


async def test_success_path_calls_update_job_result_with_indexed_fields(
    tmp_path: Path,
) -> None:
    """Bug 3: on success the indexed columns must be populated.

    The task must call ``repo.update_job_result`` with all five
    indexed fields extracted from the PipelineResult:

    * ``confidence_tier``     — from ``assessment.tier``
    * ``confidence_score``    — from ``assessment.overall_confidence``
    * ``processing_time_ms``  — measured in the task body
    * ``otel_trace_id``       — from ``PipelineResult.otel_trace_id``
    * ``pipeline_trace``      — from ``PipelineResult.pipeline_trace``
    """
    job = _build_job()
    pdf_path = tmp_path / str(job.batch_id) / f"{job.job_id}.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    repo = RecordingRepository(job)
    pipeline_result = _build_pipeline_result(
        score=0.91, tier=ConfidenceTier.AUTO
    )
    orchestrator = SuccessOrchestrator(pipeline_result)

    with patch.object(celery_module, "TEMP_PDF_ROOT", tmp_path), \
            _patch_get_job_repository(repo), patch(
        "zubot_ingestion.services.build_orchestrator", return_value=orchestrator
    ):
        result = await _run_extract_document_task(job.job_id)

    assert result["status"] == "completed"

    # Exactly one update_job_result call
    assert len(repo.update_result_calls) == 1
    call = repo.update_result_calls[0]

    # All five indexed fields must be present and match the
    # PipelineResult object.
    assert call["confidence_tier"] == ConfidenceTier.AUTO
    assert call["confidence_score"] == pytest.approx(0.91)
    assert isinstance(call["processing_time_ms"], int)
    assert call["processing_time_ms"] >= 0
    assert call["otel_trace_id"] == "trace-abc"
    assert call["pipeline_trace"] == pipeline_result.pipeline_trace

    # Sanity: the result payload still carries the full blob.
    payload = call["result"]
    assert payload["drawing_number"] == "DN-7"
    assert payload["confidence_tier"] == "auto"


# ---------------------------------------------------------------------------
# (5) No oscillation FAILED↔PROCESSING
# ---------------------------------------------------------------------------


def test_no_oscillation_between_failed_and_processing_across_retries(
    tmp_path: Path,
) -> None:
    """Bug 2 (regression): across the full retry cycle the repository
    must NEVER oscillate FAILED↔PROCESSING.

    The scenario simulates every retry attempt from 0 through the
    terminal attempt. Before the fix, each attempt wrote PROCESSING
    (pre-pipeline) and then FAILED (manual cleanup), producing a
    FAILED→PROCESSING→FAILED→PROCESSING→...→FAILED timeline with up to
    ``max_retries + 1`` oscillations. After the fix, only the terminal
    attempt writes FAILED, so the timeline is
    PROCESSING, PROCESSING, ..., PROCESSING, FAILED — monotonic, no
    oscillation.
    """
    job = _build_job()
    pdf_path = tmp_path / str(job.batch_id) / f"{job.job_id}.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    repo = RecordingRepository(job)
    orchestrator = RaisingOrchestrator(RuntimeError("always fails"))

    async def _spy_mark_failed(job_id: UUID, exc: BaseException) -> None:
        await repo.update_job_status(
            job_id,
            status=JobStatus.FAILED,
            result=None,
            error_message=f"{type(exc).__name__}: {exc}",
        )

    max_retries = 3

    with patch.object(celery_module, "TEMP_PDF_ROOT", tmp_path), \
            _patch_get_job_repository(repo), patch(
        "zubot_ingestion.services.build_orchestrator", return_value=orchestrator
    ), patch.object(celery_module, "_mark_job_failed", _spy_mark_failed):
        # Simulate every attempt from 0 through max_retries inclusive.
        for attempt in range(max_retries + 1):
            _push_task_request(retries=attempt, max_retries=max_retries)
            try:
                with pytest.raises(RuntimeError):
                    extract_document_task.run(str(job.job_id))
            finally:
                _pop_task_request()

    # Timeline should be: PROCESSING (x4 attempts) then a single FAILED
    # on the terminal attempt. Each attempt writes exactly one PROCESSING
    # (the pre-pipeline marker). The terminal attempt adds a FAILED on
    # top.
    assert repo.timeline.count(JobStatus.PROCESSING.value) == max_retries + 1
    assert repo.timeline.count(JobStatus.FAILED.value) == 1

    # And the FAILED transition only appears at the very end — never
    # followed by another PROCESSING (which would be the oscillation
    # symptom).
    last_failed_index = repo.timeline.index(JobStatus.FAILED.value)
    assert JobStatus.PROCESSING.value not in repo.timeline[last_failed_index + 1:]

    # Regression guard: scan for any FAILED→PROCESSING adjacent pair,
    # which is the exact bug pattern being fixed.
    for i in range(len(repo.timeline) - 1):
        assert not (
            repo.timeline[i] == JobStatus.FAILED.value
            and repo.timeline[i + 1] == JobStatus.PROCESSING.value
        ), f"oscillation detected at index {i}: {repo.timeline}"


# ---------------------------------------------------------------------------
# Bonus: _mark_job_failed now acquires its own repository
# ---------------------------------------------------------------------------


async def test_mark_job_failed_acquires_repository_via_context_manager() -> None:
    """``_mark_job_failed`` must open its own async-CM repository.

    Callers in the sync ``except`` block cannot share the async-session
    bound to the main task body, so the helper is responsible for
    acquiring its own repository. This test patches ``get_job_repository``
    and asserts the helper uses it and writes a FAILED row.
    """
    job = _build_job()
    repo = RecordingRepository(job)
    exc = RuntimeError("kaboom")

    with _patch_get_job_repository(repo):
        await _mark_job_failed(job.job_id, exc)

    assert repo.timeline == [JobStatus.FAILED.value]
    assert len(repo.update_status_calls) == 1
    call = repo.update_status_calls[0]
    assert call["status"] == JobStatus.FAILED
    assert call["error_message"] == "RuntimeError: kaboom"
