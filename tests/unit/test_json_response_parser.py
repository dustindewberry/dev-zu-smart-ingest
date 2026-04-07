"""Unit tests for :mod:`zubot_ingestion.domain.pipeline.json_parser`."""

from __future__ import annotations

import pytest

from zubot_ingestion.domain.pipeline.json_parser import JsonResponseParser


# ---------------------------------------------------------------------------
# Strategy 1: strict json.loads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parses_clean_json() -> None:
    parser = JsonResponseParser()
    result = await parser.parse('{"a": 1, "b": "two"}')
    assert result == {"a": 1, "b": "two"}


@pytest.mark.asyncio
async def test_parses_clean_json_with_nulls() -> None:
    parser = JsonResponseParser()
    result = await parser.parse('{"drawing_number": null, "confidence": 0.0}')
    assert result == {"drawing_number": None, "confidence": 0.0}


# ---------------------------------------------------------------------------
# Strategy 2: outer-brace slicing (strips fences/commentary)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strips_markdown_fences() -> None:
    parser = JsonResponseParser()
    raw = "```json\n{\"k\": 1}\n```"
    result = await parser.parse(raw)
    assert result == {"k": 1}


@pytest.mark.asyncio
async def test_extracts_json_buried_in_prose() -> None:
    parser = JsonResponseParser()
    raw = 'Sure! Here is the answer: {"drawing_number": "A-101", "confidence": 0.9} done.'
    result = await parser.parse(raw)
    assert result["drawing_number"] == "A-101"
    assert result["confidence"] == 0.9


# ---------------------------------------------------------------------------
# Strategy 3: json_repair fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_repair_fixes_trailing_comma() -> None:
    parser = JsonResponseParser()
    raw = '{"a": 1, "b": 2,}'
    result = await parser.parse(raw)
    assert result == {"a": 1, "b": 2}


@pytest.mark.asyncio
async def test_json_repair_fixes_unquoted_keys() -> None:
    parser = JsonResponseParser()
    raw = "{a: 1, b: 'two'}"
    result = await parser.parse(raw)
    assert result == {"a": 1, "b": "two"}


@pytest.mark.asyncio
async def test_json_repair_fixes_truncated_json() -> None:
    parser = JsonResponseParser()
    raw = '{"a": 1, "b": "incomplete'
    result = await parser.parse(raw)
    # json_repair will close it best-effort; we just want a dict back.
    assert isinstance(result, dict)
    assert result.get("a") == 1


# ---------------------------------------------------------------------------
# Strategy 4: regex KV fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regex_fallback_extracts_kvs_from_garbage() -> None:
    parser = JsonResponseParser()
    raw = 'Garbage prose with "drawing_number": "X-1" and "confidence": 0.5 stuff'
    # Note: this contains a brace-less structure so strict + repair both fail.
    # The leading prose has no `{` so the slice strategy is skipped.
    result = await parser.parse(raw.replace("{", "").replace("}", ""))
    # The regex should still find "drawing_number": "X-1" and "confidence": 0.5
    assert result.get("drawing_number") == "X-1"
    assert result.get("confidence") == 0.5


# ---------------------------------------------------------------------------
# Never-raise invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unparseable_returns_empty_dict_when_no_schema() -> None:
    parser = JsonResponseParser()
    result = await parser.parse("absolutely nothing parseable here")
    assert result == {}


@pytest.mark.asyncio
async def test_none_input_returns_empty_dict_when_no_schema() -> None:
    parser = JsonResponseParser()
    result = await parser.parse(None)  # type: ignore[arg-type]
    assert result == {}


@pytest.mark.asyncio
async def test_empty_input_returns_empty_dict_when_no_schema() -> None:
    parser = JsonResponseParser()
    result = await parser.parse("")
    assert result == {}


# ---------------------------------------------------------------------------
# Schema-fill behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_fills_missing_keys_with_defaults() -> None:
    parser = JsonResponseParser()
    schema = {"drawing_number": None, "confidence": 0.0, "title": None}
    result = await parser.parse('{"drawing_number": "X-1"}', schema)
    assert result == {"drawing_number": "X-1", "confidence": 0.0, "title": None}


@pytest.mark.asyncio
async def test_schema_preserves_extra_keys_from_model() -> None:
    parser = JsonResponseParser()
    schema = {"drawing_number": None}
    result = await parser.parse(
        '{"drawing_number": "X-1", "bonus_field": 42}', schema
    )
    assert result == {"drawing_number": "X-1", "bonus_field": 42}


@pytest.mark.asyncio
async def test_schema_returned_with_all_defaults_on_unparseable_input() -> None:
    parser = JsonResponseParser()
    schema = {"drawing_number": None, "confidence": 0.0}
    result = await parser.parse("nothing here", schema)
    assert result == {"drawing_number": None, "confidence": 0.0}


@pytest.mark.asyncio
async def test_schema_returned_when_input_is_none() -> None:
    parser = JsonResponseParser()
    schema = {"a": "default"}
    result = await parser.parse(None, schema)  # type: ignore[arg-type]
    assert result == {"a": "default"}


# ---------------------------------------------------------------------------
# Sync variant for IResponseParser-like callers
# ---------------------------------------------------------------------------


def test_parse_with_schema_sync_variant_works() -> None:
    parser = JsonResponseParser()
    schema = {"x": None}
    result = parser.parse_with_schema('{"x": 1}', schema)
    assert result == {"x": 1}


def test_parse_with_schema_never_raises_on_garbage() -> None:
    parser = JsonResponseParser()
    result = parser.parse_with_schema("garbage", {"a": None})
    assert result == {"a": None}
