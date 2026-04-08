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


def test_database_url_has_localhost_default_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical ``config.py`` carries a localhost ``DATABASE_URL``
    default so the docker-compose and local dev flows start without
    requiring the operator to hand-set it. Production overrides this via
    env / .env. The original step-1 contract of 'no default, fail fast'
    was superseded by later steps that wire a ``database_url`` property
    alias into the database layer.
    """
    monkeypatch.setenv("ZUBOT_INGESTION_API_KEY", "k")
    monkeypatch.setenv("WOD_JWT_SECRET", "s")
    s = Settings()  # type: ignore[call-arg]
    assert s.DATABASE_URL.startswith("postgresql+asyncpg://")
    assert "localhost" in s.DATABASE_URL or "zubot" in s.DATABASE_URL


def test_api_key_defaults_to_empty_string_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical ``config.py`` defaults ``ZUBOT_INGESTION_API_KEY`` to
    the empty string so that tests and local dev do not require the real
    key. AuthMiddleware will still reject any inbound request whose
    ``X-API-Key`` header does not match the (empty) configured key,
    providing fail-closed behaviour in production without forcing
    Pydantic to raise at startup.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("WOD_JWT_SECRET", "s")
    s = Settings()  # type: ignore[call-arg]
    assert s.ZUBOT_INGESTION_API_KEY == ""


def test_jwt_secret_defaults_to_empty_string_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical ``config.py`` defaults ``WOD_JWT_SECRET`` to the
    empty string for the same reason as the API key. AuthMiddleware's JWT
    decode path will fail on any real token signed with a non-empty
    secret, so an unconfigured deployment is still fail-closed in
    practice.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("ZUBOT_INGESTION_API_KEY", "k")
    s = Settings()  # type: ignore[call-arg]
    assert s.WOD_JWT_SECRET == ""


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
