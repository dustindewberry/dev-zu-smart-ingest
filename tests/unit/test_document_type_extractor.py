"""Unit tests for :mod:`zubot_ingestion.domain.pipeline.extractors.document_type`."""

from __future__ import annotations

import json

import pytest

from zubot_ingestion.domain.enums import DocumentType
from zubot_ingestion.domain.pipeline.extractors.document_type import (
    DOCUMENT_TYPE_TEXT_PROMPT_V1,
    DocumentTypeExtractor,
)
from zubot_ingestion.domain.pipeline.extractors.filename_parser import FilenameParser
from zubot_ingestion.domain.pipeline.json_parser import JsonResponseParser
from zubot_ingestion.infrastructure.pdf.processor import PyMuPDFProcessor

from tests.unit._extractor_helpers import (
    MockOllamaClient,
    calls_for,
    make_pipeline_context,
)


def _build(client: MockOllamaClient) -> DocumentTypeExtractor:
    return DocumentTypeExtractor(
        pdf_processor=PyMuPDFProcessor(),
        ollama_client=client,  # type: ignore[arg-type]
        response_parser=JsonResponseParser(),
        filename_parser=FilenameParser(),
    )


# ---------------------------------------------------------------------------
# Happy path: in-vocabulary returns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recognises_technical_drawing() -> None:
    client = MockOllamaClient(
        text_responses=[
            json.dumps({"document_type": "technical_drawing", "confidence": 0.92})
        ]
    )
    extractor = _build(client)
    result = await extractor.extract(make_pipeline_context())
    assert result.document_type is DocumentType.TECHNICAL_DRAWING
    assert result.document_type_confidence == 0.92
    assert result.sources_used == ["text"]


@pytest.mark.asyncio
async def test_recognises_floor_plan() -> None:
    client = MockOllamaClient(
        text_responses=[
            json.dumps({"document_type": "floor_plan", "confidence": 0.88})
        ]
    )
    extractor = _build(client)
    result = await extractor.extract(make_pipeline_context())
    assert result.document_type is DocumentType.FLOOR_PLAN


@pytest.mark.asyncio
async def test_recognises_inspection_report() -> None:
    client = MockOllamaClient(
        text_responses=[
            json.dumps({"document_type": "inspection_report", "confidence": 0.75})
        ]
    )
    extractor = _build(client)
    result = await extractor.extract(make_pipeline_context())
    assert result.document_type is DocumentType.INSPECTION_REPORT


@pytest.mark.asyncio
async def test_recognises_fallback_document_value() -> None:
    client = MockOllamaClient(
        text_responses=[
            json.dumps({"document_type": "document", "confidence": 0.5})
        ]
    )
    extractor = _build(client)
    result = await extractor.extract(make_pipeline_context())
    assert result.document_type is DocumentType.DOCUMENT
    assert result.document_type_confidence == 0.5


# ---------------------------------------------------------------------------
# Out-of-vocabulary handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_out_of_vocabulary_falls_back_to_document_with_capped_conf() -> None:
    client = MockOllamaClient(
        text_responses=[
            json.dumps({"document_type": "WIDGET_INVOICE", "confidence": 0.95})
        ]
    )
    extractor = _build(client)
    result = await extractor.extract(make_pipeline_context())
    assert result.document_type is DocumentType.DOCUMENT
    # Capped at the fallback ceiling (0.30) regardless of model's reported value
    assert result.document_type_confidence == 0.30


@pytest.mark.asyncio
async def test_out_of_vocabulary_with_low_reported_confidence() -> None:
    client = MockOllamaClient(
        text_responses=[
            json.dumps({"document_type": "made_up", "confidence": 0.10})
        ]
    )
    extractor = _build(client)
    result = await extractor.extract(make_pipeline_context())
    assert result.document_type is DocumentType.DOCUMENT
    # min(0.10, 0.30) == 0.10
    assert result.document_type_confidence == 0.10


# ---------------------------------------------------------------------------
# Null / missing values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_document_type_returns_none() -> None:
    client = MockOllamaClient(
        text_responses=[json.dumps({"document_type": None, "confidence": 0.0})]
    )
    extractor = _build(client)
    result = await extractor.extract(make_pipeline_context())
    assert result.document_type is None
    assert result.document_type_confidence == 0.0
    assert result.sources_used == []


@pytest.mark.asyncio
async def test_missing_document_type_field_returns_none() -> None:
    client = MockOllamaClient(text_responses=[json.dumps({"foo": "bar"})])
    extractor = _build(client)
    result = await extractor.extract(make_pipeline_context())
    assert result.document_type is None


# ---------------------------------------------------------------------------
# Robustness: failures, malformed JSON
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handles_ollama_failure_gracefully() -> None:
    client = MockOllamaClient(raise_on_text=RuntimeError("ollama down"))
    extractor = _build(client)
    result = await extractor.extract(make_pipeline_context())
    assert result.document_type is None
    assert result.document_type_confidence == 0.0
    assert result.sources_used == []


@pytest.mark.asyncio
async def test_handles_malformed_json_via_repair() -> None:
    client = MockOllamaClient(
        text_responses=['{"document_type": "specification", "confidence": 0.7,}']
    )
    extractor = _build(client)
    result = await extractor.extract(make_pipeline_context())
    assert result.document_type is DocumentType.SPECIFICATION
    assert result.document_type_confidence == 0.7


# ---------------------------------------------------------------------------
# Prompt + temperature wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uses_v1_prompt_template() -> None:
    client = MockOllamaClient(
        text_responses=[json.dumps({"document_type": "document", "confidence": 0.5})]
    )
    extractor = _build(client)
    await extractor.extract(make_pipeline_context())
    text_calls = calls_for(client, "text")
    assert len(text_calls) == 1
    assert text_calls[0].prompt == DOCUMENT_TYPE_TEXT_PROMPT_V1


@pytest.mark.asyncio
async def test_uses_temperature_zero() -> None:
    client = MockOllamaClient(
        text_responses=[json.dumps({"document_type": "document", "confidence": 0.5})]
    )
    extractor = _build(client)
    await extractor.extract(make_pipeline_context())
    assert all(c.temperature == 0.0 for c in client.calls)


@pytest.mark.asyncio
async def test_prompt_lists_all_21_enum_values() -> None:
    """The auto-generated V1 prompt MUST enumerate every DocumentType value."""
    for member in DocumentType:
        assert member.value in DOCUMENT_TYPE_TEXT_PROMPT_V1


def test_prompt_mentions_correct_count() -> None:
    """Sanity check: prompt header references the count of enum values."""
    n = len(list(DocumentType))
    assert n == 21
    assert f"{n} controlled-vocabulary types" in DOCUMENT_TYPE_TEXT_PROMPT_V1
