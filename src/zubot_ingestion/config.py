"""Application configuration via Pydantic Settings (transitional stub).

NOTE (transitional stub): This worker (task-7 / step-7 celery-configuration)
was branched from the initial commit and therefore could NOT see the canonical
config.py produced by task-4 (commit 03626b1, witty-summit worktree).

This stub exposes ONLY the field required by task-7's celery_app.py
(REDIS_URL) plus the get_settings() singleton accessor. It does NOT enforce
the production secret fields (ZUBOT_INGESTION_API_KEY, WOD_JWT_SECRET) so
that this branch's tests can construct Settings() without environment setup.

When this branch is merged with task-4's branch, the canonical config.py is
the source of truth and should overwrite this stub. The REDIS_URL default is
identical between the two so behavior is unchanged.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed runtime configuration (stub: REDIS_URL only)."""

    REDIS_URL: str = "redis://redis:6379"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process-wide :class:`Settings` singleton."""
    return Settings()


__all__ = ["Settings", "get_settings"]
