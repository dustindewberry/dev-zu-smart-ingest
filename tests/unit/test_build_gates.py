"""Build-gate assertions for the zutec-smart-ingest eight-bug fingerprint.

This module encodes the structural invariants that must hold after the
canonical fix tasks for steps 19 (companion validator), 20 (review queue
API), 21 (Elasticsearch + callback), and the four cross-cutting integration
bugs (composition-root drift, celery retry flapping, update_job_status vs
update_job_result, IJobRepository protocol drift) have all landed.

Each gate maps 1:1 to one of the eight critical bugs that have
historically reappeared across PolyForge builds for this service. The
tests are deliberately file-level / introspection-level so they run in
milliseconds and do not require any external services.

If ANY of these gates fails, the corresponding canonical implementation
has regressed — do NOT disable the gate, fix the regression.

Registered in the Anvil test registry alongside
``test_regression_eight_bug_fingerprint.py``.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Locate the project src/ root once at import time.
# ---------------------------------------------------------------------------


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "zubot_ingestion"
SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _read(relative: str) -> str:
    """Read a file under ``src/zubot_ingestion`` and return its text."""
    path = PACKAGE_ROOT / relative
    assert path.is_file(), f"expected file to exist: {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Gate 1 — review.py is NOT a placeholder stub (Bug 4).
# ---------------------------------------------------------------------------


def test_gate_review_route_is_not_placeholder() -> None:
    """``api/routes/review.py`` must NOT be a placeholder stub.

    Regression of Bug 4 (api/routes/review.py is a 24-line placeholder
    returning ``{"items": []}``). If the canonical step-20 review-queue
    API landed successfully, none of the three placeholder markers should
    appear anywhere in the file.
    """
    source = _read("api/routes/review.py")

    # Case-insensitive to catch "Placeholder" / "PLACEHOLDER" too.
    assert "placeholder" not in source.lower(), (
        "api/routes/review.py contains the substring 'placeholder' — the "
        "canonical step-20 review-queue API has not landed."
    )
    assert "MUST overwrite" not in source, (
        "api/routes/review.py still carries the 'MUST overwrite' stub "
        "directive — the canonical step-20 review-queue API has not landed."
    )
    assert '{"items": []}' not in source, (
        "api/routes/review.py still returns the placeholder "
        "'{\"items\": []}' literal — the canonical step-20 review-queue "
        "API has not landed."
    )


# ---------------------------------------------------------------------------
# Gate 2 — validation.py exists with class CompanionValidator (Bug 5).
# ---------------------------------------------------------------------------


def test_gate_companion_validator_module_exists() -> None:
    """``domain/pipeline/validation.py`` must exist and define CompanionValidator.

    Regression of Bug 5 (domain/pipeline/validation.py missing entirely).
    """
    validation_path = PACKAGE_ROOT / "domain" / "pipeline" / "validation.py"
    assert validation_path.is_file(), (
        f"{validation_path} does not exist — canonical step-19 "
        "companion-validator has not landed."
    )

    source = validation_path.read_text(encoding="utf-8")
    assert "class CompanionValidator" in source, (
        "domain/pipeline/validation.py exists but does not declare "
        "'class CompanionValidator' — canonical step-19 implementation "
        "is incomplete."
    )

    # Import smoke-check: the symbol is reachable without error.
    from zubot_ingestion.domain.pipeline.validation import (  # noqa: WPS433
        CompanionValidator,
    )

    assert inspect.isclass(CompanionValidator), (
        "CompanionValidator is exported from validation.py but is not a class."
    )


# ---------------------------------------------------------------------------
# Gate 3 — infrastructure/elasticsearch exports ElasticsearchSearchIndexer.
# ---------------------------------------------------------------------------


def test_gate_elasticsearch_package_exports_indexer() -> None:
    """``infrastructure/elasticsearch`` must export ElasticsearchSearchIndexer.

    Regression of Bug 6a (infrastructure/elasticsearch/__init__.py is a
    1-line docstring placeholder).
    """
    init_path = PACKAGE_ROOT / "infrastructure" / "elasticsearch" / "__init__.py"
    assert init_path.is_file(), f"{init_path} does not exist"

    # Text check first (covers re-exports via "from .indexer import ...").
    init_source = init_path.read_text(encoding="utf-8")
    assert "ElasticsearchSearchIndexer" in init_source, (
        "infrastructure/elasticsearch/__init__.py does not reference "
        "ElasticsearchSearchIndexer — canonical step-21 elasticsearch "
        "adapter has not landed."
    )

    # Import smoke-check.
    module = __import__(
        "zubot_ingestion.infrastructure.elasticsearch",
        fromlist=["ElasticsearchSearchIndexer"],
    )
    assert hasattr(module, "ElasticsearchSearchIndexer"), (
        "zubot_ingestion.infrastructure.elasticsearch does not expose "
        "ElasticsearchSearchIndexer as an attribute."
    )
    assert inspect.isclass(module.ElasticsearchSearchIndexer), (
        "ElasticsearchSearchIndexer is exported but is not a class."
    )


# ---------------------------------------------------------------------------
# Gate 4 — infrastructure/callback exports CallbackHttpClient.
# ---------------------------------------------------------------------------


def test_gate_callback_package_exports_http_client() -> None:
    """``infrastructure/callback`` must export CallbackHttpClient.

    Regression of Bug 6b (infrastructure/callback/__init__.py is a 1-line
    docstring placeholder).
    """
    init_path = PACKAGE_ROOT / "infrastructure" / "callback" / "__init__.py"
    assert init_path.is_file(), f"{init_path} does not exist"

    init_source = init_path.read_text(encoding="utf-8")
    assert "CallbackHttpClient" in init_source, (
        "infrastructure/callback/__init__.py does not reference "
        "CallbackHttpClient — canonical step-21 callback client has not "
        "landed."
    )

    module = __import__(
        "zubot_ingestion.infrastructure.callback",
        fromlist=["CallbackHttpClient"],
    )
    assert hasattr(module, "CallbackHttpClient"), (
        "zubot_ingestion.infrastructure.callback does not expose "
        "CallbackHttpClient as an attribute."
    )
    assert inspect.isclass(module.CallbackHttpClient), (
        "CallbackHttpClient is exported but is not a class."
    )


# ---------------------------------------------------------------------------
# Gate 5 — get_job_repository is an @asynccontextmanager (Bug 1).
# ---------------------------------------------------------------------------


def test_gate_job_repository_factory_is_async_context_manager() -> None:
    """``services.get_job_repository`` must be an @asynccontextmanager.

    Regression of Bug 1 (services/__init__.py:132 calls
    ``JobRepository(database_url=...)`` instead of yielding
    ``JobRepository(session)`` from ``get_session()``).

    ``@asynccontextmanager`` wraps the decorated async generator function;
    the underlying function is available via ``__wrapped__`` and should
    pass ``inspect.isasyncgenfunction``. The public name itself should be
    usable with ``async with``.
    """
    from zubot_ingestion.services import get_job_repository

    # Either the decorated form (has __wrapped__ pointing at an async
    # generator) or a direct async generator function is acceptable.
    wrapped = getattr(get_job_repository, "__wrapped__", None)
    is_wrapped_async_gen = wrapped is not None and inspect.isasyncgenfunction(
        wrapped,
    )
    is_direct_async_gen = inspect.isasyncgenfunction(get_job_repository)

    assert is_wrapped_async_gen or is_direct_async_gen, (
        "services.get_job_repository is not an @asynccontextmanager "
        "(neither the function nor its __wrapped__ attribute is an async "
        "generator). Bug 1 (composition-root drift) has regressed."
    )

    # Structural check: the returned object must support the async context
    # manager protocol so callers can use `async with get_job_repository()`.
    # We don't actually enter the context (that would require a live DB),
    # we only verify the returned object carries __aenter__/__aexit__.
    #
    # Construct the context manager WITHOUT calling it as an awaitable —
    # @asynccontextmanager-decorated functions are plain callables that
    # return an _AsyncGeneratorContextManager instance.
    try:
        cm = get_job_repository()
    except Exception as exc:  # pragma: no cover - only reached on regression
        pytest.fail(
            f"services.get_job_repository() raised {type(exc).__name__}: "
            f"{exc}. The factory is no longer safely constructible.",
        )
    else:
        assert hasattr(cm, "__aenter__") and hasattr(cm, "__aexit__"), (
            "get_job_repository() did not return an async context manager "
            "(missing __aenter__/__aexit__). Bug 1 has regressed."
        )
        # Close the cm without entering it to avoid lingering generator
        # state. Since we never called __aenter__, __aexit__ is a no-op.
        close = getattr(cm, "aclose", None)
        if close is not None:
            # It's a coroutine; schedule and discard to avoid warnings.
            import asyncio

            try:
                asyncio.get_event_loop().run_until_complete(close())
            except RuntimeError:
                # No running loop — just drop the reference.
                pass


# ---------------------------------------------------------------------------
# Gate 6 — Concrete JobRepository implements every IJobRepository method.
# ---------------------------------------------------------------------------


def test_gate_job_repository_implements_full_protocol() -> None:
    """Concrete ``JobRepository`` must implement every IJobRepository method.

    Regression of Bug 8 (IJobRepository protocol drift). Even if the
    protocol is not ``@runtime_checkable``, a purely structural name-level
    check must pass.
    """
    from zubot_ingestion.domain.protocols import IJobRepository
    from zubot_ingestion.infrastructure.database.repository import JobRepository

    # Pull every method declared on the Protocol that is NOT inherited
    # from ``typing.Protocol`` / ``object`` infrastructure. We only care
    # about coroutine-style methods the protocol defines.
    protocol_methods: set[str] = set()
    for name, value in vars(IJobRepository).items():
        if name.startswith("_"):
            continue
        if callable(value):
            protocol_methods.add(name)

    assert protocol_methods, (
        "IJobRepository has no public methods — protocol definition is "
        "broken."
    )

    missing: list[str] = []
    for method_name in sorted(protocol_methods):
        if not hasattr(JobRepository, method_name):
            missing.append(method_name)

    assert not missing, (
        "Concrete JobRepository is missing these IJobRepository methods: "
        f"{sorted(missing)}. Bug 8 (protocol drift) has regressed."
    )

    # Spot-check that update_job_result — the method that was dead code in
    # the pre-fix state — is BOTH on the protocol and on the concrete.
    assert "update_job_result" in protocol_methods, (
        "IJobRepository.update_job_result is not declared on the protocol. "
        "Bug 8 has regressed."
    )
    assert hasattr(JobRepository, "update_job_result"), (
        "Concrete JobRepository does not define update_job_result. "
        "Bug 8 has regressed."
    )


# ---------------------------------------------------------------------------
# Gate 7 — No file under src/ calls `update_job_status(result=` (Bug 3).
# ---------------------------------------------------------------------------


def test_gate_no_update_job_status_with_result_kwarg() -> None:
    """No src/ file may call ``update_job_status(result=``.

    Regression of Bug 3 (celery extract_document_task calls
    ``update_job_status(result=...)`` which only writes the JSONB blob and
    leaves the indexed columns NULL). The canonical fix replaces this with
    ``update_job_result(...)``.

    ``update_job_status`` remains valid for status-only transitions; only
    the dead-code pattern with the ``result=`` keyword is forbidden.
    """
    offenders: list[str] = []
    for py_file in SRC_ROOT.rglob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "update_job_status(result=" in source:
            offenders.append(str(py_file.relative_to(SRC_ROOT)))

    assert not offenders, (
        "The dead-code pattern 'update_job_status(result=' appears in "
        f"{len(offenders)} file(s): {offenders}. Bug 3 has regressed — "
        "callers must switch to update_job_result(...) so the indexed "
        "columns (confidence_score, confidence_tier, processing_time_ms, "
        "otel_trace_id, pipeline_trace) are populated."
    )
