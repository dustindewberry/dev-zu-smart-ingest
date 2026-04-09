"""Composition-root sanity tests.

These tests are CRITICAL regression coverage against the recurring
composition-root drift pattern documented in PolyForge's institutional
learning: a factory in ``services/__init__.py`` passes kwargs the
adapter's ``__init__`` does not accept, or calls a builder for a
protocol whose concrete implementation has a mismatched signature.

Each test invokes one of the composition-root factories with the default
settings and asserts the call returns without raising. The tests also
verify that the returned objects satisfy their declared protocols via
``@runtime_checkable`` isinstance checks, guarding against signature
drift between the abstract protocol and the concrete implementer.
"""

from __future__ import annotations

import pytest

from zubot_ingestion.config import Settings
from zubot_ingestion.domain.pipeline.validation import (
    CompanionValidator,
    build_companion_validator,
)
from zubot_ingestion.domain.protocols import (
    ICallbackClient,
    ICompanionValidator,
    IOrchestrator,
    ISearchIndexer,
)
from zubot_ingestion.infrastructure.callback import build_callback_client
from zubot_ingestion.infrastructure.elasticsearch import build_search_indexer


def _make_settings(**overrides: object) -> Settings:
    """Return a Settings instance with test-safe defaults.

    Uses empty API key / JWT secret so the factory doesn't hit
    environment variables and stays hermetic.
    """
    defaults: dict[str, object] = {
        "ZUBOT_INGESTION_API_KEY": "test-key",
        "WOD_JWT_SECRET": "test-secret",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_build_companion_validator_returns_runtime_checkable_instance() -> None:
    validator = build_companion_validator()
    assert isinstance(validator, CompanionValidator)
    assert isinstance(validator, ICompanionValidator)


def test_build_search_indexer_with_default_settings_returns_indexer() -> None:
    settings = _make_settings()
    indexer = build_search_indexer(settings)
    assert isinstance(indexer, ISearchIndexer)


def test_build_search_indexer_with_empty_url_returns_noop_fallback() -> None:
    settings = _make_settings(ELASTICSEARCH_URL="")
    indexer = build_search_indexer(settings)
    assert isinstance(indexer, ISearchIndexer)
    # NoOp fallback should still be runtime-checkable against the
    # ISearchIndexer protocol.
    from zubot_ingestion.infrastructure.elasticsearch import NoOpSearchIndexer

    assert isinstance(indexer, NoOpSearchIndexer)


def test_build_callback_client_with_disabled_returns_noop() -> None:
    settings = _make_settings(CALLBACK_ENABLED=False)
    client = build_callback_client(settings)
    assert isinstance(client, ICallbackClient)
    from zubot_ingestion.infrastructure.callback import NoOpCallbackClient

    assert isinstance(client, NoOpCallbackClient)


def test_build_callback_client_with_enabled_returns_real_client() -> None:
    settings = _make_settings(
        CALLBACK_ENABLED=True,
        CALLBACK_SIGNING_SECRET="test-secret",
    )
    client = build_callback_client(settings)
    assert isinstance(client, ICallbackClient)
    from zubot_ingestion.infrastructure.callback import CallbackHttpClient

    assert isinstance(client, CallbackHttpClient)


def test_build_orchestrator_composition_root_fully_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: invoking build_orchestrator() must not raise.

    This is the canonical test against composition-root drift. Every
    new adapter factory added to ``services/__init__.py`` must be
    exercised by this path.

    If the test environment is missing third-party dependencies that
    the real adapters need (e.g. ``json_repair`` for the Stage 1 JSON
    parser, ``pymupdf`` for the PDF processor), the test is skipped
    rather than falsely failing on environment drift.
    """
    # Pre-flight: ensure we can import the full factory chain before
    # attempting the call. ImportError at this point is an environment
    # issue, not a composition-root regression.
    try:
        import json_repair  # noqa: F401 - presence check
        import fitz  # noqa: F401 - pymupdf presence check
    except ImportError as exc:
        pytest.skip(f"missing runtime dependency for full wiring: {exc}")

    # ChromaDBMetadataWriter may attempt to initialize a client at
    # construction time; patch it with a dummy that only needs
    # host/port keyword args so the factory call stays hermetic.
    from zubot_ingestion.infrastructure.chromadb import writer as chroma_writer

    class _DummyChromaWriter:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.args = args
            self.kwargs = kwargs

    monkeypatch.setattr(
        chroma_writer, "ChromaDBMetadataWriter", _DummyChromaWriter
    )

    from zubot_ingestion.services import build_orchestrator

    orchestrator = build_orchestrator()
    assert isinstance(orchestrator, IOrchestrator)
    # Every cross-cutting adapter must be populated after the wiring fix.
    assert orchestrator._companion_validator is not None  # type: ignore[attr-defined]
    assert orchestrator._search_indexer is not None  # type: ignore[attr-defined]
    assert orchestrator._callback_client is not None  # type: ignore[attr-defined]


def test_services_init_factory_imports_dont_raise() -> None:
    """Static check: every new factory import in services/__init__.py resolves.

    This is a lighter regression guard that doesn't require the full
    dependency chain — it simply imports every factory function the
    composition root depends on and asserts they're callable. Catches
    the most common drift pattern (missing module, renamed symbol)
    without needing PyMuPDF or json_repair at test time.
    """
    from zubot_ingestion.domain.pipeline.validation import (
        build_companion_validator as v_factory,
    )
    from zubot_ingestion.infrastructure.callback import (
        build_callback_client as c_factory,
    )
    from zubot_ingestion.infrastructure.elasticsearch import (
        build_search_indexer as s_factory,
    )

    assert callable(v_factory)
    assert callable(c_factory)
    assert callable(s_factory)

    settings = _make_settings()
    assert v_factory() is not None
    assert c_factory(settings) is not None
    assert s_factory(settings) is not None
