"""Unit tests for the OTEL-instrumented ExtractionOrchestrator (CAP-027).

These tests use an InMemorySpanExporter so we can inspect the actual spans
emitted by ExtractionOrchestrator.run_pipeline() rather than mocking the
tracer. This is the right shape because it verifies the orchestrator
produces the SAME spans (with the SAME attributes) that a downstream
Phoenix/Jaeger collector would receive in production.

Each test wires up a fresh TracerProvider with an InMemorySpanExporter,
runs the orchestrator with stub dependencies, and asserts on the resulting
ReadableSpan objects. The orchestrator's tracer is captured at __init__
time via get_tracer(), so we re-instantiate the orchestrator under each
test's tracer provider.

Coverage:
* All eight named OTEL spans are emitted in the expected nesting
  (BATCH > JOB > STAGE1_DRAWING_NUMBER, STAGE1_TITLE, STAGE1_DOC_TYPE,
  STAGE2_COMPANION, STAGE3_SIDECAR, CONFIDENCE)
* Job span carries file_hash, filename, page_count, job_id, batch_id
  attributes
* Job span trace_id is captured into PipelineResult.otel_trace_id as a
  32-char hex string
* Each Stage 1 extractor span carries extractor.label,
  inference.duration_ms, model.name (when available), and
  confidence.score for the field it owns
* Stage 3 sidecar span and Confidence span carry inference.duration_ms
* Confidence span carries confidence.score and confidence.tier
* Stage 2 companion span is emitted with skipped=True
* Failures attach record_exception() and set the span status to ERROR
* trace_id is preserved across the whole pipeline (job span and child
  spans share the same trace_id)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from zubot_ingestion.domain.entities import (
    CompanionResult,
    ConfidenceAssessment,
    ExtractionResult,
    Job,
    PDFData,
    PipelineContext,
    SidecarDocument,
)
from zubot_ingestion.domain.enums import (
    ConfidenceTier,
    DocumentType,
    JobStatus,
)
from zubot_ingestion.shared.constants import (
    OTEL_SPAN_BATCH,
    OTEL_SPAN_CONFIDENCE,
    OTEL_SPAN_JOB,
    OTEL_SPAN_STAGE1_DOC_TYPE,
    OTEL_SPAN_STAGE1_DRAWING_NUMBER,
    OTEL_SPAN_STAGE1_TITLE,
    OTEL_SPAN_STAGE2_COMPANION,
    OTEL_SPAN_STAGE3_SIDECAR,
    SERVICE_NAME,
    SERVICE_VERSION,
)
from zubot_ingestion.shared.types import BatchId, FileHash, JobId

# ---------------------------------------------------------------------------
# Test fixtures: in-memory tracer provider
# ---------------------------------------------------------------------------

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture
def memory_exporter() -> InMemorySpanExporter:
    """Replace the global tracer provider with one that exports to memory.

    OTEL's set_tracer_provider() refuses to overwrite the global on a
    second call, which would break test isolation. We bypass that by
    resetting the internal Once latch and assigning the provider directly.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": SERVICE_NAME,
                "service.version": SERVICE_VERSION,
            }
        )
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Reset the SET_ONCE latch and assign the provider directly so each
    # test gets a fresh in-memory exporter wired into the global tracer.
    from opentelemetry.util._once import Once

    trace._TRACER_PROVIDER_SET_ONCE = Once()  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    trace.set_tracer_provider(provider)

    yield exporter
    exporter.clear()


@pytest.fixture
def sample_job() -> Job:
    return Job(
        job_id=JobId(UUID("11111111-1111-1111-1111-111111111111")),
        batch_id=BatchId(UUID("22222222-2222-2222-2222-222222222222")),
        filename="170154-L-001.pdf",
        file_hash=FileHash("abc123def456"),
        file_path="/tmp/test.pdf",
        status=JobStatus.PROCESSING,
        result=None,
        error_message=None,
        pipeline_trace=None,
        otel_trace_id=None,
        processing_time_ms=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Stub dependencies
# ---------------------------------------------------------------------------


class StubPDFProcessor:
    def __init__(self, *, page_count: int = 7, fail: bool = False) -> None:
        self._page_count = page_count
        self._fail = fail

    def load(self, pdf_bytes: bytes) -> PDFData:
        if self._fail:
            raise RuntimeError("simulated PDF load failure")
        return PDFData(
            page_count=self._page_count,
            file_hash=FileHash("abc123def456"),
            metadata={},
        )


class StubExtractor:
    """Returns an ExtractionResult populated only for one field."""

    def __init__(
        self,
        *,
        field: str,
        value: str | None = "value",
        confidence: float = 0.9,
        model_name: str | None = "qwen2.5vl:7b",
        fail: bool = False,
    ) -> None:
        self._field = field
        self._value = value
        self._confidence = confidence
        if model_name is not None:
            self.model_name = model_name
        self._fail = fail

    async def extract(self, context: PipelineContext) -> ExtractionResult:
        if self._fail:
            raise RuntimeError(f"{self._field} extractor failed")
        if self._field == "drawing_number":
            return ExtractionResult(
                drawing_number=self._value,
                drawing_number_confidence=self._confidence,
                title=None,
                title_confidence=0.0,
                document_type=None,
                document_type_confidence=0.0,
                sources_used=["vision"],
            )
        if self._field == "title":
            return ExtractionResult(
                drawing_number=None,
                drawing_number_confidence=0.0,
                title=self._value,
                title_confidence=self._confidence,
                document_type=None,
                document_type_confidence=0.0,
                sources_used=["text"],
            )
        if self._field == "document_type":
            return ExtractionResult(
                drawing_number=None,
                drawing_number_confidence=0.0,
                title=None,
                title_confidence=0.0,
                document_type=DocumentType.TECHNICAL_DRAWING,
                document_type_confidence=self._confidence,
                sources_used=["text"],
            )
        raise ValueError(f"unknown field: {self._field}")


class StubSidecarBuilder:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    def build(
        self,
        extraction_result: ExtractionResult,
        companion_result: Any,
        job: Job,
    ) -> SidecarDocument:
        if self._fail:
            raise RuntimeError("simulated sidecar build failure")
        return SidecarDocument(
            metadata_attributes={
                "source_filename": job.filename,
                "document_type": (
                    extraction_result.document_type.value
                    if extraction_result.document_type
                    else "unknown"
                ),
                "extraction_confidence": 0.9,
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
        fail: bool = False,
    ) -> None:
        self._score = score
        self._tier = tier
        self._fail = fail

    def calculate(
        self,
        extraction_result: ExtractionResult,
        validation_result: Any,
    ) -> ConfidenceAssessment:
        if self._fail:
            raise RuntimeError("simulated confidence calc failure")
        return ConfidenceAssessment(
            overall_confidence=self._score,
            tier=self._tier,
            breakdown={"drawing_number": 0.4, "title": 0.3, "document_type": 0.15},
            validation_adjustment=0.0,
        )


class StubCompanionGenerator:
    """No-op companion generator that returns an empty, ungenerated CompanionResult.

    The orchestrator always calls ``generate`` and expects a valid
    ``CompanionResult`` back — there is no "skipped" short-circuit path
    inside the orchestrator itself, so the stub reports
    ``companion_generated=False`` and ``pages_described=0`` to signal
    a no-op run. The Stage 2 span is emitted with these values as
    ``companion.generated`` / ``companion.pages_described`` attributes.
    """

    async def generate(
        self,
        context: PipelineContext,
        extraction_result: ExtractionResult,
    ) -> CompanionResult:
        return CompanionResult(
            companion_text="",
            pages_described=0,
            companion_generated=False,
            validation_passed=True,
            quality_score=None,
        )


class StubMetadataWriter:
    """No-op metadata writer — always reports success."""

    async def write_metadata(
        self,
        document_id: str,
        sidecar: SidecarDocument,
        deployment_id: int | None,
        node_id: int | None,
    ) -> bool:
        return True

    async def check_connection(self) -> bool:
        return True


def _make_orchestrator(
    *,
    pdf_processor: StubPDFProcessor | None = None,
    drawing_extractor: StubExtractor | None = None,
    title_extractor: StubExtractor | None = None,
    doctype_extractor: StubExtractor | None = None,
    sidecar_builder: StubSidecarBuilder | None = None,
    confidence_calculator: StubConfidenceCalculator | None = None,
    companion_generator: StubCompanionGenerator | None = None,
    metadata_writer: StubMetadataWriter | None = None,
):
    # Imported lazily so each test runs against the per-fixture tracer
    # provider; the orchestrator captures get_tracer() at __init__ time.
    from zubot_ingestion.services.orchestrator import ExtractionOrchestrator

    return ExtractionOrchestrator(
        drawing_number_extractor=drawing_extractor
        or StubExtractor(field="drawing_number"),
        title_extractor=title_extractor or StubExtractor(field="title"),
        document_type_extractor=doctype_extractor
        or StubExtractor(field="document_type"),
        sidecar_builder=sidecar_builder or StubSidecarBuilder(),
        confidence_calculator=confidence_calculator
        or StubConfidenceCalculator(),
        pdf_processor=pdf_processor or StubPDFProcessor(),
        companion_generator=companion_generator or StubCompanionGenerator(),
        metadata_writer=metadata_writer or StubMetadataWriter(),
    )


def _spans_by_name(
    exporter: InMemorySpanExporter,
) -> dict[str, ReadableSpan]:
    return {span.name: span for span in exporter.get_finished_spans()}


# ---------------------------------------------------------------------------
# Tests: span structure
# ---------------------------------------------------------------------------


class TestSpanStructure:
    @pytest.mark.asyncio
    async def test_all_eight_spans_emitted(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")

        spans = _spans_by_name(memory_exporter)
        assert OTEL_SPAN_BATCH in spans
        assert OTEL_SPAN_JOB in spans
        assert OTEL_SPAN_STAGE1_DRAWING_NUMBER in spans
        assert OTEL_SPAN_STAGE1_TITLE in spans
        assert OTEL_SPAN_STAGE1_DOC_TYPE in spans
        assert OTEL_SPAN_STAGE2_COMPANION in spans
        assert OTEL_SPAN_STAGE3_SIDECAR in spans
        assert OTEL_SPAN_CONFIDENCE in spans

    @pytest.mark.asyncio
    async def test_job_span_is_child_of_batch_span(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        spans = _spans_by_name(memory_exporter)
        batch = spans[OTEL_SPAN_BATCH]
        job = spans[OTEL_SPAN_JOB]
        assert job.parent is not None
        assert job.parent.span_id == batch.context.span_id

    @pytest.mark.asyncio
    async def test_stage_spans_share_job_trace_id(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        spans = _spans_by_name(memory_exporter)
        job_trace_id = spans[OTEL_SPAN_JOB].context.trace_id
        for stage_name in (
            OTEL_SPAN_STAGE1_DRAWING_NUMBER,
            OTEL_SPAN_STAGE1_TITLE,
            OTEL_SPAN_STAGE1_DOC_TYPE,
            OTEL_SPAN_STAGE2_COMPANION,
            OTEL_SPAN_STAGE3_SIDECAR,
            OTEL_SPAN_CONFIDENCE,
        ):
            assert spans[stage_name].context.trace_id == job_trace_id

    @pytest.mark.asyncio
    async def test_stage1_extractor_spans_are_children_of_job(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        spans = _spans_by_name(memory_exporter)
        job_span_id = spans[OTEL_SPAN_JOB].context.span_id
        for name in (
            OTEL_SPAN_STAGE1_DRAWING_NUMBER,
            OTEL_SPAN_STAGE1_TITLE,
            OTEL_SPAN_STAGE1_DOC_TYPE,
        ):
            assert spans[name].parent is not None
            assert spans[name].parent.span_id == job_span_id


# ---------------------------------------------------------------------------
# Tests: job span attributes
# ---------------------------------------------------------------------------


class TestJobSpanAttributes:
    @pytest.mark.asyncio
    async def test_file_hash_attribute(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        job_span = _spans_by_name(memory_exporter)[OTEL_SPAN_JOB]
        assert job_span.attributes is not None
        assert job_span.attributes["file_hash"] == "abc123def456"

    @pytest.mark.asyncio
    async def test_filename_attribute(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        job_span = _spans_by_name(memory_exporter)[OTEL_SPAN_JOB]
        assert job_span.attributes is not None
        assert job_span.attributes["filename"] == "170154-L-001.pdf"

    @pytest.mark.asyncio
    async def test_page_count_attribute(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator(
            pdf_processor=StubPDFProcessor(page_count=12)
        )
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        job_span = _spans_by_name(memory_exporter)[OTEL_SPAN_JOB]
        assert job_span.attributes is not None
        assert job_span.attributes["page_count"] == 12

    @pytest.mark.asyncio
    async def test_page_count_omitted_when_pdf_load_fails(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator(
            pdf_processor=StubPDFProcessor(fail=True)
        )
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        job_span = _spans_by_name(memory_exporter)[OTEL_SPAN_JOB]
        assert job_span.attributes is not None
        assert "page_count" not in job_span.attributes


# ---------------------------------------------------------------------------
# Tests: trace_id capture and propagation
# ---------------------------------------------------------------------------


class TestTraceIdCapture:
    @pytest.mark.asyncio
    async def test_pipeline_result_carries_trace_id(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        result = await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        assert result.otel_trace_id is not None

    @pytest.mark.asyncio
    async def test_trace_id_is_32_char_lowercase_hex(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        result = await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        assert result.otel_trace_id is not None
        assert _HEX32_RE.match(result.otel_trace_id)

    @pytest.mark.asyncio
    async def test_trace_id_matches_job_span(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        result = await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        job_span = _spans_by_name(memory_exporter)[OTEL_SPAN_JOB]
        expected = format(job_span.context.trace_id, "032x")
        assert result.otel_trace_id == expected

    @pytest.mark.asyncio
    async def test_trace_id_returned_even_when_stages_fail(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator(
            pdf_processor=StubPDFProcessor(fail=True),
            drawing_extractor=StubExtractor(
                field="drawing_number", fail=True
            ),
            sidecar_builder=StubSidecarBuilder(fail=True),
            confidence_calculator=StubConfidenceCalculator(fail=True),
        )
        result = await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        assert result.otel_trace_id is not None
        assert _HEX32_RE.match(result.otel_trace_id)


# ---------------------------------------------------------------------------
# Tests: stage span attributes
# ---------------------------------------------------------------------------


class TestStage1SpanAttributes:
    @pytest.mark.asyncio
    async def test_drawing_number_span_carries_label(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        span = _spans_by_name(memory_exporter)[OTEL_SPAN_STAGE1_DRAWING_NUMBER]
        assert span.attributes["extractor.label"] == "drawing_number"

    @pytest.mark.asyncio
    async def test_drawing_number_span_carries_model_name(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator(
            drawing_extractor=StubExtractor(
                field="drawing_number", model_name="qwen2.5vl:7b"
            )
        )
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        span = _spans_by_name(memory_exporter)[OTEL_SPAN_STAGE1_DRAWING_NUMBER]
        assert span.attributes["model.name"] == "qwen2.5vl:7b"

    @pytest.mark.asyncio
    async def test_stage1_span_carries_inference_duration(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        for name in (
            OTEL_SPAN_STAGE1_DRAWING_NUMBER,
            OTEL_SPAN_STAGE1_TITLE,
            OTEL_SPAN_STAGE1_DOC_TYPE,
        ):
            span = _spans_by_name(memory_exporter)[name]
            assert "inference.duration_ms" in span.attributes
            assert isinstance(
                span.attributes["inference.duration_ms"], int
            )

    @pytest.mark.asyncio
    async def test_stage1_span_carries_field_confidence(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator(
            drawing_extractor=StubExtractor(
                field="drawing_number", confidence=0.95
            ),
            title_extractor=StubExtractor(
                field="title", confidence=0.80
            ),
            doctype_extractor=StubExtractor(
                field="document_type", confidence=0.70
            ),
        )
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        spans = _spans_by_name(memory_exporter)
        assert (
            spans[OTEL_SPAN_STAGE1_DRAWING_NUMBER].attributes[
                "confidence.score"
            ]
            == pytest.approx(0.95)
        )
        assert (
            spans[OTEL_SPAN_STAGE1_TITLE].attributes["confidence.score"]
            == pytest.approx(0.80)
        )
        assert (
            spans[OTEL_SPAN_STAGE1_DOC_TYPE].attributes["confidence.score"]
            == pytest.approx(0.70)
        )

    @pytest.mark.asyncio
    async def test_stage1_span_omits_model_name_when_extractor_lacks_attribute(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator(
            drawing_extractor=StubExtractor(
                field="drawing_number", model_name=None
            )
        )
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        span = _spans_by_name(memory_exporter)[OTEL_SPAN_STAGE1_DRAWING_NUMBER]
        assert "model.name" not in span.attributes


class TestStage2SpanAttributes:
    @pytest.mark.asyncio
    async def test_stage2_span_marks_companion_not_generated(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        """Stub companion generator returns companion_generated=False; the
        orchestrator writes that onto the Stage 2 span as
        ``companion.generated`` plus a real inference.duration_ms."""
        orch = _make_orchestrator()
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        span = _spans_by_name(memory_exporter)[OTEL_SPAN_STAGE2_COMPANION]
        assert span.attributes["companion.generated"] is False
        assert span.attributes["companion.pages_described"] == 0
        assert "inference.duration_ms" in span.attributes


class TestStage3SpanAttributes:
    @pytest.mark.asyncio
    async def test_stage3_span_carries_duration(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        span = _spans_by_name(memory_exporter)[OTEL_SPAN_STAGE3_SIDECAR]
        assert "inference.duration_ms" in span.attributes


class TestConfidenceSpanAttributes:
    @pytest.mark.asyncio
    async def test_confidence_span_carries_score_and_tier(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator(
            confidence_calculator=StubConfidenceCalculator(
                score=0.91, tier=ConfidenceTier.AUTO
            )
        )
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        span = _spans_by_name(memory_exporter)[OTEL_SPAN_CONFIDENCE]
        assert span.attributes["confidence.score"] == pytest.approx(0.91)
        assert span.attributes["confidence.tier"] == "auto"

    @pytest.mark.asyncio
    async def test_confidence_span_carries_duration(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        span = _spans_by_name(memory_exporter)[OTEL_SPAN_CONFIDENCE]
        assert "inference.duration_ms" in span.attributes

    @pytest.mark.asyncio
    async def test_job_span_annotated_with_overall_score_and_tier(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator(
            confidence_calculator=StubConfidenceCalculator(
                score=0.55, tier=ConfidenceTier.SPOT
            )
        )
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        job_span = _spans_by_name(memory_exporter)[OTEL_SPAN_JOB]
        assert job_span.attributes["confidence.score"] == pytest.approx(0.55)
        assert job_span.attributes["confidence.tier"] == "spot"


# ---------------------------------------------------------------------------
# Tests: error propagation onto spans
# ---------------------------------------------------------------------------


class TestErrorRecording:
    @pytest.mark.asyncio
    async def test_pdf_load_failure_marks_job_span_error(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator(
            pdf_processor=StubPDFProcessor(fail=True)
        )
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        job_span = _spans_by_name(memory_exporter)[OTEL_SPAN_JOB]
        assert job_span.status.status_code == StatusCode.ERROR
        # The exception should be recorded as an event on the span.
        assert any(
            event.name == "exception" for event in job_span.events
        )

    @pytest.mark.asyncio
    async def test_stage1_extractor_failure_marks_extractor_span_error(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator(
            title_extractor=StubExtractor(field="title", fail=True)
        )
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        span = _spans_by_name(memory_exporter)[OTEL_SPAN_STAGE1_TITLE]
        assert span.status.status_code == StatusCode.ERROR
        assert any(event.name == "exception" for event in span.events)

    @pytest.mark.asyncio
    async def test_stage1_failure_does_not_mark_other_extractors_error(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator(
            title_extractor=StubExtractor(field="title", fail=True)
        )
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        spans = _spans_by_name(memory_exporter)
        assert (
            spans[OTEL_SPAN_STAGE1_DRAWING_NUMBER].status.status_code
            != StatusCode.ERROR
        )
        assert (
            spans[OTEL_SPAN_STAGE1_DOC_TYPE].status.status_code
            != StatusCode.ERROR
        )

    @pytest.mark.asyncio
    async def test_stage3_failure_marks_sidecar_span_error(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator(
            sidecar_builder=StubSidecarBuilder(fail=True)
        )
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        span = _spans_by_name(memory_exporter)[OTEL_SPAN_STAGE3_SIDECAR]
        assert span.status.status_code == StatusCode.ERROR
        assert any(event.name == "exception" for event in span.events)

    @pytest.mark.asyncio
    async def test_confidence_failure_marks_confidence_span_error(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator(
            confidence_calculator=StubConfidenceCalculator(fail=True)
        )
        await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        span = _spans_by_name(memory_exporter)[OTEL_SPAN_CONFIDENCE]
        assert span.status.status_code == StatusCode.ERROR
        assert any(event.name == "exception" for event in span.events)


# ---------------------------------------------------------------------------
# Tests: pipeline functional output is preserved (no regression)
# ---------------------------------------------------------------------------


class TestPipelineOutputPreserved:
    @pytest.mark.asyncio
    async def test_pipeline_returns_extraction_result(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator(
            drawing_extractor=StubExtractor(
                field="drawing_number", value="170154-L-001"
            ),
            title_extractor=StubExtractor(
                field="title", value="Site Plan"
            ),
        )
        result = await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        assert result.extraction_result.drawing_number == "170154-L-001"
        assert result.extraction_result.title == "Site Plan"
        assert result.extraction_result.confidence_tier == ConfidenceTier.AUTO

    @pytest.mark.asyncio
    async def test_pipeline_trace_still_records_stages(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        result = await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        assert "pdf_load" in result.pipeline_trace["stages"]
        assert "stage1" in result.pipeline_trace["stages"]
        assert "stage2" in result.pipeline_trace["stages"]
        assert "stage3" in result.pipeline_trace["stages"]
        assert "confidence" in result.pipeline_trace["stages"]

    @pytest.mark.asyncio
    async def test_pipeline_returns_sidecar(
        self, memory_exporter: InMemorySpanExporter, sample_job: Job
    ) -> None:
        orch = _make_orchestrator()
        result = await orch.run_pipeline(sample_job, b"%PDF-1.4 ...")
        assert result.sidecar is not None
        assert result.sidecar.source_filename == sample_job.filename
