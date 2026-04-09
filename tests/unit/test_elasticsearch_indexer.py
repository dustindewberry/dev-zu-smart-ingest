"""Unit tests for ``ElasticsearchSearchIndexer`` and ``NoOpSearchIndexer``.

Covers:

1. Protocol parity — method names and signatures on the concrete
   ``ElasticsearchSearchIndexer`` match ``ISearchIndexer`` exactly via
   ``inspect.signature``.
2. ``build_search_indexer`` returns ``NoOpSearchIndexer`` when
   ``settings.ELASTICSEARCH_URL`` is ``None`` (and returns the concrete
   class when the URL is set).
3. ``index_companion`` issues a ``PUT`` to the correct URL, using a
   ``monkeypatch``-installed ``httpx.MockTransport`` so no real network
   calls occur.
4. ``NoOpSearchIndexer`` methods never raise, return ``True``, and do
   not touch the network.

These tests do NOT wire the indexer into the orchestrator or
composition root — that's left to the owning integration task.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from zubot_ingestion.domain.entities import CompanionResult, ExtractionResult
from zubot_ingestion.domain.enums import ConfidenceTier, DocumentType
from zubot_ingestion.domain.protocols import ISearchIndexer
from zubot_ingestion.infrastructure.elasticsearch import (
    ElasticsearchSearchIndexer,
    NoOpSearchIndexer,
    build_search_indexer,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_extraction_result() -> ExtractionResult:
    return ExtractionResult(
        drawing_number="KXC-001",
        drawing_number_confidence=0.91,
        title="Ground Floor Plan",
        title_confidence=0.85,
        document_type=DocumentType.FLOOR_PLAN,
        document_type_confidence=0.79,
        confidence_score=0.87,
        confidence_tier=ConfidenceTier.AUTO,
    )


def _make_companion_result() -> CompanionResult:
    return CompanionResult(
        companion_text="A floor plan showing the ground floor.",
        pages_described=2,
        companion_generated=True,
        validation_passed=True,
        quality_score=0.8,
    )


@dataclass
class _FakeSettings:
    """Duck-typed stand-in for the real ``Settings`` class."""

    ELASTICSEARCH_URL: str | None = None


# ---------------------------------------------------------------------------
# (1) Protocol parity
# ---------------------------------------------------------------------------


class TestProtocolParity:
    """Verify the concrete class matches ISearchIndexer exactly."""

    def test_index_companion_signature_matches_protocol(self) -> None:
        proto_sig = inspect.signature(ISearchIndexer.index_companion)
        impl_sig = inspect.signature(ElasticsearchSearchIndexer.index_companion)
        assert proto_sig == impl_sig, (
            f"ElasticsearchSearchIndexer.index_companion signature drift:\n"
            f"  protocol: {proto_sig}\n"
            f"  impl:     {impl_sig}"
        )

    def test_check_connection_signature_matches_protocol(self) -> None:
        proto_sig = inspect.signature(ISearchIndexer.check_connection)
        impl_sig = inspect.signature(
            ElasticsearchSearchIndexer.check_connection
        )
        assert proto_sig == impl_sig, (
            f"ElasticsearchSearchIndexer.check_connection signature drift:\n"
            f"  protocol: {proto_sig}\n"
            f"  impl:     {impl_sig}"
        )

    def test_noop_has_same_methods_and_signatures(self) -> None:
        for name in ("index_companion", "check_connection"):
            proto_sig = inspect.signature(getattr(ISearchIndexer, name))
            noop_sig = inspect.signature(getattr(NoOpSearchIndexer, name))
            assert proto_sig == noop_sig, (
                f"NoOpSearchIndexer.{name} signature drift:\n"
                f"  protocol: {proto_sig}\n"
                f"  noop:     {noop_sig}"
            )

    def test_concrete_class_exposes_exactly_protocol_methods(self) -> None:
        """The concrete class must declare every protocol method by name."""
        required = {"index_companion", "check_connection"}
        for name in required:
            assert hasattr(ElasticsearchSearchIndexer, name), (
                f"ElasticsearchSearchIndexer missing protocol method: {name}"
            )
            assert hasattr(NoOpSearchIndexer, name), (
                f"NoOpSearchIndexer missing protocol method: {name}"
            )

    def test_concrete_class_is_runtime_checkable_instance(self) -> None:
        """@runtime_checkable isinstance check must pass for both classes."""
        concrete = ElasticsearchSearchIndexer(base_url="http://localhost:9200")
        noop = NoOpSearchIndexer()
        assert isinstance(concrete, ISearchIndexer)
        assert isinstance(noop, ISearchIndexer)


# ---------------------------------------------------------------------------
# (2) Factory behavior
# ---------------------------------------------------------------------------


class TestBuildSearchIndexerFactory:
    def test_returns_noop_when_url_is_none(self) -> None:
        settings = _FakeSettings(ELASTICSEARCH_URL=None)
        indexer = build_search_indexer(settings)
        assert isinstance(indexer, NoOpSearchIndexer)

    def test_returns_noop_when_url_is_empty_string(self) -> None:
        settings = _FakeSettings(ELASTICSEARCH_URL="")
        indexer = build_search_indexer(settings)
        assert isinstance(indexer, NoOpSearchIndexer)

    def test_returns_concrete_when_url_is_set(self) -> None:
        settings = _FakeSettings(ELASTICSEARCH_URL="http://es.example:9200")
        indexer = build_search_indexer(settings)
        assert isinstance(indexer, ElasticsearchSearchIndexer)


# ---------------------------------------------------------------------------
# (3) HTTP transport — verify PUT target URL via MockTransport
# ---------------------------------------------------------------------------


class _TransportSpy:
    """Capture the last request issued by an httpx client patch."""

    def __init__(self, response_status: int = 201) -> None:
        self.requests: list[httpx.Request] = []
        self._response_status = response_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            self._response_status,
            json={"_index": "companions", "result": "created"},
        )


def _patch_async_client(
    monkeypatch: pytest.MonkeyPatch,
    spy: _TransportSpy,
) -> None:
    """Replace ``httpx.AsyncClient`` in the indexer module with a MockTransport-backed one."""
    transport = httpx.MockTransport(spy.handler)
    real_async_client = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        # Drop constructor args that AsyncClient(transport=...) does not accept
        # alongside transport (e.g. verify must be removed because MockTransport
        # short-circuits TLS).
        kwargs.pop("verify", None)
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(
        "zubot_ingestion.infrastructure.elasticsearch.indexer.httpx.AsyncClient",
        _factory,
    )


class TestIndexCompanionHttp:
    def test_index_companion_puts_to_correct_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _TransportSpy(response_status=201)
        _patch_async_client(monkeypatch, spy)

        indexer = ElasticsearchSearchIndexer(base_url="http://es.example:9200")
        result = asyncio.run(
            indexer.index_companion(
                document_id="job-abc-123",
                extraction_result=_make_extraction_result(),
                companion_result=_make_companion_result(),
                deployment_id=42,
            )
        )

        assert result is True
        assert len(spy.requests) == 1
        req = spy.requests[0]
        assert req.method == "PUT"
        assert str(req.url) == "http://es.example:9200/companions/_doc/job-abc-123"

    def test_index_companion_strips_trailing_slash_from_base_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _TransportSpy(response_status=200)
        _patch_async_client(monkeypatch, spy)

        indexer = ElasticsearchSearchIndexer(base_url="http://es.example:9200/")
        result = asyncio.run(
            indexer.index_companion(
                document_id="doc-1",
                extraction_result=_make_extraction_result(),
                companion_result=_make_companion_result(),
                deployment_id=None,
            )
        )

        assert result is True
        assert str(spy.requests[0].url) == "http://es.example:9200/companions/_doc/doc-1"

    def test_index_companion_returns_false_on_5xx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _TransportSpy(response_status=503)
        _patch_async_client(monkeypatch, spy)

        indexer = ElasticsearchSearchIndexer(base_url="http://es.example:9200")
        result = asyncio.run(
            indexer.index_companion(
                document_id="doc-1",
                extraction_result=_make_extraction_result(),
                companion_result=_make_companion_result(),
                deployment_id=1,
            )
        )

        assert result is False

    def test_index_companion_never_raises_on_network_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        transport = httpx.MockTransport(_boom)
        real_async_client = httpx.AsyncClient

        def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs.pop("verify", None)
            kwargs["transport"] = transport
            return real_async_client(*args, **kwargs)

        monkeypatch.setattr(
            "zubot_ingestion.infrastructure.elasticsearch.indexer.httpx.AsyncClient",
            _factory,
        )

        indexer = ElasticsearchSearchIndexer(base_url="http://es.example:9200")
        # Must not raise — adapter swallows network errors and returns False
        result = asyncio.run(
            indexer.index_companion(
                document_id="doc-1",
                extraction_result=_make_extraction_result(),
                companion_result=_make_companion_result(),
                deployment_id=1,
            )
        )
        assert result is False

    def test_check_connection_hits_cluster_health_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _TransportSpy(response_status=200)
        _patch_async_client(monkeypatch, spy)

        indexer = ElasticsearchSearchIndexer(base_url="http://es.example:9200")
        result = asyncio.run(indexer.check_connection())

        assert result is True
        assert len(spy.requests) == 1
        req = spy.requests[0]
        assert req.method == "GET"
        assert str(req.url) == "http://es.example:9200/_cluster/health"

    def test_check_connection_returns_false_on_non_2xx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _TransportSpy(response_status=500)
        _patch_async_client(monkeypatch, spy)

        indexer = ElasticsearchSearchIndexer(base_url="http://es.example:9200")
        assert asyncio.run(indexer.check_connection()) is False


# ---------------------------------------------------------------------------
# (4) NoOpSearchIndexer does not raise
# ---------------------------------------------------------------------------


class TestNoOpSearchIndexer:
    def test_index_companion_returns_true_without_raising(self) -> None:
        noop = NoOpSearchIndexer()
        result = asyncio.run(
            noop.index_companion(
                document_id="doc-1",
                extraction_result=_make_extraction_result(),
                companion_result=_make_companion_result(),
                deployment_id=None,
            )
        )
        assert result is True

    def test_check_connection_returns_true_without_raising(self) -> None:
        noop = NoOpSearchIndexer()
        assert asyncio.run(noop.check_connection()) is True
