"""Unit tests for ``zubot_ingestion.config``.

Verifies:
* Defaults are applied for every non-secret field.
* Required secrets cause a validation error when missing.
* Environment variables override defaults.
* ``case_sensitive=True`` is enforced (lowercase vars are ignored).
* ``get_settings`` returns the same instance on repeated calls.
"""

from __future__ import annotations

import importlib

import pytest
from pydantic import ValidationError

from zubot_ingestion.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Strip every variable that ``Settings`` reads, then chdir to a tmp
    directory so a stray ``.env`` in the developer's checkout doesn't leak
    into the test."""
    for var in (
        "ZUBOT_HOST",
        "ZUBOT_PORT",
        "DATABASE_URL",
        "REDIS_URL",
        "OLLAMA_HOST",
        "CHROMADB_HOST",
        "CHROMADB_PORT",
        "ELASTICSEARCH_URL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "ZUBOT_INGESTION_API_KEY",
        "WOD_JWT_SECRET",
        "LOG_LEVEL",
        "LOG_DIR",
        "TEST_PDF_DIR",
        "RATE_LIMIT_DEFAULT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("ZUBOT_INGESTION_API_KEY", "test-api-key")
    monkeypatch.setenv("WOD_JWT_SECRET", "test-jwt-secret")


def test_defaults_are_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    _required(monkeypatch)
    s = Settings()  # type: ignore[call-arg]
    assert s.ZUBOT_HOST == "0.0.0.0"
    assert s.ZUBOT_PORT == 4243
    assert s.REDIS_URL == "redis://redis:6379"
    assert s.OLLAMA_HOST == "http://ollama:11434"
    assert s.CHROMADB_HOST == "chromadb"
    assert s.CHROMADB_PORT == 8000
    assert s.ELASTICSEARCH_URL == "http://elasticsearch:9200"
    assert s.OTEL_EXPORTER_OTLP_ENDPOINT == "http://phoenix:4317"
    assert s.LOG_LEVEL == "INFO"
    assert s.LOG_DIR == "/var/log/zubot-ingestion"
    assert s.TEST_PDF_DIR is None
    assert s.RATE_LIMIT_DEFAULT == "100/minute"


def test_required_database_url_missing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZUBOT_INGESTION_API_KEY", "k")
    monkeypatch.setenv("WOD_JWT_SECRET", "s")
    with pytest.raises(ValidationError) as ei:
        Settings()  # type: ignore[call-arg]
    assert "DATABASE_URL" in str(ei.value)


def test_required_api_key_missing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("WOD_JWT_SECRET", "s")
    with pytest.raises(ValidationError) as ei:
        Settings()  # type: ignore[call-arg]
    assert "ZUBOT_INGESTION_API_KEY" in str(ei.value)


def test_required_jwt_secret_missing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("ZUBOT_INGESTION_API_KEY", "k")
    with pytest.raises(ValidationError) as ei:
        Settings()  # type: ignore[call-arg]
    assert "WOD_JWT_SECRET" in str(ei.value)


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _required(monkeypatch)
    monkeypatch.setenv("ZUBOT_HOST", "127.0.0.1")
    monkeypatch.setenv("ZUBOT_PORT", "9999")
    monkeypatch.setenv("CHROMADB_PORT", "8123")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("TEST_PDF_DIR", "/tmp/pdfs")
    monkeypatch.setenv("RATE_LIMIT_DEFAULT", "5/second")
    s = Settings()  # type: ignore[call-arg]
    assert s.ZUBOT_HOST == "127.0.0.1"
    assert s.ZUBOT_PORT == 9999
    assert s.CHROMADB_PORT == 8123
    assert s.LOG_LEVEL == "DEBUG"
    assert s.TEST_PDF_DIR == "/tmp/pdfs"
    assert s.RATE_LIMIT_DEFAULT == "5/second"


def test_case_sensitive_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lowercase variable names must NOT be picked up."""
    _required(monkeypatch)
    monkeypatch.setenv("zubot_port", "1234")  # lowercase — must be ignored
    s = Settings()  # type: ignore[call-arg]
    assert s.ZUBOT_PORT == 4243  # default, not the lowercase override


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    _required(monkeypatch)
    a = get_settings()
    b = get_settings()
    assert a is b


def test_get_settings_cache_clear_picks_up_new_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _required(monkeypatch)
    a = get_settings()
    monkeypatch.setenv("ZUBOT_PORT", "5555")
    # Without cache_clear we still see the cached instance.
    assert get_settings() is a
    # After cache_clear a new instance is built and reflects the new env.
    get_settings.cache_clear()
    b = get_settings()
    assert b is not a
    assert b.ZUBOT_PORT == 5555


def test_module_exports() -> None:
    """Sanity-check the public API of the config module."""
    mod = importlib.import_module("zubot_ingestion.config")
    assert hasattr(mod, "Settings")
    assert hasattr(mod, "get_settings")
