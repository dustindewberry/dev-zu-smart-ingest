"""TRANSITIONAL STUB — minimal subset of the canonical task-4 config module.

This file exists ONLY because the sharp-spark worktree was branched from the
initial commit and the canonical ``src/zubot_ingestion/config.py`` produced
by sibling task-4 (warm-cedar) is not present here.

It contains the minimal Settings fields the rate-limit middleware reads
(``REDIS_URL`` and ``RATE_LIMIT_DEFAULT``) plus the ``get_settings`` lru_cache
singleton. On merge with task-4's output, the canonical, full-featured
config module is a strict superset of this stub and MUST overwrite this file
— with the single exception that ``RATE_LIMIT_DEFAULT`` is a NEW field
introduced by THIS task and must be carried into the canonical Settings
class on merge. Default value: ``"100/minute"`` (per CAP-030).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration — minimal subset required by the rate-limit layer."""

    # Datastores / external services
    REDIS_URL: str = "redis://redis:6379"

    # Rate limiting (CAP-030) — global default applied to every endpoint that
    # does not declare its own ``@limiter.limit(...)`` decorator. Per-endpoint
    # limits override this value (e.g. POST /extract uses 20/minute).
    RATE_LIMIT_DEFAULT: str = "100/minute"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process-wide :class:`Settings` singleton."""
    return Settings()
