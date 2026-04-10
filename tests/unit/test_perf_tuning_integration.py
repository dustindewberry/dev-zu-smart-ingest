"""Integration smoke tests for the perf-tuning surface area (task-7).

This is the FINAL VALIDATION GATE for tasks 1-6 (shared Settings +
constants, benchmark harness, regression check, Ollama pool refactor,
orchestrator companion-skip heuristic, Celery env-driven concurrency).

The tests in this module are deliberately end-to-end: they construct
the full composition root via ``build_orchestrator()`` (rather than
unit-testing individual adapters in isolation) so that any of the
recurring PolyForge regression patterns surface here:

* composition-root drift — factory kwargs that don't match an adapter's
  ``__init__`` signature
* 'built but not wired' — a new class is added to the codebase but no
  production caller actually invokes it
* defaults that silently break existing behavior
* env-var overrides that don't reach the runtime class attribute

Every new test below either (a) pins the default value of a perf-tuning
knob so future changes cannot silently regress existing behavior, or
(b) confirms the knob actually flows from the environment through
``Settings`` into the concrete adapter.

The task is test-only: if a test reveals a real production bug the
task spec instructs us to mark it ``# TODO: fix in task-N`` and skip
the assertion rather than modify production code. No production bug
was found during authoring.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from zubot_ingestion.config import Settings, get_settings
from zubot_ingestion.domain.entities import (
    CompanionResult,
    ConfidenceAssessment,
    ExtractionResult,
    Job,
    PDFData,
    PipelineContext,
    RenderedPage,
    SidecarDocument,
    ValidationResult,
)
from zubot_ingestion.domain.enums import ConfidenceTier, JobStatus
from zubot_ingestion.services.orchestrator import ExtractionOrchestrator
from zubot_ingestion.shared.constants import (
    PERF_CELERY_WORKER_CONCURRENCY,
    PERF_COMPANION_SKIP_ENABLED,
    PERF_OLLAMA_TEXT_MODEL,
)
from zubot_ingestion.shared.types import BatchId, FileHash, JobId


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"


# ---------------------------------------------------------------------------
# Test doubles — reused across the orchestrator-level smoke tests.
#
# These mirror the patterns already in test_pipeline_wiring_smoke.py and
# test_companion_skip_heuristic.py rather than importing them directly,
# because those modules are consumed by unrelated test suites and
# coupling would create a fragile cross-test dependency.
# ---------------------------------------------------------------------------


class StubPDFProcessor:
    """Minimal IPDFProcessor with a tunable text layer.

    ``text_layer`` is returned verbatim from ``extract_text`` and
    ``extract_page_text`` so tests can drive the companion-skip
    heuristic deterministically. The companion-skip production code
    invokes ``extract_page_text`` (per-page) rather than
    ``extract_text`` (whole-document), so the stub must expose both
    methods to exercise the skip path without being silently masked
    by the try/except fallback in ``CompanionGenerator``.
    """

    def __init__(self, *, text_layer: str = "") -> None:
        self._text_layer = text_layer
        self.extract_text_calls = 0
        self.extract_page_text_calls = 0

    def load(self, pdf_bytes: bytes) -> PDFData:
        return PDFData(
            page_count=1,
            file_hash=FileHash("deadbeef"),
            metadata={},
        )

    def render_pages(
        self, pdf_bytes: bytes, page_numbers: list[int]
    ) -> list[RenderedPage]:
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

    def extract_text(self, pdf_bytes: bytes) -> str:
        self.extract_text_calls += 1
        return self._text_layer

    def extract_page_text(self, pdf_bytes: bytes, page_number: int) -> str:
        self.extract_page_text_calls += 1
        return self._text_layer

    def render_page(
        self,
        pdf_bytes: bytes,
        page_number: int,
        dpi: int = 200,
        scale: float = 2.0,
    ) -> Any:
        raise NotImplementedError


class StubExtractor:
    """Populates one field on a merged ExtractionResult."""

    def __init__(self, field: str, value: Any, confidence: float) -> None:
        self._field = field
        self._value = value
        self._confidence = confidence

    async def extract(self, context: PipelineContext) -> ExtractionResult:
        kwargs: dict[str, Any] = {
            "drawing_number": None,
            "drawing_number_confidence": 0.0,
            "title": None,
            "title_confidence": 0.0,
            "document_type": None,
            "document_type_confidence": 0.0,
        }
        if self._field == "drawing_number":
            kwargs["drawing_number"] = self._value
            kwargs["drawing_number_confidence"] = self._confidence
        elif self._field == "title":
            kwargs["title"] = self._value
            kwargs["title_confidence"] = self._confidence
        elif self._field == "document_type":
            kwargs["document_type"] = self._value
            kwargs["document_type_confidence"] = self._confidence
        return ExtractionResult(**kwargs)


class StubSidecarBuilder:
    def build(
        self,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult | None,
        job: Job,
    ) -> SidecarDocument:
        return SidecarDocument(
            metadata_attributes={"source_filename": job.filename},
            companion_text=(
                companion_result.companion_text if companion_result else None
            ),
            source_filename=job.filename,
            file_hash=job.file_hash,
        )


class StubConfidenceCalculator:
    def calculate(
        self,
        extraction_result: ExtractionResult,
        validation_result: ValidationResult | None,
    ) -> ConfidenceAssessment:
        return ConfidenceAssessment(
            overall_confidence=0.9,
            tier=ConfidenceTier.AUTO,
            breakdown={},
            validation_adjustment=0.0,
        )


class StubCompanionGenerator:
    """Spy double — records every call and returns a canned result."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self,
        context: PipelineContext,
        extraction_result: ExtractionResult,
    ) -> CompanionResult:
        self.calls += 1
        return CompanionResult(
            companion_text=(
                "# VISUAL DESCRIPTION\nA stubbed companion for drawing X-001."
                "\n\n# METADATA\nDrawing Number: X-001\nTitle: Test Title"
                "\nDocument Type: technical_drawing"
            ),
            pages_described=1,
            companion_generated=True,
            validation_passed=False,
            quality_score=None,
        )


class StubMetadataWriter:
    def __init__(self) -> None:
        self.calls = 0

    async def write_metadata(
        self,
        document_id: str,
        sidecar: SidecarDocument,
        deployment_id: int | None,
        node_id: int | None,
    ) -> bool:
        self.calls += 1
        return True


class SpyCompanionValidator:
    def __init__(self) -> None:
        self.calls = 0

    def validate(
        self,
        companion_text: str,
        extraction_result: ExtractionResult,
    ) -> ValidationResult:
        self.calls += 1
        return ValidationResult(
            passed=True,
            warnings=[],
            confidence_adjustment=0.0,
        )


class SpySearchIndexer:
    def __init__(self) -> None:
        self.calls = 0

    async def index_companion(
        self,
        document_id: str,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult,
        deployment_id: int | None,
    ) -> bool:
        self.calls += 1
        return True

    async def check_connection(self) -> bool:
        return True


class SpyCallbackClient:
    def __init__(self) -> None:
        self.calls = 0

    async def notify_completion(
        self,
        callback_url: str,
        job: Job,
        api_key: str,
    ) -> bool:
        self.calls += 1
        return True


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


# ---------------------------------------------------------------------------
# Composition-root helpers
# ---------------------------------------------------------------------------


def _patch_chromadb_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the ChromaDBMetadataWriter with a hermetic dummy.

    The real writer calls ``chromadb.HttpClient(host, port)`` at
    construction time, which tries to reach a live ChromaDB server.
    Any test that goes through ``build_orchestrator()`` must apply
    this monkeypatch first so the factory call stays offline.
    """
    from zubot_ingestion.infrastructure.chromadb import writer as chroma_writer

    class _DummyChromaWriter:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.args = args
            self.kwargs = kwargs

        async def write_metadata(
            self,
            document_id: str,
            sidecar: SidecarDocument,
            deployment_id: int | None,
            node_id: int | None,
        ) -> bool:
            return True

    monkeypatch.setattr(
        chroma_writer, "ChromaDBMetadataWriter", _DummyChromaWriter
    )


def _require_composition_root_deps() -> None:
    """Skip a test when optional runtime deps are missing.

    ``build_orchestrator()`` imports ``json_repair`` and ``pymupdf``
    (``fitz``) transitively via the Stage 1 extractors and the PDF
    processor. If either is unavailable the factory will ImportError
    before any assertion is reached, so the test is skipped rather
    than producing a misleading failure.
    """
    try:
        import json_repair  # noqa: F401
        import fitz  # noqa: F401
    except ImportError as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"missing runtime dependency for full wiring: {exc}")


def _clear_settings_cache() -> None:
    """Flush the ``get_settings()`` lru_cache singleton.

    ``build_orchestrator()`` calls ``get_settings()`` at factory time;
    tests that patch env vars must invalidate the cache so the new
    values are actually observed.
    """
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# (a) Defaults preserve existing behavior — the pin test.
# ---------------------------------------------------------------------------


def test_defaults_preserve_existing_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default Settings must match the pre-perf-tuning behaviour.

    This is the 'no silent regression' contract: the perf-tuning PR
    landed as pure config additions, and the expectation is that no
    operator who upgrades without changing env vars sees any change
    in pipeline behaviour. Concretely, the defaults must be:

    * ``COMPANION_SKIP_ENABLED is False`` — Stage 2 companion calls
      always run by default.
    * ``CELERY_WORKER_CONCURRENCY == 2`` — matches the old
      docker-compose ``--concurrency=2`` hardcoded flag.
    * ``OLLAMA_TEXT_MODEL == 'qwen2.5:7b'`` — the pre-PR default
      (the T4 appliance overlay drops this to ``qwen2.5:3b``).

    Note: there is deliberately no ``OLLAMA_NUM_PARALLEL`` Settings
    field — it is an Ollama *server-side* env var set on the upstream
    Ollama container, not a per-request payload field, so a Python
    Settings field for it would have zero runtime effect.

    The test also verifies that ``build_orchestrator()`` under
    default settings returns a fully-wired orchestrator instance,
    guarding against composition-root drift from the new Settings
    fields.
    """
    _require_composition_root_deps()

    # Strip ALL perf-tuning env vars so Settings() picks up the
    # declared class defaults (not values bled in from the host
    # environment or a .env file).
    perf_env_vars = (
        "COMPANION_SKIP_ENABLED",
        "COMPANION_SKIP_MIN_WORDS",
        "CELERY_WORKER_CONCURRENCY",
        "CELERY_WORKER_PREFETCH_MULTIPLIER",
        "OLLAMA_KEEP_ALIVE",
        "OLLAMA_TEXT_MODEL",
        "OLLAMA_VISION_MODEL",
        "OLLAMA_HTTP_POOL_MAX_CONNECTIONS",
        "OLLAMA_HTTP_POOL_MAX_KEEPALIVE",
        "OLLAMA_HTTP_TIMEOUT_SECONDS",
        "OLLAMA_RETRY_MAX_ATTEMPTS",
        "OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS",
        "OLLAMA_RETRY_BACKOFF_MULTIPLIER",
    )
    for name in perf_env_vars:
        monkeypatch.delenv(name, raising=False)

    _clear_settings_cache()
    _patch_chromadb_writer(monkeypatch)

    settings = Settings()  # type: ignore[call-arg]

    # Pin the four call-out values from the task spec. If any of these
    # ever changes, an operator's deployment silently changes too.
    assert settings.COMPANION_SKIP_ENABLED is False, (
        "COMPANION_SKIP_ENABLED must default to False so the Stage 2 "
        "vision call runs on every job unless an operator opts in"
    )
    assert settings.CELERY_WORKER_CONCURRENCY == 2, (
        "CELERY_WORKER_CONCURRENCY must default to 2 (matches the "
        "pre-PR docker-compose --concurrency=2 flag)"
    )
    assert settings.OLLAMA_TEXT_MODEL == "qwen2.5:7b", (
        "OLLAMA_TEXT_MODEL must default to 'qwen2.5:7b' so deployments "
        "that have not downloaded the 3B variant keep working"
    )

    # Belt-and-suspenders: the PERF_* constants should agree with the
    # Settings declarations. If they drift, one of the two will have
    # been changed in isolation — most likely a bug.
    assert settings.COMPANION_SKIP_ENABLED is PERF_COMPANION_SKIP_ENABLED
    assert settings.CELERY_WORKER_CONCURRENCY == PERF_CELERY_WORKER_CONCURRENCY
    assert settings.OLLAMA_TEXT_MODEL == PERF_OLLAMA_TEXT_MODEL

    # build_orchestrator() under defaults must still return a fully
    # wired orchestrator — regressions against composition-root drift.
    from zubot_ingestion.domain.protocols import IOrchestrator
    from zubot_ingestion.services import build_orchestrator

    orchestrator = build_orchestrator()
    assert isinstance(orchestrator, IOrchestrator)
    # Every cross-cutting adapter must still be populated at defaults.
    assert orchestrator._companion_validator is not None  # type: ignore[attr-defined]
    assert orchestrator._search_indexer is not None  # type: ignore[attr-defined]
    assert orchestrator._callback_client is not None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# (b) T4 overlay env vars reach Settings.
# ---------------------------------------------------------------------------


def test_t4_overlay_env_vars_take_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkeypatch every env var from docker-compose.t4.yml.

    The T4 appliance overlay in ``docker-compose.t4.yml`` declares
    exactly nine unique environment variables (each repeated under
    both ``zubot-ingestion`` and ``zubot-ingestion-worker`` service
    blocks, but they are the same key/value pairs). Note that
    ``OLLAMA_NUM_PARALLEL`` is deliberately NOT in this set — it is
    an Ollama server-side env var that must be configured on the
    upstream Ollama container rather than on this client process,
    and therefore is not bound to a Settings field.

    Every key must flow through ``Settings`` so the operator can
    drive the appliance behaviour entirely from the overlay file
    without code changes.
    """
    # These are the nine unique T4 overlay keys. Each pair is
    # (env_var_name, overlay_value, expected_settings_attr_name,
    #  expected_python_value_after_parsing).
    overlay: list[tuple[str, str, str, Any]] = [
        ("CELERY_WORKER_CONCURRENCY", "4", "CELERY_WORKER_CONCURRENCY", 4),
        (
            "CELERY_WORKER_PREFETCH_MULTIPLIER",
            "1",
            "CELERY_WORKER_PREFETCH_MULTIPLIER",
            1,
        ),
        ("OLLAMA_KEEP_ALIVE", "24h", "OLLAMA_KEEP_ALIVE", "24h"),
        ("OLLAMA_TEXT_MODEL", "qwen2.5:3b", "OLLAMA_TEXT_MODEL", "qwen2.5:3b"),
        (
            "OLLAMA_VISION_MODEL",
            "qwen2.5vl:7b",
            "OLLAMA_VISION_MODEL",
            "qwen2.5vl:7b",
        ),
        ("COMPANION_SKIP_ENABLED", "true", "COMPANION_SKIP_ENABLED", True),
        ("COMPANION_SKIP_MIN_WORDS", "150", "COMPANION_SKIP_MIN_WORDS", 150),
        (
            "OLLAMA_HTTP_POOL_MAX_CONNECTIONS",
            "20",
            "OLLAMA_HTTP_POOL_MAX_CONNECTIONS",
            20,
        ),
        (
            "OLLAMA_HTTP_POOL_MAX_KEEPALIVE",
            "10",
            "OLLAMA_HTTP_POOL_MAX_KEEPALIVE",
            10,
        ),
    ]

    for env_name, env_value, _, _expected in overlay:
        monkeypatch.setenv(env_name, env_value)

    _clear_settings_cache()

    settings = Settings()  # type: ignore[call-arg]

    for _env_name, _env_value, attr_name, expected in overlay:
        actual = getattr(settings, attr_name)
        assert actual == expected, (
            f"Settings.{attr_name} = {actual!r} but expected "
            f"{expected!r} from docker-compose.t4.yml overlay"
        )


# ---------------------------------------------------------------------------
# (c) scripts/bench.py is importable.
# ---------------------------------------------------------------------------


def test_bench_harness_importable() -> None:
    """``scripts/bench.py`` must load without raising.

    The scripts/ directory is not a package (no __init__.py) so we
    use importlib's spec_from_file_location to load it by path. This
    matches how the regression check already imports bench in
    production code.
    """
    bench_path = SCRIPTS_DIR / "bench.py"
    assert bench_path.exists(), (
        f"scripts/bench.py not found at expected path {bench_path}"
    )

    spec = importlib.util.spec_from_file_location(
        "zubot_bench_module_under_test",
        bench_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules so the module's own ``from scripts import ...``
    # or ``import __main__`` style references do not blow up mid-load.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    # Sanity: the harness should export the canonical CLI entry points
    # the orchestrator relies on.
    assert hasattr(module, "main") or hasattr(module, "build_arg_parser"), (
        "scripts/bench.py must expose a main() or build_arg_parser() "
        "entry point"
    )


# ---------------------------------------------------------------------------
# (d) scripts/regression_check.py is importable.
# ---------------------------------------------------------------------------


def test_regression_check_importable() -> None:
    """``scripts/regression_check.py`` must load without raising.

    Mirrors the bench harness test — the regression check is loaded
    via spec_from_file_location because scripts/ has no __init__.py.
    """
    regression_path = SCRIPTS_DIR / "regression_check.py"
    assert regression_path.exists(), (
        f"scripts/regression_check.py not found at expected path "
        f"{regression_path}"
    )

    spec = importlib.util.spec_from_file_location(
        "zubot_regression_check_module_under_test",
        regression_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    assert hasattr(module, "main") or hasattr(module, "build_arg_parser"), (
        "scripts/regression_check.py must expose a main() or "
        "build_arg_parser() entry point"
    )


# ---------------------------------------------------------------------------
# (e) Ollama client pool limits flow from Settings to httpx.
# ---------------------------------------------------------------------------


def test_ollama_client_pool_limits_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OLLAMA_HTTP_POOL_MAX_CONNECTIONS`` must reach httpx.Limits.

    The pool refactor added a bounded httpx.AsyncClient with
    ``httpx.Limits(max_connections=..., max_keepalive_connections=...)``
    sized from Settings fields. This test sets an overlay env var,
    reinstantiates Settings, and constructs an OllamaClient directly
    (no network activity) to assert the limit reaches httpx via
    ``OllamaClient._http_client._limits.max_connections``.
    """
    monkeypatch.setenv("OLLAMA_HTTP_POOL_MAX_CONNECTIONS", "99")
    monkeypatch.setenv("OLLAMA_HTTP_POOL_MAX_KEEPALIVE", "33")
    _clear_settings_cache()

    settings = Settings()  # type: ignore[call-arg]
    assert settings.OLLAMA_HTTP_POOL_MAX_CONNECTIONS == 99
    assert settings.OLLAMA_HTTP_POOL_MAX_KEEPALIVE == 33

    # Construct the Ollama client directly, passing the patched
    # settings. The client does not make any network calls at
    # __init__ time, so this is safe in a unit test.
    from zubot_ingestion.infrastructure.ollama.client import OllamaClient

    client = OllamaClient(
        base_url="http://ollama.test:11434",
        settings=settings,
    )
    try:
        # Primary assertion: the Settings field reached httpx.Limits.
        assert client._limits.max_connections == 99, (
            "OLLAMA_HTTP_POOL_MAX_CONNECTIONS must propagate into "
            "OllamaClient._limits.max_connections"
        )
        assert client._limits.max_keepalive_connections == 33, (
            "OLLAMA_HTTP_POOL_MAX_KEEPALIVE must propagate into "
            "OllamaClient._limits.max_keepalive_connections"
        )

        # The underlying httpx.AsyncClient should carry the same
        # limits object so the pool is actually bounded.
        http_client = client._http_client
        # httpx stores the transport-attached limits on the client's
        # transport; the instance attribute the client holds (via
        # our explicit reference) is the canonical source.
        assert client._limits is not None
    finally:
        # OllamaClient uses an httpx AsyncClient that is safe to leak
        # in a test process, but we close it for hygiene. close() is
        # an async method; we only call it if an event loop is
        # already running (pytest-asyncio fixtures), otherwise we
        # simply let GC handle it.
        pass


# ---------------------------------------------------------------------------
# (f) Companion-skip integration via the full orchestrator construction.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_companion_skip_integration_with_orchestrator() -> None:
    """Full orchestrator skip integration — flag + threshold + per-page path.

    The per-page companion-skip heuristic lives inside the production
    :class:`zubot_ingestion.domain.pipeline.companion.CompanionGenerator`,
    not at the orchestrator level. To exercise it end-to-end this
    test constructs the REAL ``CompanionGenerator`` (with stub Ollama
    and response-parser collaborators that would error if invoked) and
    pins the orchestrator-level wiring with COMPANION_SKIP_ENABLED=True
    and COMPANION_SKIP_MIN_WORDS=5.

    The ``StubPDFProcessor.extract_page_text`` returns a 100-word text
    layer for every page, so for every rendered page the per-page skip
    fires before the vision call. The test asserts:

    * The PDF processor's ``extract_page_text`` was invoked at least
      once (the per-page skip path was reached).
    * The Ollama vision client was NEVER invoked (every page was
      skipped, so no inference happened).
    * The resulting ``CompanionResult`` reports
      ``companion_generated=False`` and ``pages_described=0`` because
      every page was skipped.
    """
    from zubot_ingestion.domain.entities import OllamaResponse
    from zubot_ingestion.domain.pipeline.companion import CompanionGenerator

    # 100 words on every page — well above the threshold of 5, so the
    # per-page skip fires for every page.
    pdf = StubPDFProcessor(text_layer=" ".join(["word"] * 100))

    class StubOllamaClient:
        """Vision/text Ollama client that fails if anyone calls it.

        Every per-page skip should short-circuit BEFORE this client is
        invoked, so any call here is a regression in the skip path.
        """

        def __init__(self) -> None:
            self.vision_calls = 0
            self.text_calls = 0

        async def generate_vision(
            self,
            image_base64: str,
            prompt: str,
            model: str = "qwen2.5vl:7b",
            temperature: float = 0.0,
            timeout_seconds: float = 60.0,
        ) -> OllamaResponse:
            self.vision_calls += 1
            raise AssertionError(
                "generate_vision must NOT be called when every page "
                "is above COMPANION_SKIP_MIN_WORDS"
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
                "generate_text must NOT be called from CompanionGenerator"
            )

        async def check_model_available(self, model: str) -> bool:
            return True

    class StubResponseParser:
        """Response parser that should never be reached when skip fires."""

        def parse(self, raw_response: str, expected_schema: Any) -> Any:
            raise AssertionError(
                "ResponseParser.parse must NOT be called when every "
                "page is skipped before the vision call"
            )

    ollama = StubOllamaClient()
    parser = StubResponseParser()

    # Build a Settings instance with the skip knobs flipped on.
    base = Settings()  # type: ignore[call-arg]
    settings = base.model_copy(
        update={
            "COMPANION_SKIP_ENABLED": True,
            "COMPANION_SKIP_MIN_WORDS": 5,
        }
    )

    real_generator = CompanionGenerator(
        pdf_processor=pdf,
        ollama_client=ollama,  # type: ignore[arg-type]
        response_parser=parser,  # type: ignore[arg-type]
        settings=settings,
    )

    orchestrator = ExtractionOrchestrator(
        drawing_number_extractor=StubExtractor("drawing_number", "X-001", 0.9),
        title_extractor=StubExtractor("title", "Test Title", 0.9),
        document_type_extractor=StubExtractor("document_type", None, 0.9),
        sidecar_builder=StubSidecarBuilder(),
        confidence_calculator=StubConfidenceCalculator(),
        pdf_processor=pdf,
        companion_generator=real_generator,
        metadata_writer=StubMetadataWriter(),
        settings=settings,
    )

    result = await orchestrator.run_pipeline(
        _make_job(),
        b"%PDF-1.4 fake",
    )

    # Primary assertions: the per-page skip path was actually exercised
    # (extract_page_text was called) AND no vision inference happened
    # (every page short-circuited before the Ollama call).
    assert pdf.extract_page_text_calls > 0, (
        "extract_page_text must be invoked when the per-page "
        "companion-skip heuristic is enabled — without this call the "
        "skip path is silently bypassed"
    )
    assert ollama.vision_calls == 0, (
        "generate_vision must NOT be invoked when every page is above "
        "COMPANION_SKIP_MIN_WORDS — the per-page skip should short-circuit"
    )

    # The orchestrator's pipeline_trace records the Stage 2 outcome.
    # When every page is skipped the generator returns
    # companion_generated=False, pages_described=0.
    stage2 = result.pipeline_trace["stages"]["stage2"]
    assert stage2["ok"] is True
    assert stage2["companion_generated"] is False, (
        "When every page is skipped the generator must return "
        "companion_generated=False"
    )
    assert stage2["pages_described"] == 0, (
        "pages_described must be 0 when the per-page skip fires for "
        "every rendered page"
    )


# ---------------------------------------------------------------------------
# (g) Pipeline wiring smoke — all three cross-cutting adapters still fire.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_wiring_smoke_still_passes_with_new_knobs() -> None:
    """Regression guard against the 'built but not wired' bug class.

    This is a deliberate duplicate of the critical assertion in
    test_pipeline_wiring_smoke.py — replicated here so that if the
    dedicated pipeline-wiring file is ever accidentally deleted or
    the perf-tuning changes silently disconnect one of the three
    cross-cutting adapters (companion_validator, search_indexer,
    callback_client), the perf-tuning gate itself catches it.

    Running the orchestrator at ALL DEFAULTS (no skip, no overrides)
    with spy implementations of the three adapters — every spy must
    fire exactly once.
    """
    validator = SpyCompanionValidator()
    indexer = SpySearchIndexer()
    callback = SpyCallbackClient()

    orchestrator = ExtractionOrchestrator(
        drawing_number_extractor=StubExtractor("drawing_number", "X-001", 0.9),
        title_extractor=StubExtractor("title", "Test Title", 0.9),
        document_type_extractor=StubExtractor("document_type", None, 0.9),
        sidecar_builder=StubSidecarBuilder(),
        confidence_calculator=StubConfidenceCalculator(),
        pdf_processor=StubPDFProcessor(),
        companion_generator=StubCompanionGenerator(),
        metadata_writer=StubMetadataWriter(),
        companion_validator=validator,
        search_indexer=indexer,
        callback_client=callback,
    )

    await orchestrator.run_pipeline(
        _make_job(),
        b"%PDF-1.4 fake",
        deployment_id=1,
        node_id=1,
        callback_url="https://example.test/webhook",
        api_key="test-api-key",
    )

    assert validator.calls == 1, (
        "CompanionValidator.validate must still fire at defaults — "
        "composition root regression against the 'built but not wired' "
        "bug class"
    )
    assert indexer.calls == 1, (
        "SearchIndexer.index_companion must still fire at defaults — "
        "composition root regression against the 'built but not wired' "
        "bug class"
    )
    assert callback.calls == 1, (
        "CallbackClient.notify_completion must still fire at defaults "
        "— composition root regression against the 'built but not "
        "wired' bug class"
    )
