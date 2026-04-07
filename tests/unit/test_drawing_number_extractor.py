"""Unit tests for :mod:`zubot_ingestion.domain.pipeline.extractors.drawing_number`."""

from __future__ import annotations

import json

import pytest

from zubot_ingestion.domain.entities import ExtractionResult
from zubot_ingestion.domain.pipeline.extractors.drawing_number import (
    CONFIDENCE_ALL_AGREE,
    CONFIDENCE_DISAGREEMENT,
    CONFIDENCE_SINGLE_SOURCE,
    CONFIDENCE_TWO_OF_THREE,
    DrawingNumberExtractor,
)
from zubot_ingestion.domain.pipeline.extractors.filename_parser import FilenameParser
from zubot_ingestion.domain.pipeline.json_parser import JsonResponseParser
from zubot_ingestion.infrastructure.pdf.processor import PyMuPDFProcessor

from tests.unit._extractor_helpers import (
    MockOllamaClient,
    calls_for,
    count_calls,
    make_pdf_bytes,
    make_pipeline_context,
)


def _build(client: MockOllamaClient) -> DrawingNumberExtractor:
    return DrawingNumberExtractor(
        pdf_processor=PyMuPDFProcessor(),
        ollama_client=client,  # type: ignore[arg-type]
        response_parser=JsonResponseParser(),
        filename_parser=FilenameParser(),
    )


# ---------------------------------------------------------------------------
# Confidence levels: all 3 sources agree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_three_sources_agree() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": "170154-L-001"})],
        text_responses=[json.dumps({"drawing_number": "170154-L-001"})],
    )
    extractor = _build(client)
    context = make_pipeline_context(filename="170154-L-001.pdf")
    result = await extractor.extract(context)
    assert isinstance(result, ExtractionResult)
    assert result.drawing_number == "170154-L-001"
    assert result.drawing_number_confidence == CONFIDENCE_ALL_AGREE
    assert set(result.sources_used) == {"vision", "text", "filename"}


# ---------------------------------------------------------------------------
# Confidence levels: 2 of 3 agree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vision_and_text_agree_filename_disagrees() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": "170154-L-001"})],
        text_responses=[json.dumps({"drawing_number": "170154-L-001"})],
    )
    extractor = _build(client)
    context = make_pipeline_context(filename="DWG-99.pdf")
    result = await extractor.extract(context)
    assert result.drawing_number == "170154-L-001"
    assert result.drawing_number_confidence == CONFIDENCE_TWO_OF_THREE


@pytest.mark.asyncio
async def test_vision_and_filename_agree_text_disagrees() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": "170154-L-001"})],
        text_responses=[json.dumps({"drawing_number": "OTHER-1"})],
    )
    extractor = _build(client)
    context = make_pipeline_context(filename="170154-L-001.pdf")
    result = await extractor.extract(context)
    assert result.drawing_number == "170154-L-001"
    assert result.drawing_number_confidence == CONFIDENCE_TWO_OF_THREE


@pytest.mark.asyncio
async def test_text_and_filename_agree_vision_disagrees() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": "ZZ-9"})],
        text_responses=[json.dumps({"drawing_number": "170154-L-001"})],
    )
    extractor = _build(client)
    context = make_pipeline_context(filename="170154-L-001.pdf")
    result = await extractor.extract(context)
    assert result.drawing_number == "170154-L-001"
    assert result.drawing_number_confidence == CONFIDENCE_TWO_OF_THREE


# ---------------------------------------------------------------------------
# Confidence levels: only one source produced a value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_filename_produces_value() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": None})],
        text_responses=[json.dumps({"drawing_number": None})],
    )
    extractor = _build(client)
    context = make_pipeline_context(filename="170154-L-001.pdf")
    result = await extractor.extract(context)
    assert result.drawing_number == "170154-L-001"
    assert result.drawing_number_confidence == CONFIDENCE_SINGLE_SOURCE
    assert result.sources_used == ["filename"]


@pytest.mark.asyncio
async def test_only_vision_produces_value() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": "VIS-1"})],
        text_responses=[json.dumps({"drawing_number": None})],
    )
    extractor = _build(client)
    context = make_pipeline_context(filename="random.pdf")
    result = await extractor.extract(context)
    assert result.drawing_number == "VIS-1"
    assert result.drawing_number_confidence == CONFIDENCE_SINGLE_SOURCE
    assert result.sources_used == ["vision"]


# ---------------------------------------------------------------------------
# Confidence levels: total disagreement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_distinct_values_disagreement() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": "VIS-1"})],
        text_responses=[json.dumps({"drawing_number": "TXT-2"})],
    )
    extractor = _build(client)
    # Filename produces yet a different value: 170154-L-001
    context = make_pipeline_context(filename="170154-L-001.pdf")
    result = await extractor.extract(context)
    # Vision wins by source priority
    assert result.drawing_number == "VIS-1"
    assert result.drawing_number_confidence == CONFIDENCE_DISAGREEMENT


# ---------------------------------------------------------------------------
# All sources null
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_sources_produce_value() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": None})],
        text_responses=[json.dumps({"drawing_number": None})],
    )
    extractor = _build(client)
    context = make_pipeline_context(filename="random.pdf")
    result = await extractor.extract(context)
    assert result.drawing_number is None
    assert result.drawing_number_confidence == 0.0
    assert result.sources_used == []


# ---------------------------------------------------------------------------
# Robustness: malformed JSON, exceptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handles_malformed_json_via_repair() -> None:
    client = MockOllamaClient(
        # Trailing comma — repaired via json_repair
        vision_responses=['{"drawing_number": "170154-L-001",}'],
        text_responses=[json.dumps({"drawing_number": "170154-L-001"})],
    )
    extractor = _build(client)
    context = make_pipeline_context(filename="170154-L-001.pdf")
    result = await extractor.extract(context)
    assert result.drawing_number == "170154-L-001"
    assert result.drawing_number_confidence == CONFIDENCE_ALL_AGREE


@pytest.mark.asyncio
async def test_vision_failure_does_not_break_pipeline() -> None:
    client = MockOllamaClient(
        text_responses=[json.dumps({"drawing_number": "170154-L-001"})],
        raise_on_vision=RuntimeError("ollama down"),
    )
    extractor = _build(client)
    context = make_pipeline_context(filename="170154-L-001.pdf")
    result = await extractor.extract(context)
    # Vision failed → 2 sources contributed; they agree → 2-of-3 vote.
    assert result.drawing_number == "170154-L-001"
    assert result.drawing_number_confidence == CONFIDENCE_TWO_OF_THREE
    assert "vision" not in result.sources_used


@pytest.mark.asyncio
async def test_text_failure_does_not_break_pipeline() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": "170154-L-001"})],
        raise_on_text=RuntimeError("ollama text down"),
    )
    extractor = _build(client)
    context = make_pipeline_context(filename="170154-L-001.pdf")
    result = await extractor.extract(context)
    assert result.drawing_number == "170154-L-001"
    assert result.drawing_number_confidence == CONFIDENCE_TWO_OF_THREE
    assert "text" not in result.sources_used


# ---------------------------------------------------------------------------
# Wiring assertions: prompts, models, temperature
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calls_use_temperature_zero() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": "X-1"})],
        text_responses=[json.dumps({"drawing_number": "X-1"})],
    )
    extractor = _build(client)
    context = make_pipeline_context(filename="170154-L-001.pdf")
    await extractor.extract(context)
    assert all(c.temperature == 0.0 for c in client.calls)


@pytest.mark.asyncio
async def test_vision_call_uses_v1_prompt_template() -> None:
    from zubot_ingestion.domain.pipeline.extractors.drawing_number import (
        DRAWING_NUMBER_VISION_PROMPT_V1,
    )

    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": "X-1"})],
        text_responses=[json.dumps({"drawing_number": "X-1"})],
    )
    extractor = _build(client)
    context = make_pipeline_context()
    await extractor.extract(context)
    vision_calls = calls_for(client, "vision")
    assert len(vision_calls) == 1
    assert vision_calls[0].prompt == DRAWING_NUMBER_VISION_PROMPT_V1


@pytest.mark.asyncio
async def test_text_call_uses_v1_prompt_template() -> None:
    from zubot_ingestion.domain.pipeline.extractors.drawing_number import (
        DRAWING_NUMBER_TEXT_PROMPT_V1,
    )

    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": "X-1"})],
        text_responses=[json.dumps({"drawing_number": "X-1"})],
    )
    extractor = _build(client)
    context = make_pipeline_context()
    await extractor.extract(context)
    text_calls = calls_for(client, "text")
    assert len(text_calls) == 1
    assert text_calls[0].prompt == DRAWING_NUMBER_TEXT_PROMPT_V1


@pytest.mark.asyncio
async def test_only_one_vision_and_one_text_call_per_extract() -> None:
    """Sanity: each source is called exactly once per ``extract()``."""
    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": "X-1"})],
        text_responses=[json.dumps({"drawing_number": "X-1"})],
    )
    extractor = _build(client)
    context = make_pipeline_context()
    await extractor.extract(context)
    assert count_calls(client, "vision") == 1
    assert count_calls(client, "text") == 1


@pytest.mark.asyncio
async def test_pipeline_context_caches_rendered_pages_and_text() -> None:
    """The extractor should populate context.rendered_pages + extracted_text."""
    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": "X-1"})],
        text_responses=[json.dumps({"drawing_number": "X-1"})],
    )
    extractor = _build(client)
    context = make_pipeline_context(pdf_bytes=make_pdf_bytes())
    await extractor.extract(context)
    assert any(p.page_number == 0 for p in context.rendered_pages)
    assert context.extracted_text is not None
    assert context.filename_hints is not None


@pytest.mark.asyncio
async def test_normalisation_treats_whitespace_and_case_as_equal() -> None:
    """Vision returns "  170154-l-001 " — should still vote with filename "170154-L-001"."""
    client = MockOllamaClient(
        vision_responses=[json.dumps({"drawing_number": "  170154-l-001 "})],
        text_responses=[json.dumps({"drawing_number": "170154-L-001"})],
    )
    extractor = _build(client)
    context = make_pipeline_context(filename="170154-L-001.pdf")
    result = await extractor.extract(context)
    assert result.drawing_number == "170154-L-001"
    assert result.drawing_number_confidence == CONFIDENCE_ALL_AGREE
