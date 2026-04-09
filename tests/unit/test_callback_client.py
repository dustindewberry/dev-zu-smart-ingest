"""Unit tests for ``infrastructure.callback.client``.

These tests use ``httpx.MockTransport`` (no network) and a direct
monkeypatch of ``asyncio.sleep`` (to skip real backoff waits) to
verify:

* :func:`compute_signature` produces the documented HMAC-SHA256 hex
  digest and the ``X-Zubot-Signature`` header contains the same value
  the receiver would compute from the raw request body
* 5xx responses retry up to ``max_retries`` times with exponential
  backoff and return ``False`` when exhausted (never raising)
* a 2xx response short-circuits the retry loop and returns ``True``
* 4xx responses are treated as non-retryable and return ``False``
  immediately
* transport errors participate in the same retry budget as 5xx
* :class:`NoOpCallbackClient` is a no-op that always returns ``True``
* :func:`build_callback_client` routes to NoOp when
  ``CALLBACK_ENABLED`` is ``False`` and to the real client otherwise
* :class:`CallbackHttpClient` and :class:`NoOpCallbackClient` both
  structurally match :class:`ICallbackClient` (runtime-checkable
  Protocol) and their ``notify_completion`` signature matches the
  protocol declaration exactly (name / kind / order / annotation)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from zubot_ingestion.domain.entities import Job
from zubot_ingestion.domain.enums import ConfidenceTier, JobStatus
from zubot_ingestion.domain.protocols import ICallbackClient
from zubot_ingestion.infrastructure.callback import (
    CallbackHttpClient,
    NoOpCallbackClient,
    build_callback_client,
)
from zubot_ingestion.infrastructure.callback.client import (
    API_KEY_HEADER,
    SIGNATURE_HEADER,
    _backoff_delay,
    _job_to_payload,
    _serialise_body,
    compute_signature,
)
from zubot_ingestion.shared.types import BatchId, FileHash, JobId

CALLBACK_URL = "https://client.example.test/webhook"
API_KEY = "test-api-key"
SIGNING_SECRET = "super-secret-signing-key"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_job(job_id: UUID | None = None) -> Job:
    now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
    return Job(
        job_id=JobId(job_id or uuid4()),
        batch_id=BatchId(uuid4()),
        filename="KXC-B6-001-Y-27-1905-301.pdf",
        file_hash=FileHash("a" * 64),
        file_path="/tmp/pdfs/KXC-B6-001-Y-27-1905-301.pdf",
        status=JobStatus.COMPLETED,
        result={"drawing_number": "KXC-B6-001-Y-27-1905-301", "title": "Plan"},
        error_message=None,
        pipeline_trace=None,
        otel_trace_id="0123456789abcdef0123456789abcdef",
        processing_time_ms=4321,
        created_at=now,
        updated_at=now,
        confidence_tier=ConfidenceTier.AUTO,
        overall_confidence=0.91,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip real ``asyncio.sleep`` calls so retries do not block tests."""

    async def _fast_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(
        "zubot_ingestion.infrastructure.callback.client.asyncio.sleep",
        _fast_sleep,
    )


_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _AsyncClientFactory:
    """Monkey-patchable ``httpx.AsyncClient`` factory injecting a transport."""

    def __init__(self, handler: Any) -> None:
        self._transport = httpx.MockTransport(handler)

    def __call__(self, *args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(transport=self._transport, **kwargs)


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> None:
    factory = _AsyncClientFactory(handler)
    monkeypatch.setattr(
        "zubot_ingestion.infrastructure.callback.client.httpx.AsyncClient",
        factory,
    )


# ---------------------------------------------------------------------------
# compute_signature / payload helpers
# ---------------------------------------------------------------------------


def test_compute_signature_matches_expected_hmac() -> None:
    body = b'{"job_id":"abc","status":"completed"}'
    expected = hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    assert compute_signature(body, SIGNING_SECRET) == expected


def test_serialise_body_is_stable_and_sorted() -> None:
    job = _make_job()
    payload = _job_to_payload(job)
    body_a = _serialise_body(payload)
    body_b = _serialise_body(payload)
    assert body_a == body_b
    decoded = json.loads(body_a.decode("utf-8"))
    assert decoded["job_id"] == str(job.job_id)
    assert decoded["status"] == "completed"
    assert decoded["confidence_tier"] == "auto"


def test_backoff_delay_exponential() -> None:
    assert _backoff_delay(1) == pytest.approx(0.5)
    assert _backoff_delay(2) == pytest.approx(1.0)
    assert _backoff_delay(3) == pytest.approx(2.0)
    assert _backoff_delay(0) == 0.0


# ---------------------------------------------------------------------------
# CallbackHttpClient — signature header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_completion_sends_signed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = bytes(request.content)
        return httpx.Response(200, json={"received": True})

    _install_mock_transport(monkeypatch, handler)

    client = CallbackHttpClient(signing_secret=SIGNING_SECRET)
    job = _make_job()
    ok = await client.notify_completion(CALLBACK_URL, job, API_KEY)

    assert ok is True
    assert captured["method"] == "POST"
    assert captured["url"] == CALLBACK_URL

    raw = captured["body"]
    expected_sig = hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        raw,
        hashlib.sha256,
    ).hexdigest()
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers[SIGNATURE_HEADER.lower()] == expected_sig
    assert headers[API_KEY_HEADER.lower()] == API_KEY
    assert headers["content-type"].startswith("application/json")

    decoded = json.loads(raw.decode("utf-8"))
    assert decoded["job_id"] == str(job.job_id)
    assert decoded["status"] == "completed"


@pytest.mark.asyncio
async def test_notify_completion_omits_signature_when_secret_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200)

    _install_mock_transport(monkeypatch, handler)

    client = CallbackHttpClient(signing_secret=None)
    ok = await client.notify_completion(CALLBACK_URL, _make_job(), API_KEY)

    assert ok is True
    headers_lower = {k.lower() for k in captured["headers"]}
    assert SIGNATURE_HEADER.lower() not in headers_lower


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_completion_retries_on_500_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"ok": True})

    _install_mock_transport(monkeypatch, handler)

    client = CallbackHttpClient(signing_secret=SIGNING_SECRET, max_retries=3)
    ok = await client.notify_completion(CALLBACK_URL, _make_job(), API_KEY)

    assert ok is True
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_notify_completion_returns_false_after_exhausted_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500, json={"error": "permanent"})

    _install_mock_transport(monkeypatch, handler)

    client = CallbackHttpClient(signing_secret=SIGNING_SECRET, max_retries=3)
    ok = await client.notify_completion(CALLBACK_URL, _make_job(), API_KEY)

    assert ok is False
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_notify_completion_non_retryable_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(400, json={"error": "bad payload"})

    _install_mock_transport(monkeypatch, handler)

    client = CallbackHttpClient(max_retries=3)
    ok = await client.notify_completion(CALLBACK_URL, _make_job(), API_KEY)

    assert ok is False
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_notify_completion_retries_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("dns failure", request=request)

    _install_mock_transport(monkeypatch, handler)

    client = CallbackHttpClient(max_retries=3)
    ok = await client.notify_completion(CALLBACK_URL, _make_job(), API_KEY)

    assert ok is False
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_notify_completion_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _install_mock_transport(monkeypatch, handler)

    client = CallbackHttpClient(max_retries=2)
    # Assert no exception escapes even when every attempt fails.
    result = await client.notify_completion(CALLBACK_URL, _make_job(), API_KEY)
    assert result is False


# ---------------------------------------------------------------------------
# NoOp variant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noop_client_returns_true_and_does_not_call_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        call_count["n"] += 1
        return httpx.Response(200)

    _install_mock_transport(monkeypatch, handler)

    noop = NoOpCallbackClient()
    ok = await noop.notify_completion(CALLBACK_URL, _make_job(), API_KEY)
    assert ok is True
    assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class _FakeSettings:
    def __init__(self, *, enabled: bool, secret: str = "") -> None:
        self.CALLBACK_ENABLED = enabled
        self.CALLBACK_SIGNING_SECRET = secret


def test_build_callback_client_returns_noop_when_disabled() -> None:
    client = build_callback_client(_FakeSettings(enabled=False))
    assert isinstance(client, NoOpCallbackClient)


def test_build_callback_client_returns_real_when_enabled() -> None:
    client = build_callback_client(
        _FakeSettings(enabled=True, secret="shhh"),
    )
    assert isinstance(client, CallbackHttpClient)
    assert client._signing_secret == "shhh"  # noqa: SLF001


def test_build_callback_client_empty_secret_becomes_none() -> None:
    client = build_callback_client(_FakeSettings(enabled=True, secret=""))
    assert isinstance(client, CallbackHttpClient)
    assert client._signing_secret is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# Protocol conformance via inspect
# ---------------------------------------------------------------------------


def test_callback_http_client_is_runtime_checkable_icallbackclient() -> None:
    assert isinstance(CallbackHttpClient(signing_secret=None), ICallbackClient)
    assert isinstance(NoOpCallbackClient(), ICallbackClient)


def _assert_signature_matches(
    protocol_cls: type, concrete_cls: type, method_name: str
) -> None:
    proto_sig = inspect.signature(getattr(protocol_cls, method_name))
    concrete_sig = inspect.signature(getattr(concrete_cls, method_name))

    # Skip "self" in both.
    proto_params = [p for name, p in proto_sig.parameters.items() if name != "self"]
    concrete_params = [
        p for name, p in concrete_sig.parameters.items() if name != "self"
    ]

    assert len(proto_params) == len(concrete_params), (
        f"{concrete_cls.__name__}.{method_name} parameter count "
        f"({len(concrete_params)}) differs from protocol "
        f"({len(proto_params)})"
    )
    for proto_p, conc_p in zip(proto_params, concrete_params):
        assert proto_p.name == conc_p.name, (
            f"{concrete_cls.__name__}.{method_name} parameter "
            f"{conc_p.name!r} differs from protocol {proto_p.name!r}"
        )
        assert proto_p.kind == conc_p.kind, (
            f"{concrete_cls.__name__}.{method_name} parameter "
            f"{conc_p.name!r} kind mismatch: "
            f"protocol={proto_p.kind}, concrete={conc_p.kind}"
        )


def test_notify_completion_signature_matches_protocol() -> None:
    _assert_signature_matches(
        ICallbackClient, CallbackHttpClient, "notify_completion"
    )
    _assert_signature_matches(
        ICallbackClient, NoOpCallbackClient, "notify_completion"
    )


def test_notify_completion_is_coroutine() -> None:
    assert asyncio.iscoroutinefunction(CallbackHttpClient.notify_completion)
    assert asyncio.iscoroutinefunction(NoOpCallbackClient.notify_completion)
