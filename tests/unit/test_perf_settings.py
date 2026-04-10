"""Unit tests for the performance-tuning knobs on ``Settings``.

Pins the defaults and env-var-override contract of the performance tuning
knobs added to :class:`zubot_ingestion.config.Settings` and the matching
``PERF_*`` / ``OLLAMA_HTTP_POOL_*`` / ``COMPANION_SKIP_*`` /
``OTEL_SPAN_COMPANION_SKIPPED`` constants in
``zubot_ingestion.shared.constants``.

All defaults are chosen to PRESERVE CURRENT BEHAVIOR so that the
appliance-tuning refactor can land as a pure additive change. Downstream
tasks (Ollama client, Celery app, orchestrator) read these knobs and will
get additional coverage in their own test files.
"""

from __future__ import annotations

import importlib

import pytest

from zubot_ingestion.config import Settings, get_settings
from zubot_ingestion.shared import constants as C


# ---------------------------------------------------------------------------
# Environment-isolation fixture — stripped of every env var that the new
# performance knobs read, PLUS the required-secret vars so ``Settings()``
# constructs cleanly. Also chdir to tmp_path so a stray ``.env`` in the
# developer checkout cannot leak in.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for var in (
        # Required secrets (give them known test values so Settings() loads)
        "DATABASE_URL",
        "ZUBOT_INGESTION_API_KEY",
        "WOD_JWT_SECRET",
        # Performance knobs — must be stripped so defaults apply
        "OLLAMA_KEEP_ALIVE",
        "OLLAMA_HTTP_POOL_MAX_CONNECTIONS",
        "OLLAMA_HTTP_POOL_MAX_KEEPALIVE",
        "OLLAMA_HTTP_TIMEOUT_SECONDS",
        "OLLAMA_RETRY_MAX_ATTEMPTS",
        "OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS",
        "OLLAMA_RETRY_BACKOFF_MULTIPLIER",
        "OLLAMA_VISION_MODEL",
        "OLLAMA_TEXT_MODEL",
        "CELERY_WORKER_CONCURRENCY",
        "CELERY_WORKER_PREFETCH_MULTIPLIER",
        "COMPANION_SKIP_ENABLED",
        "COMPANION_SKIP_MIN_WORDS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("ZUBOT_INGESTION_API_KEY", "test-api-key")
    monkeypatch.setenv("WOD_JWT_SECRET", "test-jwt-secret")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Defaults preserve current behavior
# ---------------------------------------------------------------------------


def test_default_settings_preserves_current_behavior() -> None:
    """The four load-bearing defaults called out by the task spec must hold.

    Any drift in these defaults is treated as a performance regression and
    must be caught immediately — the appliance deployment overrides them
    explicitly in its ``.env`` file.
    """
    s = Settings()  # type: ignore[call-arg]
    assert s.CELERY_WORKER_CONCURRENCY == 2
    assert s.COMPANION_SKIP_ENABLED is False
    assert s.OLLAMA_TEXT_MODEL == "qwen2.5:7b"


def test_default_settings_full_knob_matrix() -> None:
    """Every new performance knob must default to its PERF_* constant."""
    s = Settings()  # type: ignore[call-arg]
    # Ollama runtime
    assert s.OLLAMA_KEEP_ALIVE == C.PERF_OLLAMA_KEEP_ALIVE
    assert s.OLLAMA_KEEP_ALIVE == "5m"
    # Ollama HTTP transport
    assert s.OLLAMA_HTTP_POOL_MAX_CONNECTIONS == 20
    assert s.OLLAMA_HTTP_POOL_MAX_KEEPALIVE == 10
    assert s.OLLAMA_HTTP_TIMEOUT_SECONDS == 120.0
    assert (
        s.OLLAMA_HTTP_POOL_MAX_CONNECTIONS
        == C.PERF_OLLAMA_HTTP_POOL_MAX_CONNECTIONS
    )
    assert (
        s.OLLAMA_HTTP_POOL_MAX_KEEPALIVE
        == C.PERF_OLLAMA_HTTP_POOL_MAX_KEEPALIVE
    )
    assert s.OLLAMA_HTTP_TIMEOUT_SECONDS == C.PERF_OLLAMA_HTTP_TIMEOUT_SECONDS
    # Retry budget
    assert s.OLLAMA_RETRY_MAX_ATTEMPTS == 3
    assert s.OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS == 1.0
    assert s.OLLAMA_RETRY_BACKOFF_MULTIPLIER == 2.0
    assert s.OLLAMA_RETRY_MAX_ATTEMPTS == C.PERF_OLLAMA_RETRY_MAX_ATTEMPTS
    assert (
        s.OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS
        == C.PERF_OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS
    )
    assert (
        s.OLLAMA_RETRY_BACKOFF_MULTIPLIER
        == C.PERF_OLLAMA_RETRY_BACKOFF_MULTIPLIER
    )
    # Models
    assert s.OLLAMA_VISION_MODEL == "qwen2.5vl:7b"
    assert s.OLLAMA_VISION_MODEL == C.PERF_OLLAMA_VISION_MODEL
    assert s.OLLAMA_TEXT_MODEL == C.PERF_OLLAMA_TEXT_MODEL
    # Celery
    assert s.CELERY_WORKER_CONCURRENCY == C.PERF_CELERY_WORKER_CONCURRENCY
    assert (
        s.CELERY_WORKER_PREFETCH_MULTIPLIER
        == C.PERF_CELERY_WORKER_PREFETCH_MULTIPLIER
    )
    assert s.CELERY_WORKER_PREFETCH_MULTIPLIER == 1
    # Companion skip heuristic
    assert s.COMPANION_SKIP_MIN_WORDS == 150
    assert s.COMPANION_SKIP_ENABLED == C.PERF_COMPANION_SKIP_ENABLED
    assert s.COMPANION_SKIP_MIN_WORDS == C.PERF_COMPANION_SKIP_MIN_WORDS


# ---------------------------------------------------------------------------
# Environment-variable overrides take effect
# ---------------------------------------------------------------------------


def test_env_var_overrides_take_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "24h")
    monkeypatch.setenv("OLLAMA_HTTP_POOL_MAX_CONNECTIONS", "64")
    monkeypatch.setenv("OLLAMA_HTTP_POOL_MAX_KEEPALIVE", "32")
    monkeypatch.setenv("OLLAMA_HTTP_TIMEOUT_SECONDS", "45.5")
    monkeypatch.setenv("OLLAMA_RETRY_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS", "0.25")
    monkeypatch.setenv("OLLAMA_RETRY_BACKOFF_MULTIPLIER", "3.0")
    monkeypatch.setenv("OLLAMA_VISION_MODEL", "custom-vision:latest")
    monkeypatch.setenv("OLLAMA_TEXT_MODEL", "qwen2.5:3b")
    monkeypatch.setenv("CELERY_WORKER_CONCURRENCY", "8")
    monkeypatch.setenv("CELERY_WORKER_PREFETCH_MULTIPLIER", "2")
    monkeypatch.setenv("COMPANION_SKIP_ENABLED", "true")
    monkeypatch.setenv("COMPANION_SKIP_MIN_WORDS", "300")

    s = Settings()  # type: ignore[call-arg]

    assert s.OLLAMA_KEEP_ALIVE == "24h"
    assert s.OLLAMA_HTTP_POOL_MAX_CONNECTIONS == 64
    assert s.OLLAMA_HTTP_POOL_MAX_KEEPALIVE == 32
    assert s.OLLAMA_HTTP_TIMEOUT_SECONDS == 45.5
    assert s.OLLAMA_RETRY_MAX_ATTEMPTS == 5
    assert s.OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS == 0.25
    assert s.OLLAMA_RETRY_BACKOFF_MULTIPLIER == 3.0
    assert s.OLLAMA_VISION_MODEL == "custom-vision:latest"
    assert s.OLLAMA_TEXT_MODEL == "qwen2.5:3b"
    assert s.CELERY_WORKER_CONCURRENCY == 8
    assert s.CELERY_WORKER_PREFETCH_MULTIPLIER == 2
    assert s.COMPANION_SKIP_ENABLED is True
    assert s.COMPANION_SKIP_MIN_WORDS == 300


def test_companion_skip_enabled_false_string_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``COMPANION_SKIP_ENABLED=false`` must still parse to False."""
    monkeypatch.setenv("COMPANION_SKIP_ENABLED", "false")
    s = Settings()  # type: ignore[call-arg]
    assert s.COMPANION_SKIP_ENABLED is False


# ---------------------------------------------------------------------------
# Lowercase property aliases mirror the canonical UPPER_CASE fields
# ---------------------------------------------------------------------------


def test_lowercase_property_aliases_mirror_upper_case() -> None:
    s = Settings()  # type: ignore[call-arg]
    assert s.ollama_keep_alive == s.OLLAMA_KEEP_ALIVE
    assert s.ollama_http_pool_max_connections == s.OLLAMA_HTTP_POOL_MAX_CONNECTIONS
    assert s.ollama_http_pool_max_keepalive == s.OLLAMA_HTTP_POOL_MAX_KEEPALIVE
    assert s.ollama_http_timeout_seconds == s.OLLAMA_HTTP_TIMEOUT_SECONDS
    assert s.ollama_retry_max_attempts == s.OLLAMA_RETRY_MAX_ATTEMPTS
    assert (
        s.ollama_retry_initial_backoff_seconds
        == s.OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS
    )
    assert s.ollama_retry_backoff_multiplier == s.OLLAMA_RETRY_BACKOFF_MULTIPLIER
    assert s.ollama_vision_model == s.OLLAMA_VISION_MODEL
    assert s.ollama_text_model == s.OLLAMA_TEXT_MODEL
    assert s.celery_worker_concurrency == s.CELERY_WORKER_CONCURRENCY
    assert (
        s.celery_worker_prefetch_multiplier == s.CELERY_WORKER_PREFETCH_MULTIPLIER
    )
    assert s.companion_skip_enabled == s.COMPANION_SKIP_ENABLED
    assert s.companion_skip_min_words == s.COMPANION_SKIP_MIN_WORDS


# ---------------------------------------------------------------------------
# New constants are importable from shared.constants and appear in __all__
# ---------------------------------------------------------------------------


_EXPECTED_NEW_CONSTANT_NAMES: tuple[str, ...] = (
    # PERF_* defaults
    "PERF_OLLAMA_KEEP_ALIVE",
    "PERF_OLLAMA_VISION_MODEL",
    "PERF_OLLAMA_TEXT_MODEL",
    "PERF_OLLAMA_HTTP_POOL_MAX_CONNECTIONS",
    "PERF_OLLAMA_HTTP_POOL_MAX_KEEPALIVE",
    "PERF_OLLAMA_HTTP_TIMEOUT_SECONDS",
    "PERF_OLLAMA_RETRY_MAX_ATTEMPTS",
    "PERF_OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS",
    "PERF_OLLAMA_RETRY_BACKOFF_MULTIPLIER",
    "PERF_CELERY_WORKER_CONCURRENCY",
    "PERF_CELERY_WORKER_PREFETCH_MULTIPLIER",
    "PERF_COMPANION_SKIP_ENABLED",
    "PERF_COMPANION_SKIP_MIN_WORDS",
    # Canonical names (also referenced by downstream wiring)
    "COMPANION_SKIP_ENABLED",
    "COMPANION_SKIP_MIN_WORDS",
    # OTEL span
    "OTEL_SPAN_COMPANION_SKIPPED",
)


def test_new_constants_are_importable_from_shared_constants() -> None:
    mod = importlib.import_module("zubot_ingestion.shared.constants")
    for name in _EXPECTED_NEW_CONSTANT_NAMES:
        assert hasattr(mod, name), f"{name} missing from shared.constants"


def test_new_constants_are_in_all_export_list() -> None:
    mod = importlib.import_module("zubot_ingestion.shared.constants")
    for name in _EXPECTED_NEW_CONSTANT_NAMES:
        assert name in mod.__all__, f"{name} missing from shared.constants.__all__"


def test_otel_span_companion_skipped_value() -> None:
    """The OTEL span name is part of the observability contract — pin it."""
    assert C.OTEL_SPAN_COMPANION_SKIPPED == "zubot.pipeline.stage2.companion_skipped"


# ---------------------------------------------------------------------------
# Public module surface — the new knobs must be real Settings fields.
# ---------------------------------------------------------------------------


_EXPECTED_NEW_SETTINGS_FIELDS: tuple[str, ...] = (
    "OLLAMA_KEEP_ALIVE",
    "OLLAMA_HTTP_POOL_MAX_CONNECTIONS",
    "OLLAMA_HTTP_POOL_MAX_KEEPALIVE",
    "OLLAMA_HTTP_TIMEOUT_SECONDS",
    "OLLAMA_RETRY_MAX_ATTEMPTS",
    "OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS",
    "OLLAMA_RETRY_BACKOFF_MULTIPLIER",
    "OLLAMA_VISION_MODEL",
    "OLLAMA_TEXT_MODEL",
    "CELERY_WORKER_CONCURRENCY",
    "CELERY_WORKER_PREFETCH_MULTIPLIER",
    "COMPANION_SKIP_ENABLED",
    "COMPANION_SKIP_MIN_WORDS",
)


def test_settings_model_declares_every_new_knob_as_field() -> None:
    for name in _EXPECTED_NEW_SETTINGS_FIELDS:
        assert name in Settings.model_fields, (
            f"{name} is not declared as a pydantic field on Settings"
        )
