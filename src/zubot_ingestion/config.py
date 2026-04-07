"""Application settings (transitional stub).

NOTE: This is a minimal stub providing only DATABASE_URL — the field needed
by the database session module. The canonical Pydantic Settings class with
all 15 fields is produced by task-4 (fastapi-application-skeleton). On
merge, the canonical version must overwrite this file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    """Minimal settings stub exposing only DATABASE_URL."""

    database_url: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL",
                "postgresql+asyncpg://zubot:zubot@localhost:5432/zubot_ingestion",
            )
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
