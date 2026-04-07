"""Transitional stub of application Settings.

NOTE: This is a MINIMAL stub containing only the fields the /health
endpoint reads. The canonical full Pydantic Settings class with all
15 fields is produced by task-4 (warm-cedar worktree). On merge the
canonical version MUST overwrite this stub. Field names and defaults
are byte-identical to the canonical version so no behavioral drift.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Minimal settings stub exposing only fields needed by /health."""

    # Datastores / external services
    DATABASE_URL: str = "postgresql+asyncpg://zubot:zubot@postgres:5432/zubot_ingestion"
    REDIS_URL: str = "redis://redis:6379"
    OLLAMA_HOST: str = "http://ollama:11434"
    CHROMADB_HOST: str = "chromadb"
    CHROMADB_PORT: int = 8000
    ELASTICSEARCH_URL: str = "http://elasticsearch:9200"

    # Auth secrets — canonical has no defaults, but the health endpoint
    # does not use them. Providing dummies here so the stub Settings()
    # call does not fail in unit tests.
    ZUBOT_INGESTION_API_KEY: str = "stub-api-key"
    WOD_JWT_SECRET: str = "stub-jwt-secret"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process-wide Settings singleton."""
    return Settings()  # type: ignore[call-arg]
