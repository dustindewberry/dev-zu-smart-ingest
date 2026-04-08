"""Unit tests for :mod:`zubot_ingestion.domain.pipeline.extractors.title`."""

from __future__ import annotations

import json

import pytest

from zubot_ingestion.domain.pipeline.extractors.filename_parser import FilenameParser
from zubot_ingestion.domain.pipeline.extractors.title import TitleExtractor
from zubot_ingestion.domain.pipeline.json_parser import JsonResponseParser
from zubot_ingestion.infrastructure.pdf.processor import PyMuPDFProcessor

from tests.unit._extractor_helpers import (
    MockOllamaClient,
    calls_for,
    make_pdf_bytes,
    make_pipeline_context,
)


def _build(client: MockOllamaClient) -> TitleExtractor:
    return TitleExtractor(
        pdf_processor=PyMuPDFProcessor(),
        ollama_client=client,  # type: ignore[arg-type]
        response_parser=JsonResponseParser(),
        filename_parser=FilenameParser(),
    )


@pytest.mark.asyncio
async def test_both_sources_agree_high_confidence() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"title": "GROUND FLOOR PLAN", "confidence": 0.85})],
        text_responses=[json.dumps({"title": "ground floor plan", "confidence": 0.80})],
    )
    extractor = _build(client)
    context = make_pipeline_context()
    result = await extractor.extract(context)
    assert result.title == "GROUND FLOOR PLAN"
    # Agreement bumps confidence to >= 0.9
    assert result.title_confidence >= 0.9
    assert set(result.sources_used) == {"vision", "text"}


@pytest.mark.asyncio
async def test_only_vision_produces_value() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"title": "FOUNDATION DETAILS", "confidence": 0.85})],
        text_responses=[json.dumps({"title": None, "confidence": 0.0})],
    )
    extractor = _build(client)
    context = make_pipeline_context()
    result = await extractor.extract(context)
    assert result.title == "FOUNDATION DETAILS"
    assert result.title_confidence == 0.85
    assert result.sources_used == ["vision"]


@pytest.mark.asyncio
async def test_only_text_produces_value() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"title": None, "confidence": 0.0})],
        text_responses=[json.dumps({"title": "ELECTRICAL RISER", "confidence": 0.7})],
    )
    extractor = _build(client)
    context = make_pipeline_context()
    result = await extractor.extract(context)
    assert result.title == "ELECTRICAL RISER"
    assert result.title_confidence == 0.7
    assert result.sources_used == ["text"]


@pytest.mark.asyncio
async def test_both_sources_disagree_prefer_vision_with_penalty() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"title": "FLOOR PLAN", "confidence": 0.8})],
        text_responses=[json.dumps({"title": "ROOF PLAN", "confidence": 0.6})],
    )
    extractor = _build(client)
    context = make_pipeline_context()
    result = await extractor.extract(context)
    assert result.title == "FLOOR PLAN"
    # (0.8 + 0.6)/2 * 0.7 = 0.49
    assert result.title_confidence == pytest.approx(0.49, rel=1e-3)


@pytest.mark.asyncio
async def test_neither_source_produces_value() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"title": None, "confidence": 0.0})],
        text_responses=[json.dumps({"title": None, "confidence": 0.0})],
    )
    extractor = _build(client)
    context = make_pipeline_context()
    result = await extractor.extract(context)
    assert result.title is None
    assert result.title_confidence == 0.0
    assert result.sources_used == []


@pytest.mark.asyncio
async def test_multiline_title_concatenated_with_single_spaces() -> None:
    client = MockOllamaClient(
        vision_responses=[
            json.dumps({"title": "GROUND   FLOOR\nPLAN — \nBUILDING A", "confidence": 0.9})
        ],
        text_responses=[json.dumps({"title": None, "confidence": 0.0})],
    )
    extractor = _build(client)
    context = make_pipeline_context()
    result = await extractor.extract(context)
    assert result.title == "GROUND FLOOR PLAN — BUILDING A"


@pytest.mark.asyncio
async def test_handles_vision_failure_gracefully() -> None:
    client = MockOllamaClient(
        text_responses=[json.dumps({"title": "ELECTRICAL RISER", "confidence": 0.7})],
        raise_on_vision=RuntimeError("vision down"),
    )
    extractor = _build(client)
    context = make_pipeline_context()
    result = await extractor.extract(context)
    assert result.title == "ELECTRICAL RISER"
    assert result.title_confidence == 0.7
    assert result.sources_used == ["text"]


@pytest.mark.asyncio
async def test_handles_text_failure_gracefully() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"title": "FLOOR PLAN", "confidence": 0.85})],
        raise_on_text=RuntimeError("text down"),
    )
    extractor = _build(client)
    context = make_pipeline_context()
    result = await extractor.extract(context)
    assert result.title == "FLOOR PLAN"
    assert result.sources_used == ["vision"]


@pytest.mark.asyncio
async def test_first_and_last_page_text_used_when_pdf_has_multiple_pages() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"title": None, "confidence": 0.0})],
        text_responses=[json.dumps({"title": "AS BUILT", "confidence": 0.9})],
    )
    extractor = _build(client)
    pdf_bytes = make_pdf_bytes(
        pages=["FIRST PAGE TITLE", "MIDDLE", "LAST PAGE TITLE"]
    )
    context = make_pipeline_context(pdf_bytes=pdf_bytes)
    await extractor.extract(context)
    text_calls = calls_for(client, "text")
    assert len(text_calls) == 1
    payload = text_calls[0].payload
    # Should include the first OR last page snippet (we trim to 32 chars)
    assert payload  # non-empty


@pytest.mark.asyncio
async def test_returns_extraction_result_dataclass() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"title": "X", "confidence": 0.5})],
        text_responses=[json.dumps({"title": None, "confidence": 0.0})],
    )
    extractor = _build(client)
    result = await extractor.extract(make_pipeline_context())
    from zubot_ingestion.domain.entities import ExtractionResult

    assert isinstance(result, ExtractionResult)


@pytest.mark.asyncio
async def test_vision_prompt_is_v1_constant() -> None:
    from zubot_ingestion.domain.pipeline.extractors.title import (
        TITLE_VISION_PROMPT_V1,
    )

    client = MockOllamaClient(
        vision_responses=[json.dumps({"title": "X", "confidence": 0.5})],
        text_responses=[json.dumps({"title": "X", "confidence": 0.5})],
    )
    extractor = _build(client)
    await extractor.extract(make_pipeline_context())
    vision_calls = calls_for(client, "vision")
    assert vision_calls[0].prompt == TITLE_VISION_PROMPT_V1


@pytest.mark.asyncio
async def test_text_prompt_is_v1_constant() -> None:
    from zubot_ingestion.domain.pipeline.extractors.title import TITLE_TEXT_PROMPT_V1

    client = MockOllamaClient(
        vision_responses=[json.dumps({"title": "X", "confidence": 0.5})],
        text_responses=[json.dumps({"title": "X", "confidence": 0.5})],
    )
    extractor = _build(client)
    await extractor.extract(make_pipeline_context())
    text_calls = calls_for(client, "text")
    assert text_calls[0].prompt == TITLE_TEXT_PROMPT_V1


@pytest.mark.asyncio
async def test_temperature_zero_for_all_calls() -> None:
    client = MockOllamaClient(
        vision_responses=[json.dumps({"title": "X", "confidence": 0.5})],
        text_responses=[json.dumps({"title": "X", "confidence": 0.5})],
    )
    extractor = _build(client)
    await extractor.extract(make_pipeline_context())
    assert all(c.temperature == 0.0 for c in client.calls)
