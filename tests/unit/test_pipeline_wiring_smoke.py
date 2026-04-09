"""Pipeline wiring smoke test.

This is CRITICAL regression coverage against the "built but not wired"
pattern documented in PolyForge's institutional learning: a factory
creates a protocol implementation and all unit tests pass, but no
production caller ever invokes the new adapter, so the feature is dead
code.

This test constructs an :class:`ExtractionOrchestrator` with spy
implementations of the three cross-cutting adapters
(ICompanionValidator, ISearchIndexer, ICallbackClient) and runs
:meth:`ExtractionOrchestrator.run_pipeline` end-to-end with stub
pipeline collaborators. After the run returns, the test asserts:

1. The validator's ``validate`` method was called with the Stage 2
   companion text.
2. The search indexer's ``index_companion`` method was called with
   the extraction result and companion result.
3. The callback client's ``notify_completion`` method was called with
   the persisted job.

If any of these assertions fail, the composition root has drifted
again and one of the "fixed" bugs has regressed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import pytest

from zubot_ingestion.domain.entities import (
    CompanionResult,
    ConfidenceAssessment,
    ExtractionResult,
    Job,
    PDFData,
    PipelineContext,
    RenderedPage,
    SidecarDocument,
    ValidationResult,
)
from zubot_ingestion.domain.enums import ConfidenceTier, JobStatus
from zubot_ingestion.services.orchestrator import ExtractionOrchestrator


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class StubPDFProcessor:
    def load(self, pdf_bytes: bytes) -> PDFData:
        return PDFData(
            page_count=2,
            file_hash="deadbeef",  # type: ignore[arg-type]
            metadata={},
        )

    def render_pages(
        self, pdf_bytes: bytes, page_numbers: list[int]
    ) -> list[RenderedPage]:
        return [
            RenderedPage(
                page_number=n,
                jpeg_bytes=b"",
                base64_jpeg="",
                width_px=100,
                height_px=100,
                dpi=72,
                render_time_ms=1,
            )
            for n in page_numbers
        ]

    def extract_text(self, pdf_bytes: bytes) -> str:
        return ""


class StubExtractor:
    def __init__(self, field: str, value: Any, confidence: float) -> None:
        self._field = field
        self._value = value
        self._confidence = confidence
        self.calls = 0

    async def extract(self, context: PipelineContext) -> ExtractionResult:
        self.calls += 1
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
    def __init__(self) -> None:
        self.calls = 0
        self.received_validation_result: ValidationResult | None = None

    def calculate(
        self,
        extraction_result: ExtractionResult,
        validation_result: ValidationResult | None,
    ) -> ConfidenceAssessment:
        self.calls += 1
        self.received_validation_result = validation_result
        return ConfidenceAssessment(
            overall_confidence=0.9,
            tier=ConfidenceTier.AUTO,
            breakdown={},
            validation_adjustment=0.0,
        )


class StubCompanionGenerator:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self,
        context: PipelineContext,
        extraction_result: ExtractionResult,
    ) -> CompanionResult:
        self.calls += 1
        return CompanionResult(
            companion_text=(
                "# VISUAL DESCRIPTION\nA test drawing with drawing_number X-001."
                "\n\n# TECHNICAL DETAILS\nScale 1:100."
                "\n\n# METADATA\nDrawing Number: X-001\nTitle: Test Title"
                "\nDocument Type: technical_drawing"
            ),
            pages_described=1,
            companion_generated=True,
            validation_passed=False,
            quality_score=None,
        )


class StubMetadataWriter:
    def __init__(self) -> None:
        self.calls = 0

    async def write_metadata(
        self,
        document_id: str,
        sidecar: SidecarDocument,
        deployment_id: int | None,
        node_id: int | None,
    ) -> bool:
        self.calls += 1
        return True


class SpyCompanionValidator:
    def __init__(self) -> None:
        self.calls = 0
        self.last_companion_text: str | None = None
        self.last_extraction_result: ExtractionResult | None = None

    def validate(
        self,
        companion_text: str,
        extraction_result: ExtractionResult,
    ) -> ValidationResult:
        self.calls += 1
        self.last_companion_text = companion_text
        self.last_extraction_result = extraction_result
        return ValidationResult(
            passed=True,
            warnings=[],
            confidence_adjustment=0.0,
        )


class SpySearchIndexer:
    def __init__(self) -> None:
        self.calls = 0
        self.last_document_id: str | None = None
        self.last_extraction_result: ExtractionResult | None = None
        self.last_companion_result: CompanionResult | None = None
        self.last_deployment_id: int | None = None

    async def index_companion(
        self,
        document_id: str,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult,
        deployment_id: int | None,
    ) -> bool:
        self.calls += 1
        self.last_document_id = document_id
        self.last_extraction_result = extraction_result
        self.last_companion_result = companion_result
        self.last_deployment_id = deployment_id
        return True

    async def check_connection(self) -> bool:
        return True


class SpyCallbackClient:
    def __init__(self) -> None:
        self.calls = 0
        self.last_job: Job | None = None

    async def notify_completion(
        self,
        callback_url: str,
        job: Job,
        api_key: str,
    ) -> bool:
        self.calls += 1
        self.last_job = job
        return True


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_job() -> Job:
    return Job(
        job_id=UUID("00000000-0000-0000-0000-000000000001"),  # type: ignore[arg-type]
        batch_id=UUID("00000000-0000-0000-0000-000000000002"),  # type: ignore[arg-type]
        filename="test.pdf",
        file_hash="deadbeef",  # type: ignore[arg-type]
        file_path="/tmp/test.pdf",
        status=JobStatus.PROCESSING,
        result=None,
        error_message=None,
        pipeline_trace=None,
        otel_trace_id=None,
        processing_time_ms=None,
        created_at=datetime(2025, 1, 1),
        updated_at=datetime(2025, 1, 1),
    )


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_wiring_smoke_invokes_all_cross_cutting_adapters() -> None:
    """End-to-end smoke test: validator + indexer + callback must all fire.

    This is the CRITICAL regression test against the recurring
    'built but not wired' pattern. If any of the three cross-cutting
    adapters is not invoked at least once during a happy-path pipeline
    run, the composition root has drifted.
    """
    validator = SpyCompanionValidator()
    indexer = SpySearchIndexer()
    callback = SpyCallbackClient()

    orchestrator = ExtractionOrchestrator(
        drawing_number_extractor=StubExtractor("drawing_number", "X-001", 0.9),
        title_extractor=StubExtractor("title", "Test Title", 0.9),
        document_type_extractor=StubExtractor("document_type", None, 0.9),
        sidecar_builder=StubSidecarBuilder(),
        confidence_calculator=StubConfidenceCalculator(),
        pdf_processor=StubPDFProcessor(),
        companion_generator=StubCompanionGenerator(),
        metadata_writer=StubMetadataWriter(),
        companion_validator=validator,
        search_indexer=indexer,
        callback_client=callback,
    )

    result = await orchestrator.run_pipeline(
        _make_job(),
        b"%PDF-1.4 fake",
        deployment_id=42,
        node_id=7,
    )

    # (1) Validator was invoked.
    assert validator.calls == 1, (
        "CompanionValidator.validate was never called — composition "
        "root regression"
    )
    assert validator.last_companion_text is not None
    assert "X-001" in validator.last_companion_text

    # (2) Search indexer was invoked.
    assert indexer.calls == 1, (
        "SearchIndexer.index_companion was never called — "
        "composition root regression"
    )
    assert indexer.last_document_id is not None
    assert indexer.last_deployment_id == 42

    # (3) Callback client was invoked.
    assert callback.calls == 1, (
        "CallbackClient.notify_completion was never called — "
        "composition root regression"
    )
    assert callback.last_job is not None

    # Sanity: the pipeline trace records all three new stages.
    assert "companion_validation" in result.pipeline_trace["stages"]
    assert "search_index" in result.pipeline_trace["stages"]
    assert "callback" in result.pipeline_trace["stages"]
