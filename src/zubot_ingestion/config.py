"""Transitional config stub for the sharp-owl worktree (task step-10).

This file exists ONLY because the canonical task-4 (warm-cedar) config module
is not present in this worktree's branch. It contains the minimal Settings
fields needed by the auth middleware (``ZUBOT_INGESTION_API_KEY`` and
``WOD_JWT_SECRET``).

On merge with the canonical task-4 output, the canonical, full-featured
config module is a strict superset of this stub and MUST overwrite this file.
The two secret field names defined here are byte-identical to the canonical
version so behaviour is unchanged.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration — minimal subset required by the auth layer."""

    # HTTP server
    ZUBOT_HOST: str = "0.0.0.0"
    ZUBOT_PORT: int = 4243

    # Datastores / external services (defaulted so tests can construct
    # Settings() without exporting the full env)
    DATABASE_URL: str = "postgresql+asyncpg://zubot:zubot@postgres:5432/zubot"
    REDIS_URL: str = "redis://redis:6379"
    OLLAMA_HOST: str = "http://ollama:11434"
    CHROMADB_HOST: str = "chromadb"
    CHROMADB_PORT: int = 8000
    ELASTICSEARCH_URL: str = "http://elasticsearch:9200"

    # Observability
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://phoenix:4317"

    # Auth secrets — provided via env in production. Defaulted here ONLY so
    # the unit-test suite can build the app without needing a real .env.
    # Tests that exercise the auth path inject deterministic values via
    # Settings(...) overrides or get_settings.cache_clear() + monkeypatch.
    ZUBOT_INGESTION_API_KEY: str = "test-api-key"
    WOD_JWT_SECRET: str = "test-jwt-secret"

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "/var/log/zubot-ingestion"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process-wide :class:`Settings` singleton."""
    return Settings()
