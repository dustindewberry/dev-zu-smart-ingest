"""Application configuration via Pydantic Settings (transitional stub).

This file is a minimal stub of the canonical :mod:`zubot_ingestion.config`
module produced by task-4 (warm-cedar). It exposes only the fields this
worktree's imports actually touch (``REDIS_URL`` for the celery broker URL
calculation done at module-load time in ``services.celery_app``, plus
``DATABASE_URL`` with a sensible default).

On merge with task-4 the canonical version must overwrite this stub. The
field names and defaults here are byte-compatible with the canonical
version so no behaviour drift is possible.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Minimal transitional Settings class — see module docstring."""

    ZUBOT_HOST: str = "0.0.0.0"
    ZUBOT_PORT: int = 4243

    DATABASE_URL: str = (
        "postgresql+asyncpg://zubot:zubot@localhost:5432/zubot_ingestion"
    )
    REDIS_URL: str = "redis://redis:6379"

    ZUBOT_INGESTION_API_KEY: str = "dev-api-key"
    WOD_JWT_SECRET: str = "dev-jwt-secret"

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
    return Settings()  # type: ignore[call-arg]
