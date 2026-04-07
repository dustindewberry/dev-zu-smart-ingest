"""Application configuration via Pydantic Settings.

This is a minimal stub that mirrors the canonical task-4 config so that the
logging integration in this worktree can import LOG_LEVEL and LOG_DIR from
settings. On merge with task-4's branch, the canonical version (which has
all 15 fields) takes precedence.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Minimal Settings stub — only fields touched by the logging integration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    LOG_LEVEL: str = Field(default="INFO")
    LOG_DIR: str | None = Field(default=None)

    ZUBOT_HOST: str = Field(default="0.0.0.0")
    ZUBOT_PORT: int = Field(default=4243)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton."""
    return Settings()
