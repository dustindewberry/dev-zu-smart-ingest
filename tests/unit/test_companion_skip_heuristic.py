"""Unit tests for the Stage-2 companion-skip heuristic (task-6 / perf).

Covers the flag-gated heuristic added to
:class:`zubot_ingestion.services.orchestrator.ExtractionOrchestrator`
in Stage 2 of :meth:`run_pipeline`. When
``Settings.COMPANION_SKIP_ENABLED`` is True and the PDF text layer's
word count meets or exceeds ``Settings.COMPANION_SKIP_MIN_WORDS``, the
orchestrator skips the vision-model companion call for that job,
records the decision as an OTEL span event, and marks the pipeline
trace with a ``companion_skipped`` marker.

Scenarios asserted here:

(a) flag=False → generator is called regardless of word count
    (preserves existing behavior — the default)
(b) flag=True, word_count < threshold → generator IS called
(c) flag=True, word_count >= threshold → generator is NOT called,
    pipeline_trace contains ``companion_skipped``, and the OTEL
    span event ``OTEL_SPAN_COMPANION_SKIPPED`` is recorded on the
    Stage 2 parent span.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from zubot_ingestion.config import Settings
from zubot_ingestion.domain.entities import (
    CompanionResult,
    ConfidenceAssessment,
    ExtractionResult,
    Job,
    PDFData,
    PipelineContext,
    SidecarDocument,
    ValidationResult,
)
from zubot_ingestion.domain.enums import ConfidenceTier, JobStatus
from zubot_ingestion.shared.constants import (
    OTEL_SPAN_COMPANION_SKIPPED,
    OTEL_SPAN_STAGE2_COMPANION,
    SERVICE_NAME,
    SERVICE_VERSION,
)
from zubot_ingestion.shared.types import BatchId, FileHash, JobId


# ---------------------------------------------------------------------------
# In-memory OTEL tracer fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_exporter() -> InMemorySpanExporter:
    """Replace the global tracer provider with one that exports to memory.

    Mirrors the pattern already in test_orchestrator_otel.py: we reset
    the internal Once latch so each test gets a fresh provider. Spans
    emitted by the orchestrator (via ``get_tracer()`` at __init__ time)
    land in the in-memory exporter so we can inspect events and
    attributes.
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

    from opentelemetry.util._once import Once

    trace._TRACER_PROVIDER_SET_ONCE = Once()  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    trace.set_tracer_provider(provider)

    yield exporter
    exporter.clear()


# ---------------------------------------------------------------------------
# Stub collaborators
# ---------------------------------------------------------------------------


class StubPDFProcessor:
    """Minimal :class:`IPDFProcessor` double with a tunable text layer."""

    def __init__(self, *, text_layer: str = "") -> None:
        self._text_layer = text_layer
        self.extract_text_calls = 0

    def load(self, pdf_bytes: bytes) -> PDFData:
        return PDFData(
            page_count=1,
            file_hash=FileHash("abc123"),
            metadata={},
        )

    def extract_text(self, pdf_bytes: bytes) -> str:
        self.extract_text_calls += 1
        return self._text_layer

    # render_page is declared on IPDFProcessor but the orchestrator
    # does not invoke it directly in this code path — declaring a
    # no-op helper keeps the stub forwards-compatible if that ever
    # changes.
    def render_page(
        self,
        pdf_bytes: bytes,
        page_number: int,
        dpi: int = 200,
        scale: float = 2.0,
    ) -> Any:
        raise NotImplementedError


class StubExtractor:
    """Populates one field on the merged :class:`ExtractionResult`."""

    def __init__(self, field: str, value: Any, confidence: float) -> None:
        self._field = field
        self._value = value
        self._confidence = confidence

    async def extract(self, context: PipelineContext) -> ExtractionResult:
        kwargs: dict[str, Any] = {
            "drawing_number": None,
            "drawing_number_confidence": 0.0,
            "title": None,
            "title_confidence": 0.0,
            "document_type": None,
            "document_type_confidence": 0.0,
        }
        if self._field == "drawing_number":
            kwargs["drawing_number"] = self._value
            kwargs["drawing_number_confidence"] = self._confidence
        elif self._field == "title":
            kwargs["title"] = self._value
            kwargs["title_confidence"] = self._confidence
        elif self._field == "document_type":
            kwargs["document_type"] = self._value
            kwargs["document_type_confidence"] = self._confidence
        return ExtractionResult(**kwargs)


class StubSidecarBuilder:
    def build(
        self,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult | None,
        job: Job,
    ) -> SidecarDocument:
        return SidecarDocument(
            metadata_attributes={"source_filename": job.filename},
            companion_text=(
                companion_result.companion_text if companion_result else None
            ),
            source_filename=job.filename,
            file_hash=job.file_hash,
        )


class StubConfidenceCalculator:
    def calculate(
        self,
        extraction_result: ExtractionResult,
        validation_result: ValidationResult | None,
    ) -> ConfidenceAssessment:
        return ConfidenceAssessment(
            overall_confidence=0.9,
            tier=ConfidenceTier.AUTO,
            breakdown={},
            validation_adjustment=0.0,
        )


class StubCompanionGenerator:
    """Spy double — records every call and returns a canned result."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_context: PipelineContext | None = None
        self.last_extraction_result: ExtractionResult | None = None

    async def generate(
        self,
        context: PipelineContext,
        extraction_result: ExtractionResult,
    ) -> CompanionResult:
        self.calls += 1
        self.last_context = context
        self.last_extraction_result = extraction_result
        return CompanionResult(
            companion_text="# VISUAL DESCRIPTION\nA stubbed companion.",
            pages_described=1,
            companion_generated=True,
            validation_passed=False,
            quality_score=None,
        )


class StubMetadataWriter:
    async def write_metadata(
        self,
        document_id: str,
        sidecar: SidecarDocument,
        deployment_id: int | None,
        node_id: int | None,
    ) -> bool:
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(
    *,
    enabled: bool,
    threshold: int,
) -> Settings:
    """Build a :class:`Settings` instance with the skip flags overridden.

    Constructs a fresh Settings (env-var defaults still apply) and uses
    ``model_copy`` to override just the two companion-skip fields so
    test cases can dial the heuristic on/off without touching the rest
    of the configuration surface.
    """
    base = Settings()  # type: ignore[call-arg]
    return base.model_copy(
        update={
            "COMPANION_SKIP_ENABLED": enabled,
            "COMPANION_SKIP_MIN_WORDS": threshold,
        }
    )


def _make_job() -> Job:
    return Job(
        job_id=JobId(UUID("00000000-0000-0000-0000-000000000001")),
        batch_id=BatchId(UUID("00000000-0000-0000-0000-000000000002")),
        filename="test.pdf",
        file_hash=FileHash("deadbeef"),
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


def _make_orchestrator(
    *,
    settings: Settings,
    pdf_processor: StubPDFProcessor,
    companion_generator: StubCompanionGenerator,
):
    """Build an ExtractionOrchestrator with the injected settings and spies."""
    from zubot_ingestion.services.orchestrator import ExtractionOrchestrator

    return ExtractionOrchestrator(
        drawing_number_extractor=StubExtractor("drawing_number", "X-001", 0.9),
        title_extractor=StubExtractor("title", "Test Title", 0.9),
        document_type_extractor=StubExtractor("document_type", None, 0.9),
        sidecar_builder=StubSidecarBuilder(),
        confidence_calculator=StubConfidenceCalculator(),
        pdf_processor=pdf_processor,
        companion_generator=companion_generator,
        metadata_writer=StubMetadataWriter(),
        settings=settings,
    )


def _stage2_events(
    exporter: InMemorySpanExporter,
) -> list[tuple[str, dict[str, Any]]]:
    """Return (event_name, attributes) pairs for the Stage 2 span."""
    events: list[tuple[str, dict[str, Any]]] = []
    for span in exporter.get_finished_spans():
        if span.name != OTEL_SPAN_STAGE2_COMPANION:
            continue
        for event in span.events:
            events.append((event.name, dict(event.attributes or {})))
    return events


# ---------------------------------------------------------------------------
# Scenario (a) — flag=False preserves existing behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_disabled_calls_generator_regardless_of_word_count(
    memory_exporter: InMemorySpanExporter,
) -> None:
    """COMPANION_SKIP_ENABLED=False: generator always runs (default)."""
    # Even a huge text layer must NOT trigger the skip when the flag is off.
    pdf = StubPDFProcessor(text_layer=" ".join(["word"] * 10_000))
    generator = StubCompanionGenerator()
    settings = _make_settings(enabled=False, threshold=100)

    orchestrator = _make_orchestrator(
        settings=settings,
        pdf_processor=pdf,
        companion_generator=generator,
    )

    result = await orchestrator.run_pipeline(
        _make_job(),
        b"%PDF-1.4 fake",
    )

    assert generator.calls == 1, (
        "companion_generator must be invoked exactly once when "
        "COMPANION_SKIP_ENABLED=False (backwards-compat contract)"
    )
    assert "companion_skipped" not in result.pipeline_trace
    stage2 = result.pipeline_trace["stages"]["stage2"]
    assert stage2.get("companion_skipped") is not True
    # When the flag is off we never touch extract_text at all.
    assert pdf.extract_text_calls == 0
    # No skip event on the Stage 2 span.
    events = _stage2_events(memory_exporter)
    assert all(name != OTEL_SPAN_COMPANION_SKIPPED for name, _ in events)


# ---------------------------------------------------------------------------
# Scenario (b) — flag=True, word_count < threshold → generator still runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_enabled_but_below_threshold_calls_generator(
    memory_exporter: InMemorySpanExporter,
) -> None:
    """COMPANION_SKIP_ENABLED=True but sparse page → generator still runs."""
    # 5 words, threshold 150 — the heuristic should NOT fire.
    pdf = StubPDFProcessor(text_layer="only five words in here")
    generator = StubCompanionGenerator()
    settings = _make_settings(enabled=True, threshold=150)

    orchestrator = _make_orchestrator(
        settings=settings,
        pdf_processor=pdf,
        companion_generator=generator,
    )

    result = await orchestrator.run_pipeline(
        _make_job(),
        b"%PDF-1.4 fake",
    )

    assert generator.calls == 1, (
        "companion_generator must still fire when flag=True but the "
        "word count is below the threshold"
    )
    assert "companion_skipped" not in result.pipeline_trace
    stage2 = result.pipeline_trace["stages"]["stage2"]
    assert stage2.get("companion_skipped") is not True
    # extract_text was consulted exactly once to compute the word count.
    assert pdf.extract_text_calls == 1
    # No skip event on the Stage 2 span.
    events = _stage2_events(memory_exporter)
    assert all(name != OTEL_SPAN_COMPANION_SKIPPED for name, _ in events)


# ---------------------------------------------------------------------------
# Scenario (c) — flag=True, word_count >= threshold → skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_enabled_and_above_threshold_skips_generator(
    memory_exporter: InMemorySpanExporter,
) -> None:
    """COMPANION_SKIP_ENABLED=True and dense page → generator SKIPPED.

    Asserts the three required observable effects:

    1. companion_generator is NOT invoked.
    2. pipeline_trace contains the ``companion_skipped`` marker.
    3. Stage 2 OTEL span carries the COMPANION_SKIPPED event with
       ``word_count`` and ``threshold`` attributes.
    """
    # 200 words, threshold 150 → heuristic fires.
    pdf = StubPDFProcessor(text_layer=" ".join(["word"] * 200))
    generator = StubCompanionGenerator()
    settings = _make_settings(enabled=True, threshold=150)

    orchestrator = _make_orchestrator(
        settings=settings,
        pdf_processor=pdf,
        companion_generator=generator,
    )

    result = await orchestrator.run_pipeline(
        _make_job(),
        b"%PDF-1.4 fake",
    )

    # (1) Generator was NOT invoked.
    assert generator.calls == 0, (
        "companion_generator must NOT be invoked when flag=True and "
        "word_count >= COMPANION_SKIP_MIN_WORDS"
    )

    # (2) pipeline_trace contains the skip marker.
    assert "companion_skipped" in result.pipeline_trace, (
        "pipeline_trace must surface a 'companion_skipped' marker so "
        "downstream debugging can see the decision"
    )
    marker = result.pipeline_trace["companion_skipped"]
    assert marker["word_count"] == 200
    assert marker["threshold"] == 150

    # The per-stage trace entry should also record the skip.
    stage2 = result.pipeline_trace["stages"]["stage2"]
    assert stage2["companion_skipped"] is True
    assert stage2["companion_generated"] is False
    assert stage2["pages_described"] == 0
    assert stage2["ok"] is True

    # extract_text is called exactly once per pipeline run to compute
    # the word count.
    assert pdf.extract_text_calls == 1

    # (3) OTEL span event was recorded on the Stage 2 parent span.
    events = _stage2_events(memory_exporter)
    skip_events = [
        (name, attrs)
        for name, attrs in events
        if name == OTEL_SPAN_COMPANION_SKIPPED
    ]
    assert len(skip_events) == 1, (
        f"expected exactly one OTEL_SPAN_COMPANION_SKIPPED event on the "
        f"Stage 2 parent span, got events={events!r}"
    )
    _, attrs = skip_events[0]
    assert attrs.get("word_count") == 200
    assert attrs.get("threshold") == 150


# ---------------------------------------------------------------------------
# Extra guardrail: the Stage 2 parent span itself is still emitted,
# confirming the skip is a CHILD event not a replacement for the parent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_keeps_stage2_parent_span_present(
    memory_exporter: InMemorySpanExporter,
) -> None:
    """The skip path must still emit the Stage 2 parent span."""
    pdf = StubPDFProcessor(text_layer=" ".join(["word"] * 500))
    generator = StubCompanionGenerator()
    settings = _make_settings(enabled=True, threshold=150)

    orchestrator = _make_orchestrator(
        settings=settings,
        pdf_processor=pdf,
        companion_generator=generator,
    )

    await orchestrator.run_pipeline(_make_job(), b"%PDF-1.4 fake")

    spans: list[ReadableSpan] = list(memory_exporter.get_finished_spans())
    stage2_spans = [s for s in spans if s.name == OTEL_SPAN_STAGE2_COMPANION]
    assert len(stage2_spans) == 1, (
        "Stage 2 parent span must still be emitted on the skip path "
        "(the skip is a child event, not a replacement)"
    )
