"""Unit tests for the per-page Stage-2 companion-skip heuristic (task-6 / perf).

The companion-skip heuristic lives inside
:class:`zubot_ingestion.domain.pipeline.companion.CompanionGenerator`,
not at the orchestrator level. The decision is made PER PAGE: for every
rendered page the generator consults ``IPDFProcessor.extract_page_text``
and, when ``Settings.COMPANION_SKIP_ENABLED`` is True and that page's
text-layer word count is at or above
``Settings.COMPANION_SKIP_MIN_WORDS``, the vision-model call for that
page is short-circuited. Keeping the decision per-page means a drawing
set with a sparse title block and dense spec pages still renders the
title block through the vision model.

These tests exercise the real ``CompanionGenerator`` directly with
stub collaborators rather than going through the orchestrator, because
the orchestrator layer is a pass-through for this feature and wrapping
it adds noise without exercising any new behavior.

Scenarios:

(a) ``COMPANION_SKIP_ENABLED=False`` — every rendered page goes to the
    vision model regardless of word count, and ``extract_page_text``
    is never consulted (preserves legacy behavior; this is the
    default).
(b) ``COMPANION_SKIP_ENABLED=True`` but every page's word count is
    below the threshold — the vision model is called for every page
    and ``extract_page_text`` is called once per rendered page to
    compute the per-page word count.
(c) ``COMPANION_SKIP_ENABLED=True`` and every page's word count is at
    or above the threshold — the vision model is never called, every
    page is skipped, and the final ``CompanionResult`` reports
    ``companion_generated=False`` / ``pages_described=0``.
(d) Mixed — some pages exceed the threshold, others do not. The
    generator skips the dense pages and runs the vision model for
    the sparse pages.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest

from zubot_ingestion.config import Settings
from zubot_ingestion.domain.entities import (
    ExtractionResult,
    Job,
    OllamaResponse,
    PDFData,
    PipelineContext,
    RenderedPage,
)
from zubot_ingestion.domain.enums import JobStatus
from zubot_ingestion.domain.pipeline.companion import CompanionGenerator
from zubot_ingestion.shared.types import BatchId, FileHash, JobId


# ---------------------------------------------------------------------------
# Stub collaborators for CompanionGenerator
# ---------------------------------------------------------------------------


class StubPDFProcessor:
    """Minimal :class:`IPDFProcessor` double for the per-page skip path.

    ``page_count`` is configurable so tests can drive the generator's
    page selector (``_select_pages_to_render``) into the 1-page,
    2-page, or 4+-page branch. ``page_text_map`` lets each test pin
    the text layer for individual pages independently, so the mixed
    scenario (d) can dial some pages above the threshold and others
    below it.
    """

    def __init__(
        self,
        *,
        page_count: int = 1,
        page_text_map: dict[int, str] | None = None,
        default_text: str = "",
    ) -> None:
        self._page_count = page_count
        self._page_text_map = page_text_map or {}
        self._default_text = default_text
        self.extract_page_text_calls: list[int] = []
        self.render_pages_calls = 0

    def load(self, pdf_bytes: bytes) -> PDFData:
        return PDFData(
            page_count=self._page_count,
            file_hash=FileHash("deadbeef"),
            metadata={},
        )

    def extract_text(self, pdf_bytes: bytes) -> str:
        # The per-page heuristic never consults this method — it is
        # only implemented so the stub still satisfies IPDFProcessor
        # if another code path happens to call it.
        return self._default_text

    def extract_page_text(self, pdf_bytes: bytes, page_number: int) -> str:
        self.extract_page_text_calls.append(page_number)
        return self._page_text_map.get(page_number, self._default_text)

    def render_pages(
        self,
        pdf_bytes: bytes,
        page_numbers: list[int],
    ) -> list[RenderedPage]:
        self.render_pages_calls += 1
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

    def render_page(
        self,
        pdf_bytes: bytes,
        page_number: int,
        dpi: int = 200,
        scale: float = 2.0,
    ) -> Any:
        raise NotImplementedError


class SpyOllamaClient:
    """Spy Ollama client — records every ``generate_vision`` call.

    Returns a canned response so the generator can proceed to the
    response parser. ``vision_calls`` is the list of page numbers the
    generator passed to the vision model; tests assert against its
    length and contents.
    """

    def __init__(self) -> None:
        self.vision_calls: list[str] = []
        self.text_calls = 0

    async def generate_vision(
        self,
        image_base64: str,
        prompt: str,
        model: str = "qwen2.5vl:7b",
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
    ) -> OllamaResponse:
        # We don't have the page number here (the vision API is
        # page-agnostic), so we record the call count via list length.
        self.vision_calls.append(image_base64)
        return OllamaResponse(
            response_text='{"visual_description": "a sample page", "technical_details": "some details"}',
            model=model,
            prompt_eval_count=None,
            eval_count=None,
            total_duration_ns=None,
        )

    async def generate_text(
        self,
        text: str,
        prompt: str,
        model: str = "qwen2.5:7b",
        temperature: float = 0.0,
        timeout_seconds: float = 30.0,
    ) -> OllamaResponse:
        self.text_calls += 1
        raise AssertionError(
            "generate_text must not be called from CompanionGenerator"
        )

    async def check_model_available(self, model: str) -> bool:
        return True


class StubResponseParser:
    """Response parser that returns the canned schema-filled dict."""

    async def parse(
        self,
        raw_response: str,
        expected_schema: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "visual_description": "a sample page",
            "technical_details": "some details",
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(*, enabled: bool, threshold: int) -> Settings:
    """Build a :class:`Settings` instance with the skip flags overridden."""
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


def _make_extraction_result() -> ExtractionResult:
    return ExtractionResult(
        drawing_number="X-001",
        drawing_number_confidence=0.9,
        title="Test Title",
        title_confidence=0.9,
        document_type=None,
        document_type_confidence=0.0,
    )


def _make_context(pdf: StubPDFProcessor) -> PipelineContext:
    """Build a PipelineContext with pdf_data populated via the stub.

    ``extracted_text`` is deliberately left as its default ``None`` so
    that ``CompanionGenerator._is_text_only`` never short-circuits the
    generator via its whole-document text-only skip rule — that rule
    is unrelated to the per-page heuristic this test exercises.
    """
    return PipelineContext(
        job=_make_job(),
        pdf_bytes=b"%PDF-1.4 fake",
        pdf_data=pdf.load(b"%PDF-1.4 fake"),
    )


def _make_generator(
    *,
    pdf: StubPDFProcessor,
    ollama: SpyOllamaClient,
    settings: Settings,
) -> CompanionGenerator:
    return CompanionGenerator(
        pdf_processor=pdf,  # type: ignore[arg-type]
        ollama_client=ollama,  # type: ignore[arg-type]
        response_parser=StubResponseParser(),  # type: ignore[arg-type]
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Scenario (a) — flag=False preserves existing behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_disabled_calls_vision_model_regardless_of_word_count() -> None:
    """COMPANION_SKIP_ENABLED=False: vision model runs on every page.

    With the flag disabled the generator must NOT consult
    ``extract_page_text`` at all, and must invoke ``generate_vision``
    once per rendered page. This is the legacy/default path.
    """
    # page_count=4 → selector returns [0, 1, -2, -1] → 4 rendered pages.
    # Even if every page had 10,000 words, the flag=False means the
    # heuristic is skipped entirely.
    pdf = StubPDFProcessor(
        page_count=4,
        default_text=" ".join(["word"] * 10_000),
    )
    ollama = SpyOllamaClient()
    settings = _make_settings(enabled=False, threshold=100)
    generator = _make_generator(pdf=pdf, ollama=ollama, settings=settings)

    context = _make_context(pdf)
    result = await generator.generate(context, _make_extraction_result())

    # Vision fired once per rendered page (4 pages selected).
    assert len(ollama.vision_calls) == 4, (
        "generate_vision must be invoked for every rendered page when "
        "COMPANION_SKIP_ENABLED=False (legacy contract)"
    )

    # extract_page_text was never consulted.
    assert pdf.extract_page_text_calls == [], (
        "extract_page_text must not be called when the skip flag is off"
    )

    # Companion text was produced and all 4 pages contributed.
    assert result.companion_generated is True
    assert result.pages_described == 4


# ---------------------------------------------------------------------------
# Scenario (b) — flag=True, all pages below threshold → vision runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_enabled_but_below_threshold_calls_vision_model() -> None:
    """COMPANION_SKIP_ENABLED=True but sparse pages → vision still runs.

    With a threshold of 150 words and each page containing only 5
    words, the skip should NOT fire on any page and every page must
    go through the vision model. ``extract_page_text`` is still
    consulted once per rendered page to compute the word count.
    """
    pdf = StubPDFProcessor(
        page_count=4,
        default_text="only five words in here",  # 5 words
    )
    ollama = SpyOllamaClient()
    settings = _make_settings(enabled=True, threshold=150)
    generator = _make_generator(pdf=pdf, ollama=ollama, settings=settings)

    context = _make_context(pdf)
    result = await generator.generate(context, _make_extraction_result())

    # Vision fired for every page — no page was skipped.
    assert len(ollama.vision_calls) == 4, (
        "generate_vision must fire for every page when all per-page "
        "word counts are below the threshold"
    )

    # extract_page_text was consulted once per rendered page.
    assert len(pdf.extract_page_text_calls) == 4, (
        "extract_page_text must be called once per rendered page when "
        "the skip flag is enabled (to compute the per-page word count)"
    )

    assert result.companion_generated is True
    assert result.pages_described == 4


# ---------------------------------------------------------------------------
# Scenario (c) — flag=True, all pages at/above threshold → all skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_enabled_and_above_threshold_skips_vision_model() -> None:
    """COMPANION_SKIP_ENABLED=True and dense pages → vision never runs.

    With a threshold of 150 words and every page containing 200
    words, the per-page skip fires for every rendered page. The
    vision model must never be invoked and the resulting
    ``CompanionResult`` reports
    ``companion_generated=False`` / ``pages_described=0`` because
    every page was skipped.
    """
    pdf = StubPDFProcessor(
        page_count=4,
        default_text=" ".join(["word"] * 200),  # 200 words
    )
    ollama = SpyOllamaClient()
    settings = _make_settings(enabled=True, threshold=150)
    generator = _make_generator(pdf=pdf, ollama=ollama, settings=settings)

    context = _make_context(pdf)
    result = await generator.generate(context, _make_extraction_result())

    # No vision calls — every page short-circuited.
    assert ollama.vision_calls == [], (
        "generate_vision must NOT be called when every page is at or "
        "above COMPANION_SKIP_MIN_WORDS"
    )

    # extract_page_text was consulted once per rendered page.
    assert len(pdf.extract_page_text_calls) == 4, (
        "extract_page_text must be called once per rendered page so "
        "the heuristic can decide to skip each one"
    )

    # Every page was skipped → generator returns the "no companion" shape.
    assert result.companion_generated is False
    assert result.pages_described == 0
    assert result.companion_text == ""


# ---------------------------------------------------------------------------
# Scenario (d) — mixed: some pages above, some below threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_enabled_mixed_pages_skips_only_dense_pages() -> None:
    """COMPANION_SKIP_ENABLED=True, mixed word counts → partial skip.

    Pages 0 and 1 have 5 words each (below threshold → vision runs).
    Pages -2 and -1 have 200 words each (above threshold → skipped).
    The vision model must be invoked exactly twice, the result
    reports ``pages_described=2`` and ``companion_generated=True``.

    This is the architectural reason the heuristic is per-page: a
    drawing set where the title block is sparse (page 0) but the
    body pages are dense (specs, notes) must still render the title
    block through the vision model.
    """
    sparse = "only five words in here"  # 5 words
    dense = " ".join(["word"] * 200)  # 200 words
    pdf = StubPDFProcessor(
        page_count=4,
        # _select_pages_to_render(4) returns [0, 1, -2, -1]. The stub
        # uses the literal page numbers as keys so the generator can
        # look up "page -1" directly without normalizing to a positive
        # index.
        page_text_map={
            0: sparse,
            1: sparse,
            -2: dense,
            -1: dense,
        },
    )
    ollama = SpyOllamaClient()
    settings = _make_settings(enabled=True, threshold=150)
    generator = _make_generator(pdf=pdf, ollama=ollama, settings=settings)

    context = _make_context(pdf)
    result = await generator.generate(context, _make_extraction_result())

    # Vision fired exactly twice (for the two sparse pages).
    assert len(ollama.vision_calls) == 2, (
        f"expected 2 vision calls (one per sparse page), got "
        f"{len(ollama.vision_calls)}"
    )

    # extract_page_text was consulted once per rendered page.
    assert len(pdf.extract_page_text_calls) == 4, (
        "extract_page_text must be called once per rendered page even "
        "when only some pages end up being skipped"
    )

    # Two pages contributed → companion was generated from the sparse
    # pages only.
    assert result.companion_generated is True
    assert result.pages_described == 2
