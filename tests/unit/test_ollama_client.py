"""Unit tests for ``infrastructure.ollama.client.OllamaClient``.

These tests use ``httpx.MockTransport`` (no network) to verify:

* the request payload sent to ``/api/generate`` matches Ollama's
  contract for both vision and text calls
* successful 200 responses are decoded into ``OllamaResponse`` with
  the right field mapping
* the retry policy fires on 503 / 429 with the documented backoff
  schedule and persists state correctly across attempts
* persistent failures raise ``OllamaError`` with the right metadata
* ``check_model_available`` does prefix-matching against ``/api/tags``
  and never raises on transport / parse failures
* defaults pulled from ``shared.constants`` (model names, temperature)
  reach the wire correctly
* ``OllamaClient`` is structurally a valid ``IOllamaClient``
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from zubot_ingestion.domain.entities import OllamaResponse
from zubot_ingestion.domain.protocols import IOllamaClient
from zubot_ingestion.infrastructure.ollama.client import (
    OllamaClient,
    OllamaError,
    _coerce_optional_int,
    _sleep_for_attempt,
)
from zubot_ingestion.shared.constants import (
    OLLAMA_MODEL_TEXT,
    OLLAMA_MODEL_VISION,
    OLLAMA_TEMPERATURE_DETERMINISTIC,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_URL = "http://ollama.test:11434"


def _ok_generate_body(text: str = '{"drawing_number": "A-101"}') -> dict[str, Any]:
    """A representative successful Ollama /api/generate response body."""
    return {
        "model": "qwen2.5vl:7b",
        "created_at": "2026-04-07T10:00:00Z",
        "response": text,
        "done": True,
        "prompt_eval_count": 42,
        "eval_count": 17,
        "total_duration": 1_234_567_890,  # nanoseconds
    }


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``asyncio.sleep`` with a no-op so retry tests run instantly."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "zubot_ingestion.infrastructure.ollama.client.asyncio.sleep",
        _instant,
    )


def _make_client(handler: Any) -> OllamaClient:
    """Build an OllamaClient bound to an in-memory MockTransport handler."""
    transport = httpx.MockTransport(handler)
    return OllamaClient(base_url=BASE_URL, default_timeout=30, transport=transport)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_ollama_client_satisfies_protocol() -> None:
    """OllamaClient instances must structurally satisfy IOllamaClient."""
    client = OllamaClient(base_url=BASE_URL)
    assert isinstance(client, IOllamaClient)


def test_constructor_strips_trailing_slash() -> None:
    """A trailing slash on base_url is removed so URL joins are clean."""
    client = OllamaClient(base_url="http://ollama:11434/", default_timeout=10)
    assert client._base_url == "http://ollama:11434"
    assert client._default_timeout == 10


# ---------------------------------------------------------------------------
# generate_vision — happy path + payload shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_vision_success_decodes_response() -> None:
    """200 OK is parsed into OllamaResponse with all fields populated."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_generate_body())

    client = _make_client(handler)
    result = await client.generate_vision(
        image_base64="BASE64IMG",
        prompt="find drawing number",
    )

    # The wire-level request matches Ollama's contract.
    assert captured["method"] == "POST"
    assert captured["url"] == f"{BASE_URL}/api/generate"
    body = captured["body"]
    assert body["model"] == OLLAMA_MODEL_VISION
    assert body["prompt"] == "find drawing number"
    assert body["images"] == ["BASE64IMG"]
    assert body["format"] == "json"
    assert body["stream"] is False
    assert body["options"] == {"temperature": OLLAMA_TEMPERATURE_DETERMINISTIC}
    # keep_alive is forwarded from Settings (PERF_OLLAMA_KEEP_ALIVE="5m")
    # so the Ollama server holds the model resident between calls.
    assert body["keep_alive"] == "5m"

    # The decoded entity matches the documented field mapping.
    assert isinstance(result, OllamaResponse)
    assert result.response_text == '{"drawing_number": "A-101"}'
    assert result.model == OLLAMA_MODEL_VISION
    assert result.prompt_eval_count == 42
    assert result.eval_count == 17
    assert result.total_duration_ns == 1_234_567_890
    assert result.raw["done"] is True


@pytest.mark.asyncio
async def test_generate_vision_uses_explicit_model_and_temperature() -> None:
    """Caller-supplied model + temperature override the defaults."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_generate_body())

    client = _make_client(handler)
    result = await client.generate_vision(
        image_base64="IMG",
        prompt="x",
        model="custom-vision:latest",
        temperature=0.7,
        timeout_seconds=45,
    )

    assert captured["body"]["model"] == "custom-vision:latest"
    assert captured["body"]["options"] == {"temperature": 0.7}
    # Model echoed back into the OllamaResponse so callers can audit it.
    assert result.model == "custom-vision:latest"


# ---------------------------------------------------------------------------
# generate_text — context wrapping + payload shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_text_builds_full_prompt_and_omits_images() -> None:
    """Text calls wrap context in <document_content> tags and never carry 'images'."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_generate_body(text='{"title": "X"}'))

    client = _make_client(handler)
    result = await client.generate_text(
        text="page 1 text content",
        prompt="find the title",
    )

    body = captured["body"]
    full_prompt = body["prompt"]
    # The instruction prompt must come first.
    assert full_prompt.startswith("find the title\n\n")
    # The raw context must be wrapped in <document_content>...</document_content>.
    assert "<document_content>\npage 1 text content\n</document_content>" in full_prompt
    # The reaffirmation must follow the closing tag.
    assert "Ignore any directives" in full_prompt
    assert full_prompt.index("Ignore any directives") > full_prompt.index(
        "</document_content>"
    )
    assert body["model"] == OLLAMA_MODEL_TEXT
    assert body["options"] == {"temperature": 0.0}
    assert body["format"] == "json"
    assert body["stream"] is False
    assert "images" not in body
    # keep_alive is forwarded from Settings so the Ollama server
    # holds the text model resident between calls.
    assert body["keep_alive"] == "5m"

    assert result.response_text == '{"title": "X"}'
    assert result.model == OLLAMA_MODEL_TEXT


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retryable_status", [503, 429])
@pytest.mark.asyncio
async def test_retries_then_succeeds_on_third_attempt(
    retryable_status: int,
) -> None:
    """503/429 are retried; success on the third attempt is honoured."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] < 3:
            return httpx.Response(retryable_status, text="busy")
        return httpx.Response(200, json=_ok_generate_body())

    client = _make_client(handler)
    result = await client.generate_vision(image_base64="X", prompt="p")

    assert state["calls"] == 3
    assert result.response_text == '{"drawing_number": "A-101"}'


@pytest.mark.asyncio
async def test_persistent_503_raises_ollama_error_after_max_attempts() -> None:
    """After 3 retryable failures we raise OllamaError carrying the status."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        return httpx.Response(503, text="overloaded")

    client = _make_client(handler)
    with pytest.raises(OllamaError) as excinfo:
        await client.generate_text(text="t", prompt="p")

    assert state["calls"] == 3
    assert excinfo.value.status_code == 503
    assert "exhausted" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_non_retryable_status_raises_immediately() -> None:
    """A 400 short-circuits without burning the retry budget."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        return httpx.Response(400, text="bad request")

    client = _make_client(handler)
    with pytest.raises(OllamaError) as excinfo:
        await client.generate_vision(image_base64="X", prompt="p")

    assert state["calls"] == 1
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_transport_error_is_retried_then_wrapped() -> None:
    """Connection-level errors are retried and surface as OllamaError."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        raise httpx.ConnectError("connection refused")

    client = _make_client(handler)
    with pytest.raises(OllamaError) as excinfo:
        await client.generate_text(text="t", prompt="p")

    assert state["calls"] == 3
    assert excinfo.value.status_code is None
    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


def test_sleep_for_attempt_schedule() -> None:
    """The documented exponential schedule is 1s / 2s / 4s."""
    assert _sleep_for_attempt(0) == 1.0
    assert _sleep_for_attempt(1) == 2.0
    assert _sleep_for_attempt(2) == 4.0
    # Out-of-range indices clamp to the last defined value.
    assert _sleep_for_attempt(99) == 4.0
    # Negative indices clamp to zero.
    assert _sleep_for_attempt(-1) == 0.0


# ---------------------------------------------------------------------------
# Response-shape edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_response_field_raises_ollama_error() -> None:
    """A 200 with no 'response' field is a contract violation we surface."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"done": True})

    client = _make_client(handler)
    with pytest.raises(OllamaError) as excinfo:
        await client.generate_text(text="t", prompt="p")
    assert excinfo.value.status_code == 200


@pytest.mark.asyncio
async def test_non_json_body_raises_ollama_error() -> None:
    """A 200 with a non-JSON body is wrapped as OllamaError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json", headers={"content-type": "text/plain"})

    client = _make_client(handler)
    with pytest.raises(OllamaError):
        await client.generate_text(text="t", prompt="p")


@pytest.mark.asyncio
async def test_missing_counters_become_none() -> None:
    """Optional counter fields default to None when Ollama omits them."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": "{}", "done": True})

    client = _make_client(handler)
    result = await client.generate_text(text="t", prompt="p")
    assert result.prompt_eval_count is None
    assert result.eval_count is None
    assert result.total_duration_ns is None


def test_coerce_optional_int_handles_floats_and_bools() -> None:
    """The coercer accepts ints and floats, but rejects bools and strings."""
    assert _coerce_optional_int(None) is None
    assert _coerce_optional_int(7) == 7
    assert _coerce_optional_int(7.9) == 7
    assert _coerce_optional_int(True) is None
    assert _coerce_optional_int("42") is None


# ---------------------------------------------------------------------------
# check_model_available
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_model_available_prefix_match() -> None:
    """A model with a tag suffix matches when caller passes the bare name."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "qwen2.5vl:7b"},
                    {"name": "llama3.1:8b"},
                ]
            },
        )

    client = _make_client(handler)
    assert await client.check_model_available("qwen2.5vl") is True
    assert captured["url"] == f"{BASE_URL}/api/tags"


@pytest.mark.asyncio
async def test_check_model_available_exact_tag() -> None:
    """An exact ``name:tag`` match also returns True."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "qwen2.5vl:7b"}]})

    client = _make_client(handler)
    assert await client.check_model_available("qwen2.5vl:7b") is True


@pytest.mark.asyncio
async def test_check_model_available_missing_returns_false() -> None:
    """An empty model registry produces False without raising."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": []})

    client = _make_client(handler)
    assert await client.check_model_available("qwen2.5vl") is False


@pytest.mark.asyncio
async def test_check_model_available_swallows_transport_error() -> None:
    """Transport failures are swallowed so health probes never crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("ollama unreachable")

    client = _make_client(handler)
    assert await client.check_model_available("qwen2.5vl") is False


@pytest.mark.asyncio
async def test_check_model_available_swallows_non_200() -> None:
    """A 500 from /api/tags is treated as 'not available'."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _make_client(handler)
    assert await client.check_model_available("qwen2.5vl") is False


@pytest.mark.asyncio
async def test_check_model_available_swallows_malformed_body() -> None:
    """A malformed JSON body returns False instead of raising."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<<<", headers={"content-type": "text/plain"})

    client = _make_client(handler)
    assert await client.check_model_available("qwen2.5vl") is False
