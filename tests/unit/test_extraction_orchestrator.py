"""Unit tests for :class:`ExtractionOrchestrator` (CAP-020).

Coverage:
    - Protocol conformance
    - Happy path: all stages succeed, returns merged result + sidecar +
      assessment, pipeline_trace populated, confidence_score/tier set on
      ExtractionResult
    - Stage 1 partial failure: one extractor raises, others succeed
    - Stage 1 total failure: all extractors raise
    - Stage 2 companion generator failure -> recoverable
    - Stage 3 failure: sidecar builder raises
    - PDF load failure
    - Confidence calculator failure
    - Stage 2 companion result is produced and forwarded to sidecar builder
    - pipeline_trace records timing per stage
    - extractors run concurrently (asyncio.gather)
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


class StubCompanionGenerator:
    """In-memory :class:`ICompanionGenerator` double."""

    def __init__(
        self,
        *,
        companion_text: str = "# VISUAL DESCRIPTION\nstub\n\n# TECHNICAL DETAILS\nstub\n\n# METADATA\nstub",
        pages_described: int = 2,
        companion_generated: bool = True,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.companion_text = companion_text
        self.pages_described = pages_described
        self.companion_generated = companion_generated
        self.raise_exc = raise_exc
        self.calls: int = 0
        self.last_context: PipelineContext | None = None
        self.last_extraction: ExtractionResult | None = None

    async def generate(
        self,
        context: PipelineContext,
        extraction_result: ExtractionResult,
    ) -> CompanionResult:
        self.calls += 1
        self.last_context = context
        self.last_extraction = extraction_result
        if self.raise_exc is not None:
            raise self.raise_exc
        return CompanionResult(
            companion_text=self.companion_text,
            pages_described=self.pages_described,
            companion_generated=self.companion_generated,
            validation_passed=False,
            quality_score=None,
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
    companion_raises: BaseException | None = None,
    pdf_raises: bool = False,
    drawing_delay: float = 0.0,
    title_delay: float = 0.0,
    document_type_delay: float = 0.0,
    score: float = 0.85,
    tier: ConfidenceTier = ConfidenceTier.AUTO,
) -> tuple[
    ExtractionOrchestrator,
    StubPDFProcessor,
    StubExtractor,
    StubExtractor,
    StubExtractor,
    StubSidecarBuilder,
    StubConfidenceCalculator,
    StubCompanionGenerator,
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
    companion = StubCompanionGenerator(raise_exc=companion_raises)
    orchestrator = ExtractionOrchestrator(
        drawing_number_extractor=drawing,
        title_extractor=title,
        document_type_extractor=doctype,
        sidecar_builder=sidecar,
        confidence_calculator=confidence,
        pdf_processor=pdf,
        companion_generator=companion,
    )
    return orchestrator, pdf, drawing, title, doctype, sidecar, confidence, companion


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
    o, pdf, drawing, title, doctype, sidecar, conf, companion = _make_orchestrator()
    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    assert isinstance(result, PipelineResult)
    assert result.extraction_result.drawing_number == "X-001"
    assert result.extraction_result.title == "Test Title"
    assert result.extraction_result.document_type == DocumentType.TECHNICAL_DRAWING
    assert result.extraction_result.confidence_score == pytest.approx(0.85)
    assert result.extraction_result.confidence_tier == ConfidenceTier.AUTO
    # Stage 2 produced a companion result
    assert result.companion_result is not None
    assert result.companion_result.companion_generated is True
    assert result.companion_result.pages_described == 2
    assert result.sidecar.metadata_attributes["source_filename"] == "test.pdf"
    assert result.confidence_assessment.tier == ConfidenceTier.AUTO
    assert result.confidence_assessment.overall_confidence == pytest.approx(0.85)
    assert result.otel_trace_id is None
    assert result.errors == []

    # Each Stage 1 extractor was invoked exactly once.
    assert drawing.calls == 1
    assert title.calls == 1
    assert doctype.calls == 1

    # Companion generator was invoked once with the merged extraction result.
    assert companion.calls == 1
    assert companion.last_extraction is not None
    assert companion.last_context is not None

    # Sidecar builder received the merged extraction result and the companion.
    assert sidecar.calls == 1
    assert sidecar.last_companion is not None
    assert sidecar.last_companion is result.companion_result
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

    # Stage 2 now runs and records companion metadata
    assert trace["stages"]["stage2"]["ok"] is True
    assert trace["stages"]["stage2"]["companion_generated"] is True
    assert trace["stages"]["stage2"]["pages_described"] == 2

    # Each stage carries a duration_ms field
    for key in ("pdf_load", "stage1", "stage2", "stage3", "confidence"):
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
# Stage 2 companion forwarding
# ---------------------------------------------------------------------------


async def test_stage2_companion_result_forwarded_to_sidecar() -> None:
    o, _, _, _, _, sidecar, conf, companion = _make_orchestrator()
    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    # Companion generator was invoked and produced a result
    assert companion.calls == 1
    assert result.companion_result is not None
    assert result.companion_result.companion_generated is True

    # That same result was forwarded to the sidecar builder
    assert sidecar.last_companion is result.companion_result

    # The confidence calculator still gets None validation (CAP-018 not yet wired)
    assert conf.last_validation is None


async def test_stage2_companion_generator_failure_is_recoverable() -> None:
    """A raised companion_generator.generate() must be captured, not propagated."""
    o, _, _, _, _, sidecar, conf, companion = _make_orchestrator(
        companion_raises=RuntimeError("vision model down"),
    )
    result = await o.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    # Pipeline still completes with a structurally valid result
    assert isinstance(result, PipelineResult)
    # companion_result is None after a Stage 2 failure
    assert result.companion_result is None
    # The downstream sidecar builder ran with None companion
    assert sidecar.calls == 1
    assert sidecar.last_companion is None
    # The failure is recorded both in the trace's errors and on .errors
    assert any(e["stage"] == "stage2" for e in result.pipeline_trace["errors"])
    assert any(e.stage == "stage2" for e in result.errors)
    # The failure is marked recoverable so callers can still consume the result
    stage2_errors = [e for e in result.errors if e.stage == "stage2"]
    assert len(stage2_errors) == 1
    assert stage2_errors[0].recoverable is True
    # The stage2 trace slot shows ok=False
    assert result.pipeline_trace["stages"]["stage2"]["ok"] is False
    # The rest of the pipeline ran normally
    assert conf.calls == 1
    assert result.extraction_result.confidence_tier == ConfidenceTier.AUTO


# ---------------------------------------------------------------------------
# Failure handling: Stage 1 partial
# ---------------------------------------------------------------------------


async def test_stage1_partial_failure_drawing_extractor_raises() -> None:
    o, _, _, _, _, sidecar, conf, _ = _make_orchestrator(
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
    o, _, _, _, _, sidecar, _, _ = _make_orchestrator()
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
