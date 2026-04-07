"""Unit tests for :class:`ExtractionOrchestrator` (CAP-020).

Coverage:
    - Protocol conformance
    - Happy path: all stages succeed, returns merged result + sidecar +
      assessment, pipeline_trace populated, confidence_score/tier set on
      ExtractionResult
    - Stage 1 partial failure: one extractor raises, others succeed
    - Stage 1 total failure: all extractors raise
    - Stage 3 failure: sidecar builder raises
    - PDF load failure
    - Confidence calculator failure
    - Stage 2 is skipped (no companion result)
    - pipeline_trace records timing per stage
    - extractors run concurrently (asyncio.gather)
    - companion_result is always None for now
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from zubot_ingestion.domain.entities import (
    CompanionResult,
    ConfidenceAssessment,
    ExtractionResult,
    Job,
    PDFData,
    PipelineContext,
    PipelineResult,
    SidecarDocument,
    ValidationResult,
)
from zubot_ingestion.domain.enums import (
    ConfidenceTier,
    DocumentType,
    JobStatus,
)
from zubot_ingestion.domain.protocols import IOrchestrator
from zubot_ingestion.services.orchestrator import ExtractionOrchestrator
from zubot_ingestion.shared.types import BatchId, FileHash, JobId


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class StubPDFProcessor:
    """In-memory :class:`IPDFProcessor` double."""

    def __init__(self, *, raise_on_load: bool = False) -> None:
        self.raise_on_load = raise_on_load
        self.load_calls: int = 0

    def load(self, pdf_bytes: bytes) -> PDFData:
        self.load_calls += 1
        if self.raise_on_load:
            raise RuntimeError("simulated PDF load failure")
        return PDFData(
            page_count=2,
            file_hash=FileHash("a" * 64),
            metadata={},
        )

    def extract_text(self, pdf_bytes: bytes) -> str:
        return ""

    def render_page(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def render_pages(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class StubExtractor:
    """Configurable extractor double — returns a partial ExtractionResult."""

    def __init__(
        self,
        *,
        field: str,  # 'drawing_number' | 'title' | 'document_type'
        value: Any,
        confidence: float,
        delay: float = 0.0,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.field = field
        self.value = value
        self.confidence = confidence
        self.delay = delay
        self.raise_exc = raise_exc
        self.calls: int = 0
        self.invoked_at: float | None = None

    async def extract(self, context: PipelineContext) -> ExtractionResult:
        self.calls += 1
        self.invoked_at = asyncio.get_event_loop().time()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_exc is not None:
            raise self.raise_exc
        kwargs: dict[str, Any] = {
            "drawing_number": None,
            "drawing_number_confidence": 0.0,
            "title": None,
            "title_confidence": 0.0,
            "document_type": None,
            "document_type_confidence": 0.0,
            "sources_used": [self.field],
        }
        if self.field == "drawing_number":
            kwargs["drawing_number"] = self.value
            kwargs["drawing_number_confidence"] = self.confidence
        elif self.field == "title":
            kwargs["title"] = self.value
            kwargs["title_confidence"] = self.confidence
        elif self.field == "document_type":
            kwargs["document_type"] = self.value
            kwargs["document_type_confidence"] = self.confidence
        return ExtractionResult(**kwargs)


class StubSidecarBuilder:
    def __init__(self, *, raise_exc: BaseException | None = None) -> None:
        self.raise_exc = raise_exc
        self.calls: int = 0
        self.last_extraction: ExtractionResult | None = None
        self.last_companion: CompanionResult | None = None
        self.last_job: Job | None = None

    def build(
        self,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult | None,
        job: Job,
    ) -> SidecarDocument:
        self.calls += 1
        self.last_extraction = extraction_result
        self.last_companion = companion_result
        self.last_job = job
        if self.raise_exc is not None:
            raise self.raise_exc
        return SidecarDocument(
            metadata_attributes={
                "source_filename": job.filename,
                "document_type": (
                    extraction_result.document_type.value
                    if extraction_result.document_type
                    else "unknown"
                ),
                "extraction_confidence": 0.85,
            },
            companion_text=None,
            source_filename=job.filename,
            file_hash=job.file_hash,
        )


class StubConfidenceCalculator:
    def __init__(
        self,
        *,
        score: float = 0.85,
        tier: ConfidenceTier = ConfidenceTier.AUTO,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.score = score
        self.tier = tier
        self.raise_exc = raise_exc
        self.calls: int = 0
        self.last_extraction: ExtractionResult | None = None
        self.last_validation: ValidationResult | None = None

    def calculate(
        self,
        extraction_result: ExtractionResult,
        validation_result: ValidationResult | None = None,
    ) -> ConfidenceAssessment:
        self.calls += 1
        self.last_extraction = extraction_result
        self.last_validation = validation_result
        if self.raise_exc is not None:
            raise self.raise_exc
        return ConfidenceAssessment(
            overall_confidence=self.score,
            tier=self.tier,
            breakdown={
                "drawing_number": 0.4,
                "title": 0.3,
                "document_type": 0.15,
                "validation_penalty": 0.0,
            },
            validation_adjustment=0.0,
        )


class StubMetadataWriter:
    """In-memory :class:`IMetadataWriter` double.

    Records every call to :meth:`write_metadata` and :meth:`check_connection`
    so tests can assert (a) that AUTO/SPOT-tier results are written, (b) that
    REVIEW-tier results are NOT written, (c) that the right document_id /
    deployment_id / node_id were forwarded.
    """

    def __init__(
        self,
        *,
        return_value: bool = True,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.return_value = return_value
        self.raise_exc = raise_exc
        self.write_calls: list[dict[str, Any]] = []
        self.check_calls: int = 0

    async def write_metadata(
        self,
        document_id: str,
        sidecar: SidecarDocument,
        deployment_id: int | None,
        node_id: int | None,
    ) -> bool:
        self.write_calls.append(
            {
                "document_id": document_id,
                "sidecar": sidecar,
                "deployment_id": deployment_id,
                "node_id": node_id,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.return_value

    async def check_connection(self) -> bool:
        self.check_calls += 1
        return True


def _make_job() -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        job_id=JobId(uuid4()),
        batch_id=BatchId(uuid4()),
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


def _make_orchestrator(
    *,
    drawing_value: str | None = "X-001",
    drawing_confidence: float = 0.9,
    title_value: str | None = "Test Title",
    title_confidence: float = 0.8,
    document_type_value: DocumentType | None = DocumentType.TECHNICAL_DRAWING,
    document_type_confidence: float = 0.85,
    drawing_raises: BaseException | None = None,
    title_raises: BaseException | None = None,
    document_type_raises: BaseException | None = None,
    sidecar_raises: BaseException | None = None,
    confidence_raises: BaseException | None = None,
    pdf_raises: bool = False,
    drawing_delay: float = 0.0,
    title_delay: float = 0.0,
    document_type_delay: float = 0.0,
    score: float = 0.85,
    tier: ConfidenceTier = ConfidenceTier.AUTO,
    metadata_writer: StubMetadataWriter | None = None,
) -> tuple[
    ExtractionOrchestrator,
    StubPDFProcessor,
    StubExtractor,
    StubExtractor,
    StubExtractor,
    StubSidecarBuilder,
    StubConfidenceCalculator,
    StubMetadataWriter,
]:
    pdf = StubPDFProcessor(raise_on_load=pdf_raises)
    drawing = StubExtractor(
        field="drawing_number",
        value=drawing_value,
        confidence=drawing_confidence,
        raise_exc=drawing_raises,
        delay=drawing_delay,
    )
    title = StubExtractor(
        field="title",
        value=title_value,
        confidence=title_confidence,
        raise_exc=title_raises,
        delay=title_delay,
    )
    doctype = StubExtractor(
        field="document_type",
        value=document_type_value,
        confidence=document_type_confidence,
        raise_exc=document_type_raises,
        delay=document_type_delay,
    )
    sidecar = StubSidecarBuilder(raise_exc=sidecar_raises)
    confidence = StubConfidenceCalculator(
        score=score, tier=tier, raise_exc=confidence_raises
    )
    writer = metadata_writer or StubMetadataWriter()
    orchestrator = ExtractionOrchestrator(
        drawing_number_extractor=drawing,
        title_extractor=title,
        document_type_extractor=doctype,
        sidecar_builder=sidecar,
        confidence_calculator=confidence,
        pdf_processor=pdf,
        metadata_writer=writer,
    )
    return orchestrator, pdf, drawing, title, doctype, sidecar, confidence, writer


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_orchestrator_satisfies_protocol() -> None:
    o, *_ = _make_orchestrator()
    assert isinstance(o, IOrchestrator)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_returns_pipeline_result_with_all_fields() -> None:
    o, pdf, drawing, title, doctype, sidecar, conf, _writer = _make_orchestrator()
    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    assert isinstance(result, PipelineResult)
    assert result.extraction_result.drawing_number == "X-001"
    assert result.extraction_result.title == "Test Title"
    assert result.extraction_result.document_type == DocumentType.TECHNICAL_DRAWING
    assert result.extraction_result.confidence_score == pytest.approx(0.85)
    assert result.extraction_result.confidence_tier == ConfidenceTier.AUTO
    assert result.companion_result is None  # Stage 2 skipped
    assert result.sidecar.metadata_attributes["source_filename"] == "test.pdf"
    assert result.confidence_assessment.tier == ConfidenceTier.AUTO
    assert result.confidence_assessment.overall_confidence == pytest.approx(0.85)
    assert result.otel_trace_id is None
    assert result.errors == []

    # Each Stage 1 extractor was invoked exactly once.
    assert drawing.calls == 1
    assert title.calls == 1
    assert doctype.calls == 1

    # Sidecar builder received the merged extraction result and None companion.
    assert sidecar.calls == 1
    assert sidecar.last_companion is None
    assert sidecar.last_extraction is not None

    # Confidence calculator received the merged extraction result and no validation.
    assert conf.calls == 1
    assert conf.last_validation is None


async def test_happy_path_pipeline_trace_records_per_stage_timing() -> None:
    o, *_ = _make_orchestrator()
    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    trace = result.pipeline_trace
    assert "stages" in trace
    assert "errors" in trace
    assert "pdf_load" in trace["stages"]
    assert "stage1" in trace["stages"]
    assert "stage2" in trace["stages"]
    assert "stage3" in trace["stages"]
    assert "confidence" in trace["stages"]

    # Stage 2 must be skipped
    assert trace["stages"]["stage2"]["skipped"] is True

    # Each non-skipped stage carries a duration_ms field
    for key in ("pdf_load", "stage1", "stage3", "confidence"):
        assert "duration_ms" in trace["stages"][key]
        assert isinstance(trace["stages"][key]["duration_ms"], int)
        assert trace["stages"][key]["duration_ms"] >= 0
        assert trace["stages"][key]["ok"] is True

    # No errors recorded on the happy path.
    assert trace["errors"] == []


async def test_extractors_run_concurrently_via_gather() -> None:
    """All three extractors should overlap — wall-clock < sum of delays."""
    o, *_ = _make_orchestrator(
        drawing_delay=0.1,
        title_delay=0.1,
        document_type_delay=0.1,
    )
    start = asyncio.get_event_loop().time()
    await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")
    elapsed = asyncio.get_event_loop().time() - start
    # Sequential would be ~0.3s; concurrent should be < 0.2s.
    assert elapsed < 0.2, f"extractors did not run concurrently: {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# Stage 2 always skipped
# ---------------------------------------------------------------------------


async def test_stage2_is_skipped_companion_result_is_none() -> None:
    o, _, _, _, _, sidecar, conf, _writer = _make_orchestrator()
    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    assert result.companion_result is None
    assert sidecar.last_companion is None
    assert conf.last_validation is None


# ---------------------------------------------------------------------------
# Failure handling: Stage 1 partial
# ---------------------------------------------------------------------------


async def test_stage1_partial_failure_drawing_extractor_raises() -> None:
    o, _, _, _, _, sidecar, conf, _writer = _make_orchestrator(
        drawing_raises=RuntimeError("vision model down"),
    )
    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    # The other extractors still produced their values
    assert result.extraction_result.title == "Test Title"
    assert result.extraction_result.document_type == DocumentType.TECHNICAL_DRAWING
    # The failed field is None with zero confidence
    assert result.extraction_result.drawing_number is None
    assert result.extraction_result.drawing_number_confidence == 0.0

    # Pipeline still completes; sidecar and confidence ran
    assert sidecar.calls == 1
    assert conf.calls == 1

    # Trace records the failure
    assert any(
        e["stage"] == "stage1.drawing_number"
        for e in result.pipeline_trace["errors"]
    )
    assert any(e.stage == "stage1.drawing_number" for e in result.errors)


async def test_stage1_partial_failure_title_extractor_raises() -> None:
    o, *_ = _make_orchestrator(
        title_raises=ValueError("malformed JSON"),
    )
    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    assert result.extraction_result.drawing_number == "X-001"
    assert result.extraction_result.title is None
    assert result.extraction_result.title_confidence == 0.0
    assert any(
        e["stage"] == "stage1.title" for e in result.pipeline_trace["errors"]
    )


async def test_stage1_total_failure_all_extractors_raise() -> None:
    o, *_ = _make_orchestrator(
        drawing_raises=RuntimeError("a"),
        title_raises=RuntimeError("b"),
        document_type_raises=RuntimeError("c"),
    )
    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    assert result.extraction_result.drawing_number is None
    assert result.extraction_result.title is None
    assert result.extraction_result.document_type is None

    # All three failures recorded in the trace
    error_stages = {e["stage"] for e in result.pipeline_trace["errors"]}
    assert "stage1.drawing_number" in error_stages
    assert "stage1.title" in error_stages
    assert "stage1.document_type" in error_stages

    # Confidence and sidecar still ran with the empty result
    assert result.confidence_assessment is not None
    assert result.sidecar is not None


# ---------------------------------------------------------------------------
# Failure handling: PDF load
# ---------------------------------------------------------------------------


async def test_pdf_load_failure_continues_with_degraded_result() -> None:
    o, *_ = _make_orchestrator(pdf_raises=True)
    result = await o.run_pipeline(_make_job(), b"")

    # Stage 1 still ran (extractors get None pdf_data in context)
    # The orchestrator returns a structurally valid PipelineResult.
    assert isinstance(result, PipelineResult)

    # The failure is recorded
    assert any(
        e["stage"] == "pdf_load" for e in result.pipeline_trace["errors"]
    )
    assert result.pipeline_trace["stages"]["pdf_load"]["ok"] is False


# ---------------------------------------------------------------------------
# Failure handling: Stage 3 sidecar build
# ---------------------------------------------------------------------------


async def test_stage3_sidecar_failure_returns_fallback_sidecar() -> None:
    o, *_ = _make_orchestrator(
        sidecar_raises=ValueError("schema mismatch"),
    )
    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    # Pipeline still completes; sidecar is the fallback empty document
    assert isinstance(result.sidecar, SidecarDocument)
    assert result.sidecar.source_filename == "test.pdf"
    assert result.sidecar.metadata_attributes["document_type"] == "unknown"

    # Failure is recorded in trace
    assert any(e["stage"] == "stage3" for e in result.pipeline_trace["errors"])
    assert result.pipeline_trace["stages"]["stage3"]["ok"] is False


# ---------------------------------------------------------------------------
# Failure handling: confidence calculator
# ---------------------------------------------------------------------------


async def test_confidence_failure_returns_review_tier_assessment() -> None:
    o, *_ = _make_orchestrator(
        confidence_raises=RuntimeError("calculator broken"),
    )
    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    # Fallback assessment is REVIEW with score 0.0
    assert result.confidence_assessment.overall_confidence == 0.0
    assert result.confidence_assessment.tier == ConfidenceTier.REVIEW
    # ExtractionResult still has the fallback score and tier set
    assert result.extraction_result.confidence_score == 0.0
    assert result.extraction_result.confidence_tier == ConfidenceTier.REVIEW
    # Failure recorded
    assert any(
        e["stage"] == "confidence" for e in result.pipeline_trace["errors"]
    )


# ---------------------------------------------------------------------------
# Tier propagation onto ExtractionResult
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "tier"),
    [
        (0.95, ConfidenceTier.AUTO),
        (0.65, ConfidenceTier.SPOT),
        (0.30, ConfidenceTier.REVIEW),
    ],
)
async def test_extraction_result_carries_score_and_tier(
    score: float,
    tier: ConfidenceTier,
) -> None:
    o, *_ = _make_orchestrator(score=score, tier=tier)
    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")
    assert result.extraction_result.confidence_score == pytest.approx(score)
    assert result.extraction_result.confidence_tier == tier


# ---------------------------------------------------------------------------
# Sidecar builder receives merged extraction result
# ---------------------------------------------------------------------------


async def test_sidecar_builder_receives_merged_extraction_result() -> None:
    o, _, _, _, _, sidecar, _, _writer = _make_orchestrator()
    await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    merged = sidecar.last_extraction
    assert merged is not None
    assert merged.drawing_number == "X-001"
    assert merged.title == "Test Title"
    assert merged.document_type == DocumentType.TECHNICAL_DRAWING
    # sources_used was unioned across all three extractors
    assert "drawing_number" in merged.sources_used
    assert "title" in merged.sources_used
    assert "document_type" in merged.sources_used


async def test_pdf_processor_invoked_exactly_once() -> None:
    o, pdf, *_ = _make_orchestrator()
    await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")
    assert pdf.load_calls == 1


# ---------------------------------------------------------------------------
# Metadata write integration (CAP-023) — verify state AFTER mutation
# ---------------------------------------------------------------------------


async def test_auto_tier_writes_metadata_to_writer() -> None:
    """AUTO tier → metadata_writer.write_metadata is called exactly once."""
    writer = StubMetadataWriter()
    o, *_ = _make_orchestrator(
        score=0.95, tier=ConfidenceTier.AUTO, metadata_writer=writer
    )
    job = _make_job()

    await o.run_pipeline(job, b"%PDF-1.4 fake")

    assert len(writer.write_calls) == 1
    call = writer.write_calls[0]
    assert call["document_id"] == str(job.file_hash)
    assert call["sidecar"] is not None


async def test_spot_tier_writes_metadata_to_writer() -> None:
    """SPOT tier → metadata_writer.write_metadata is called exactly once."""
    writer = StubMetadataWriter()
    o, *_ = _make_orchestrator(
        score=0.65, tier=ConfidenceTier.SPOT, metadata_writer=writer
    )

    await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    assert len(writer.write_calls) == 1


async def test_review_tier_does_not_write_metadata_to_writer() -> None:
    """REVIEW tier → metadata_writer.write_metadata is NEVER called.

    REVIEW results are routed to the human review queue and only flushed
    to ChromaDB after a reviewer approves them (CAP-022).
    """
    writer = StubMetadataWriter()
    o, *_ = _make_orchestrator(
        score=0.30, tier=ConfidenceTier.REVIEW, metadata_writer=writer
    )

    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    assert writer.write_calls == []
    # The trace must explicitly record the skip with a reason so reviewers
    # can audit why no write happened.
    metadata_stage = result.pipeline_trace["stages"]["metadata_write"]
    assert metadata_stage["skipped"] is True
    assert metadata_stage["reason"] == "review_tier_routes_to_review_queue"


async def test_metadata_write_forwards_deployment_and_node_id() -> None:
    """run_pipeline kwargs deployment_id/node_id are forwarded to the writer."""
    writer = StubMetadataWriter()
    o, *_ = _make_orchestrator(
        score=0.95, tier=ConfidenceTier.AUTO, metadata_writer=writer
    )

    await o.run_pipeline(
        _make_job(),
        b"%PDF-1.4 fake",
        deployment_id=42,
        node_id=7,
    )

    assert len(writer.write_calls) == 1
    call = writer.write_calls[0]
    assert call["deployment_id"] == 42
    assert call["node_id"] == 7


async def test_metadata_write_passes_none_ids_when_not_supplied() -> None:
    """When run_pipeline is called without deployment/node IDs, both are None."""
    writer = StubMetadataWriter()
    o, *_ = _make_orchestrator(
        score=0.95, tier=ConfidenceTier.AUTO, metadata_writer=writer
    )

    await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    assert len(writer.write_calls) == 1
    call = writer.write_calls[0]
    assert call["deployment_id"] is None
    assert call["node_id"] is None


async def test_metadata_write_forwards_assembled_sidecar() -> None:
    """The sidecar passed to the writer is the one Stage 3 produced.

    The StubSidecarBuilder constructs a fresh SidecarDocument on every
    call, so we cannot use ``is``-identity to assert. Instead we verify
    that the writer received a SidecarDocument whose attributes match
    what the stub builder always emits (source_filename from job.filename,
    file_hash from job.file_hash).
    """
    writer = StubMetadataWriter()
    o, _, _, _, _, sidecar_builder, _, _ = _make_orchestrator(
        score=0.95, tier=ConfidenceTier.AUTO, metadata_writer=writer
    )
    job = _make_job()

    await o.run_pipeline(job, b"%PDF-1.4 fake")

    [call] = writer.write_calls
    received_sidecar = call["sidecar"]
    assert received_sidecar is not None
    assert received_sidecar.source_filename == job.filename
    assert received_sidecar.file_hash == job.file_hash
    # Stage 3 was actually invoked exactly once
    assert sidecar_builder.calls == 1


async def test_metadata_write_failure_recorded_in_trace_but_pipeline_succeeds() -> None:
    """Writer returning False is logged in pipeline_trace but does not raise."""
    writer = StubMetadataWriter(return_value=False)
    o, *_ = _make_orchestrator(
        score=0.95, tier=ConfidenceTier.AUTO, metadata_writer=writer
    )

    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    assert len(writer.write_calls) == 1
    metadata_stage = result.pipeline_trace["stages"]["metadata_write"]
    assert metadata_stage["ok"] is False
    assert metadata_stage["skipped"] is False
    # An error entry must be appended to the errors list
    error_stages = [e["stage"] for e in result.pipeline_trace["errors"]]
    assert "metadata_write" in error_stages


async def test_metadata_write_exception_recorded_in_trace_but_pipeline_succeeds() -> None:
    """Writer raising is caught, recorded, and pipeline returns normally."""
    writer = StubMetadataWriter(raise_exc=RuntimeError("chromadb is angry"))
    o, *_ = _make_orchestrator(
        score=0.95, tier=ConfidenceTier.AUTO, metadata_writer=writer
    )

    # Must not raise
    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    metadata_stage = result.pipeline_trace["stages"]["metadata_write"]
    assert metadata_stage["ok"] is False
    assert "RuntimeError" in metadata_stage["error"]
    assert "chromadb is angry" in metadata_stage["error"]
    error_stages = [e["stage"] for e in result.pipeline_trace["errors"]]
    assert "metadata_write" in error_stages


async def test_metadata_write_uses_job_file_hash_as_document_id() -> None:
    """document_id passed to write_metadata must equal str(job.file_hash)."""
    writer = StubMetadataWriter()
    o, *_ = _make_orchestrator(
        score=0.95, tier=ConfidenceTier.AUTO, metadata_writer=writer
    )
    job = _make_job()

    await o.run_pipeline(job, b"%PDF-1.4 fake")

    [call] = writer.write_calls
    assert call["document_id"] == str(job.file_hash)


async def test_review_tier_skip_records_zero_duration() -> None:
    """The skipped metadata_write stage entry has duration_ms=0 and ok=True."""
    writer = StubMetadataWriter()
    o, *_ = _make_orchestrator(
        score=0.10, tier=ConfidenceTier.REVIEW, metadata_writer=writer
    )

    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    stage = result.pipeline_trace["stages"]["metadata_write"]
    assert stage["duration_ms"] == 0
    assert stage["ok"] is True
    assert stage["skipped"] is True
