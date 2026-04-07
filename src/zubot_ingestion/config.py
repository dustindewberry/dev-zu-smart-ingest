"""Minimal stub of zubot_ingestion.config for the OTEL task.

TRANSITIONAL stub created by silver-falcon (task-22 / step-22). The canonical
full Pydantic Settings class with all 15 fields is produced by task-4
(warm-cedar worktree, commit 03626b1). On merge, the canonical task-4
config.py MUST overwrite this file. The OTEL_EXPORTER_OTLP_ENDPOINT field
defined here is byte-identical to the canonical version.
"""

from __future__ import annotations

from functools import lru_cache

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ModuleNotFoundError:  # pragma: no cover - fallback for environments
    from pydantic import BaseModel as BaseSettings  # type: ignore

    class SettingsConfigDict(dict):  # type: ignore
        def __init__(self, **kwargs):
            super().__init__(**kwargs)


class Settings(BaseSettings):
    """Minimal Settings class — only the fields the OTEL task reads."""

    model_config = SettingsConfigDict(  # type: ignore[call-arg]
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    ZUBOT_HOST: str = "0.0.0.0"
    ZUBOT_PORT: int = 4243
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the lazily-built singleton Settings instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
