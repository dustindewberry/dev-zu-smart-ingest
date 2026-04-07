"""Unit tests for :class:`CompanionGenerator` (CAP-017).

Coverage:
    - Protocol conformance
    - Skip rule: empty PDF (page_count == 0)
    - Skip rule: text-only PDF (extracted_text > 5000 AND page_count > 10)
    - Negative cases for the text-only rule (only one condition met -> not skipped)
    - Page selection algorithm: 1, 2, 3, 4, 50 page documents
    - MAX_COMPANION_PAGES cap
    - Render is invoked with the right page numbers
    - Vision client is called once per rendered page with the prompt
    - Response parser is called with expected_schema
    - Markdown assembly: VISUAL DESCRIPTION / TECHNICAL DETAILS / METADATA blocks
    - Metadata fallback to "N/A" when extraction fields are None
    - Enum DocumentType is rendered as .value
    - Page-level vision failure is silently dropped, others succeed
    - All-pages vision failure -> companion_generated=False, empty text
    - Page-level parser failure is silently dropped
    - Render failure -> companion_generated=False
    - Result fields: companion_generated, pages_described, validation_passed
    - State after mutation: pages_described count matches successful pages
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from zubot_ingestion.domain.entities import (
    CompanionResult,
    ExtractionResult,
    Job,
    OllamaResponse,
    PDFData,
    PipelineContext,
    RenderedPage,
)
from zubot_ingestion.domain.enums import DocumentType, JobStatus
from zubot_ingestion.domain.pipeline.companion import (
    CompanionGenerator,
    _select_pages_to_render,
)
from zubot_ingestion.domain.protocols import ICompanionGenerator
from zubot_ingestion.shared.constants import (
    COMPANION_DESCRIPTION_PROMPT_V1,
    MAX_COMPANION_PAGES,
    OLLAMA_MODEL_VISION,
    OLLAMA_TEMPERATURE_DETERMINISTIC,
    TEXT_ONLY_THRESHOLD_CHARS,
    TEXT_ONLY_THRESHOLD_PAGES,
)
from zubot_ingestion.shared.types import BatchId, FileHash, JobId


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class StubPDFProcessor:
    """In-memory :class:`IPDFProcessor` double.

    Tracks every render_pages call so the tests can verify the page-number
    list passed in by the generator.
    """

    def __init__(
        self,
        *,
        page_count: int = 10,
        raise_on_render: bool = False,
    ) -> None:
        self.page_count = page_count
        self.raise_on_render = raise_on_render
        self.render_calls: list[list[int]] = []
        self.load_calls = 0

    def load(self, pdf_bytes: bytes) -> PDFData:
        self.load_calls += 1
        return PDFData(
            page_count=self.page_count,
            file_hash=FileHash("a" * 64),
            metadata={},
        )

    def extract_text(self, pdf_bytes: bytes) -> str:
        return ""

    def render_page(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def render_pages(
        self,
        pdf_bytes: bytes,
        page_numbers: list[int],
        dpi: int = 200,
        scale: float = 2.0,
    ) -> list[RenderedPage]:
        self.render_calls.append(list(page_numbers))
        if self.raise_on_render:
            raise RuntimeError("simulated render failure")
        rendered: list[RenderedPage] = []
        for page in page_numbers:
            rendered.append(
                RenderedPage(
                    page_number=page,
                    jpeg_bytes=b"jpegbytes",
                    base64_jpeg=f"base64-page-{page}",
                    width_px=1024,
                    height_px=768,
                    dpi=dpi,
                    render_time_ms=10,
                )
            )
        return rendered


class StubOllamaClient:
    """In-memory :class:`IOllamaClient` double.

    Records each generate_vision call and returns a configurable response
    keyed by the order of the calls. Optionally raises on the Nth call.
    """

    def __init__(
        self,
        *,
        responses: list[str] | None = None,
        raise_on_calls: set[int] | None = None,
    ) -> None:
        self.responses = responses or []
        self.raise_on_calls = raise_on_calls or set()
        self.calls: list[dict[str, Any]] = []

    async def generate_vision(
        self,
        image_base64: str,
        prompt: str,
        model: str = "qwen2.5vl:7b",
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
    ) -> OllamaResponse:
        idx = len(self.calls)
        self.calls.append(
            {
                "image_base64": image_base64,
                "prompt": prompt,
                "model": model,
                "temperature": temperature,
                "timeout_seconds": timeout_seconds,
            }
        )
        if idx in self.raise_on_calls:
            raise RuntimeError(f"simulated vision failure on call {idx}")
        if idx < len(self.responses):
            text = self.responses[idx]
        else:
            text = (
                '{"visual_description": "stub visual desc", '
                '"technical_details": "stub technical details"}'
            )
        return OllamaResponse(
            response_text=text,
            model=model,
            prompt_eval_count=10,
            eval_count=20,
            total_duration_ns=1_000_000,
        )

    async def generate_text(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def check_model_available(self, model: str) -> bool:
        return True


class StubResponseParser:
    """In-memory :class:`IResponseParser` double.

    Implements the same async ``parse`` shape that
    :class:`JsonResponseParser` exposes — takes a string + optional
    expected_schema dict and returns a dict.
    """

    def __init__(
        self,
        *,
        raise_on_calls: set[int] | None = None,
        forced_returns: list[dict[str, Any]] | None = None,
    ) -> None:
        self.raise_on_calls = raise_on_calls or set()
        self.forced_returns = forced_returns
        self.calls: list[dict[str, Any]] = []

    async def parse(
        self,
        response_text: str,
        expected_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        idx = len(self.calls)
        self.calls.append(
            {
                "response_text": response_text,
                "expected_schema": expected_schema,
            }
        )
        if idx in self.raise_on_calls:
            raise RuntimeError(f"simulated parse failure on call {idx}")
        if self.forced_returns is not None and idx < len(self.forced_returns):
            return self.forced_returns[idx]
        # Default: pretend the JSON is well-formed and return both fields
        return {
            "visual_description": f"visual-from-call-{idx}",
            "technical_details": f"technical-from-call-{idx}",
        }


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


def _make_job() -> Job:
    now = datetime.now(timezone.utc)
    return Job(
        job_id=JobId(uuid4()),
        batch_id=BatchId(uuid4()),
        filename="drawing.pdf",
        file_hash=FileHash("a" * 64),
        file_path="/tmp/drawing.pdf",
        status=JobStatus.QUEUED,
        result=None,
        error_message=None,
        pipeline_trace=None,
        otel_trace_id=None,
        processing_time_ms=None,
        created_at=now,
        updated_at=now,
    )


def _make_context(
    *,
    page_count: int,
    extracted_text: str | None = None,
) -> PipelineContext:
    pdf_data = PDFData(
        page_count=page_count,
        file_hash=FileHash("a" * 64),
        metadata={},
    )
    return PipelineContext(
        job=_make_job(),
        pdf_bytes=b"%PDF-1.5 stub bytes",
        pdf_data=pdf_data,
        extracted_text=extracted_text,
    )


def _make_extraction_result(
    *,
    drawing_number: str | None = "DWG-001",
    title: str | None = "Floor Plan",
    document_type: DocumentType | None = DocumentType.TECHNICAL_DRAWING,
) -> ExtractionResult:
    return ExtractionResult(
        drawing_number=drawing_number,
        drawing_number_confidence=0.9,
        title=title,
        title_confidence=0.85,
        document_type=document_type,
        document_type_confidence=0.95,
    )


def _make_generator(
    *,
    pdf: StubPDFProcessor | None = None,
    ollama: StubOllamaClient | None = None,
    parser: StubResponseParser | None = None,
) -> tuple[
    CompanionGenerator,
    StubPDFProcessor,
    StubOllamaClient,
    StubResponseParser,
]:
    pdf = pdf or StubPDFProcessor()
    ollama = ollama or StubOllamaClient()
    parser = parser or StubResponseParser()
    gen = CompanionGenerator(
        pdf_processor=pdf,
        ollama_client=ollama,
        response_parser=parser,
    )
    return gen, pdf, ollama, parser


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_generator_satisfies_protocol() -> None:
    gen, *_ = _make_generator()
    assert isinstance(gen, ICompanionGenerator)


# ---------------------------------------------------------------------------
# Page selection algorithm
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("page_count", "expected"),
    [
        (0, []),
        (1, [0]),
        (2, [0, 1]),
        (3, [0, 1]),
        (4, [0, 1, -2, -1]),
        (5, [0, 1, -2, -1]),
        (10, [0, 1, -2, -1]),
        (50, [0, 1, -2, -1]),
        (1000, [0, 1, -2, -1]),
    ],
)
def test_select_pages_to_render_matches_spec(
    page_count: int,
    expected: list[int],
) -> None:
    assert _select_pages_to_render(page_count) == expected


def test_select_pages_to_render_caps_at_max_companion_pages() -> None:
    # MAX_COMPANION_PAGES is 4 by spec; verify the constant pinning
    assert MAX_COMPANION_PAGES == 4
    pages = _select_pages_to_render(100)
    assert len(pages) <= MAX_COMPANION_PAGES


def test_select_pages_dedupes_overlap_for_three_page_doc() -> None:
    # 3-page doc: first two = [0, 1], last two not added (page_count < 4).
    # Should be exactly [0, 1] with no duplicates.
    pages = _select_pages_to_render(3)
    assert pages == [0, 1]
    assert len(pages) == len(set(pages))


# ---------------------------------------------------------------------------
# Skip rule: empty PDF
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_when_page_count_is_zero() -> None:
    gen, pdf, ollama, parser = _make_generator()
    ctx = _make_context(page_count=0)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert isinstance(result, CompanionResult)
    assert result.companion_text == ""
    assert result.pages_described == 0
    assert result.companion_generated is False
    assert result.validation_passed is False
    assert pdf.render_calls == []
    assert ollama.calls == []
    assert parser.calls == []


@pytest.mark.asyncio
async def test_skip_when_pdf_data_is_none() -> None:
    gen, pdf, ollama, parser = _make_generator()
    ctx = PipelineContext(
        job=_make_job(),
        pdf_bytes=b"x",
        pdf_data=None,
    )
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert result.companion_generated is False
    assert result.companion_text == ""
    assert result.pages_described == 0
    assert pdf.render_calls == []
    assert ollama.calls == []


# ---------------------------------------------------------------------------
# Skip rule: text-only heuristic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_when_text_only_pdf() -> None:
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=20))
    long_text = "x" * (TEXT_ONLY_THRESHOLD_CHARS + 1)
    ctx = _make_context(page_count=20, extracted_text=long_text)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert result.companion_generated is False
    assert result.companion_text == ""
    assert result.pages_described == 0
    assert pdf.render_calls == []
    assert ollama.calls == []


@pytest.mark.asyncio
async def test_does_not_skip_when_only_text_long_but_few_pages() -> None:
    """Long text + few pages: vision pass is still worth running."""
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=4))
    long_text = "x" * (TEXT_ONLY_THRESHOLD_CHARS + 1000)
    ctx = _make_context(page_count=4, extracted_text=long_text)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert result.companion_generated is True
    assert result.pages_described == 4
    assert len(pdf.render_calls) == 1


@pytest.mark.asyncio
async def test_does_not_skip_when_many_pages_but_little_text() -> None:
    """Many pages + small text: vision pass is still worth running."""
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=50))
    short_text = "x" * 100  # well below threshold
    ctx = _make_context(page_count=50, extracted_text=short_text)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert result.companion_generated is True
    assert result.pages_described == 4
    assert pdf.render_calls == [[0, 1, -2, -1]]


@pytest.mark.asyncio
async def test_does_not_skip_when_extracted_text_is_none() -> None:
    """``extracted_text=None`` is the default; should not match the rule."""
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=20))
    ctx = _make_context(page_count=20, extracted_text=None)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert result.companion_generated is True
    assert pdf.render_calls == [[0, 1, -2, -1]]


@pytest.mark.asyncio
async def test_text_only_constants_pinned_to_blueprint_values() -> None:
    """Guard against drift in the threshold constants."""
    assert TEXT_ONLY_THRESHOLD_CHARS == 5000
    assert TEXT_ONLY_THRESHOLD_PAGES == 10


# ---------------------------------------------------------------------------
# Render is invoked with the right page numbers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_renders_correct_pages_for_long_doc() -> None:
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=12))
    ctx = _make_context(page_count=12)
    extraction = _make_extraction_result()

    await gen.generate(ctx, extraction)

    assert pdf.render_calls == [[0, 1, -2, -1]]


@pytest.mark.asyncio
async def test_renders_only_first_page_for_single_page_doc() -> None:
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=1))
    ctx = _make_context(page_count=1)
    extraction = _make_extraction_result()

    await gen.generate(ctx, extraction)

    assert pdf.render_calls == [[0]]


@pytest.mark.asyncio
async def test_renders_first_two_pages_for_two_page_doc() -> None:
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=2))
    ctx = _make_context(page_count=2)
    extraction = _make_extraction_result()

    await gen.generate(ctx, extraction)

    assert pdf.render_calls == [[0, 1]]


# ---------------------------------------------------------------------------
# Vision client invocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vision_called_once_per_rendered_page() -> None:
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=12))
    ctx = _make_context(page_count=12)
    extraction = _make_extraction_result()

    await gen.generate(ctx, extraction)

    assert len(ollama.calls) == 4


@pytest.mark.asyncio
async def test_vision_called_with_companion_prompt_and_pinned_settings() -> None:
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=4))
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result()

    await gen.generate(ctx, extraction)

    for call in ollama.calls:
        assert call["prompt"] == COMPANION_DESCRIPTION_PROMPT_V1
        assert call["model"] == OLLAMA_MODEL_VISION
        assert call["temperature"] == OLLAMA_TEMPERATURE_DETERMINISTIC


@pytest.mark.asyncio
async def test_vision_receives_per_page_base64_jpeg() -> None:
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=4))
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result()

    await gen.generate(ctx, extraction)

    sent_images = [call["image_base64"] for call in ollama.calls]
    assert sent_images == [
        "base64-page-0",
        "base64-page-1",
        "base64-page--2",
        "base64-page--1",
    ]


# ---------------------------------------------------------------------------
# Response parser invocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parser_invoked_with_expected_schema() -> None:
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=4))
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result()

    await gen.generate(ctx, extraction)

    assert len(parser.calls) == 4
    for call in parser.calls:
        schema = call["expected_schema"]
        assert schema is not None
        assert "visual_description" in schema
        assert "technical_details" in schema


# ---------------------------------------------------------------------------
# Markdown assembly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_companion_markdown_has_three_sections() -> None:
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=4))
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert "# VISUAL DESCRIPTION" in result.companion_text
    assert "# TECHNICAL DETAILS" in result.companion_text
    assert "# METADATA" in result.companion_text


@pytest.mark.asyncio
async def test_companion_metadata_block_uses_extraction_fields() -> None:
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=4))
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result(
        drawing_number="ARCH-101",
        title="Ground Floor Plan",
        document_type=DocumentType.TECHNICAL_DRAWING,
    )

    result = await gen.generate(ctx, extraction)

    assert "Drawing Number: ARCH-101" in result.companion_text
    assert "Title: Ground Floor Plan" in result.companion_text
    assert "Document Type: technical_drawing" in result.companion_text


@pytest.mark.asyncio
async def test_companion_metadata_block_renders_none_as_na() -> None:
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=4))
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result(
        drawing_number=None, title=None, document_type=None
    )

    result = await gen.generate(ctx, extraction)

    assert "Drawing Number: N/A" in result.companion_text
    assert "Title: N/A" in result.companion_text
    assert "Document Type: N/A" in result.companion_text


@pytest.mark.asyncio
async def test_companion_text_contains_per_page_visual_descriptions() -> None:
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=4))
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    # Each call gets a deterministic stub return value
    assert "visual-from-call-0" in result.companion_text
    assert "visual-from-call-1" in result.companion_text
    assert "visual-from-call-2" in result.companion_text
    assert "visual-from-call-3" in result.companion_text


@pytest.mark.asyncio
async def test_companion_text_contains_per_page_technical_details() -> None:
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=4))
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert "technical-from-call-0" in result.companion_text
    assert "technical-from-call-1" in result.companion_text
    assert "technical-from-call-2" in result.companion_text
    assert "technical-from-call-3" in result.companion_text


# ---------------------------------------------------------------------------
# Failure tolerance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_vision_failure_drops_that_page_only() -> None:
    pdf = StubPDFProcessor(page_count=4)
    ollama = StubOllamaClient(raise_on_calls={1})  # vision call index 1 raises
    parser = StubResponseParser()
    gen = CompanionGenerator(
        pdf_processor=pdf, ollama_client=ollama, response_parser=parser
    )
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert result.companion_generated is True
    assert result.pages_described == 3  # 4 attempted, 1 failed
    # Vision was invoked 4 times, parser was invoked 3 times (parser is only
    # called after a successful vision response). The parser stub keys its
    # return value on its own call index, so the surviving descriptions are
    # from parser calls 0, 1, 2 — parser-call-3 never happened.
    assert len(ollama.calls) == 4
    assert len(parser.calls) == 3
    assert "visual-from-call-0" in result.companion_text
    assert "visual-from-call-1" in result.companion_text
    assert "visual-from-call-2" in result.companion_text
    assert "visual-from-call-3" not in result.companion_text


@pytest.mark.asyncio
async def test_all_vision_failures_yield_empty_companion() -> None:
    pdf = StubPDFProcessor(page_count=4)
    ollama = StubOllamaClient(raise_on_calls={0, 1, 2, 3})
    parser = StubResponseParser()
    gen = CompanionGenerator(
        pdf_processor=pdf, ollama_client=ollama, response_parser=parser
    )
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert result.companion_generated is False
    assert result.companion_text == ""
    assert result.pages_described == 0


@pytest.mark.asyncio
async def test_parser_failure_drops_that_page_only() -> None:
    pdf = StubPDFProcessor(page_count=4)
    ollama = StubOllamaClient()
    parser = StubResponseParser(raise_on_calls={2})
    gen = CompanionGenerator(
        pdf_processor=pdf, ollama_client=ollama, response_parser=parser
    )
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert result.companion_generated is True
    assert result.pages_described == 3
    assert "visual-from-call-2" not in result.companion_text


@pytest.mark.asyncio
async def test_render_failure_yields_empty_companion() -> None:
    pdf = StubPDFProcessor(page_count=4, raise_on_render=True)
    ollama = StubOllamaClient()
    parser = StubResponseParser()
    gen = CompanionGenerator(
        pdf_processor=pdf, ollama_client=ollama, response_parser=parser
    )
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert result.companion_generated is False
    assert result.companion_text == ""
    assert result.pages_described == 0
    assert ollama.calls == []  # vision should not have been called


@pytest.mark.asyncio
async def test_parser_returns_none_for_one_field_only() -> None:
    """If the parser returns ``{"visual_description": "x", "technical_details": None}``
    the page should still count as described (one of the fields is present)."""
    pdf = StubPDFProcessor(page_count=4)
    ollama = StubOllamaClient()
    parser = StubResponseParser(
        forced_returns=[
            {"visual_description": "v0", "technical_details": None},
            {"visual_description": None, "technical_details": "t1"},
            {"visual_description": "v2", "technical_details": "t2"},
            {"visual_description": "v3", "technical_details": "t3"},
        ]
    )
    gen = CompanionGenerator(
        pdf_processor=pdf, ollama_client=ollama, response_parser=parser
    )
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert result.companion_generated is True
    assert result.pages_described == 4
    assert "v0" in result.companion_text
    assert "t1" in result.companion_text
    assert "v2" in result.companion_text
    assert "t3" in result.companion_text


@pytest.mark.asyncio
async def test_parser_returns_empty_dict_drops_page() -> None:
    """If the parser returns ``{"visual_description": None, "technical_details": None}``
    the page contributes nothing and is not counted."""
    pdf = StubPDFProcessor(page_count=4)
    ollama = StubOllamaClient()
    parser = StubResponseParser(
        forced_returns=[
            {"visual_description": "v0", "technical_details": "t0"},
            {"visual_description": None, "technical_details": None},
            {"visual_description": "v2", "technical_details": "t2"},
            {"visual_description": "v3", "technical_details": "t3"},
        ]
    )
    gen = CompanionGenerator(
        pdf_processor=pdf, ollama_client=ollama, response_parser=parser
    )
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert result.companion_generated is True
    assert result.pages_described == 3


@pytest.mark.asyncio
async def test_parser_returns_list_value_is_joined_into_text() -> None:
    """Defensive: some vision models return bullet lists; coerce to a string."""
    pdf = StubPDFProcessor(page_count=1)
    ollama = StubOllamaClient()
    parser = StubResponseParser(
        forced_returns=[
            {
                "visual_description": ["bullet a", "bullet b"],
                "technical_details": "scalar tech",
            },
        ]
    )
    gen = CompanionGenerator(
        pdf_processor=pdf, ollama_client=ollama, response_parser=parser
    )
    ctx = _make_context(page_count=1)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert result.companion_generated is True
    assert "bullet a" in result.companion_text
    assert "bullet b" in result.companion_text
    assert "scalar tech" in result.companion_text


# ---------------------------------------------------------------------------
# Result entity invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_is_companion_result_dataclass() -> None:
    gen, pdf, ollama, parser = _make_generator(pdf=StubPDFProcessor(page_count=4))
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    assert isinstance(result, CompanionResult)
    # quality_score is None until step-19 validator runs
    assert result.quality_score is None
    # validation_passed is False until step-19 validator runs
    assert result.validation_passed is False


@pytest.mark.asyncio
async def test_pages_described_count_matches_successful_pages() -> None:
    """State after mutation: ``pages_described`` is the count of pages
    that contributed at least one (visual or technical) description."""
    pdf = StubPDFProcessor(page_count=4)
    ollama = StubOllamaClient(raise_on_calls={0, 3})
    parser = StubResponseParser()
    gen = CompanionGenerator(
        pdf_processor=pdf, ollama_client=ollama, response_parser=parser
    )
    ctx = _make_context(page_count=4)
    extraction = _make_extraction_result()

    result = await gen.generate(ctx, extraction)

    # Pages 0 and 3 failed; 1 and 2 succeeded
    assert result.pages_described == 2
    assert result.companion_generated is True
