"""Async SQLAlchemy engine + session management.

Provides:
- create_engine(url) — build an AsyncEngine with the project's pool defaults
- get_sessionmaker(engine) — wrap an engine in an async_sessionmaker
- get_session() — async context manager that yields an AsyncSession

The default engine and sessionmaker are constructed lazily from
zubot_ingestion.config.get_settings().database_url so callers may simply
write::

    async with get_session() as session:
        ...

For tests that need to override the URL (e.g. SQLite in-memory), pass an
explicit AsyncSession to the repository constructor instead of relying on
the module-level default.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from zubot_ingestion.config import get_settings
from zubot_ingestion.shared.constants import (
    DB_MAX_OVERFLOW,
    DB_POOL_RECYCLE,
    DB_POOL_SIZE,
    DB_POOL_TIMEOUT,
)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def create_engine(url: str | None = None) -> AsyncEngine:
    """Construct an AsyncEngine using the project's pool settings.

    If ``url`` is omitted, the value from settings is used. The engine is
    configured with pool_size=10, max_overflow=20, and pool_pre_ping=True
    so dead connections are recycled automatically.
    """

    db_url = url or get_settings().database_url
    return create_async_engine(
        db_url,
        pool_size=DB_POOL_SIZE,
        max_overflow=DB_MAX_OVERFLOW,
        pool_timeout=DB_POOL_TIMEOUT,
        pool_recycle=DB_POOL_RECYCLE,
        pool_pre_ping=True,
        future=True,
    )


def get_sessionmaker(
    engine: AsyncEngine | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to the given (or default) engine."""

    global _engine, _sessionmaker
    if engine is None:
        if _engine is None:
            _engine = create_engine()
        engine = _engine
    if _sessionmaker is None or engine is not _engine:
        _sessionmaker = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Async context manager that yields a session and commits on exit.

    On exception the session is rolled back. The session is always closed.
    """

    sessionmaker = get_sessionmaker()
    session = sessionmaker()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engine() -> None:
    """Dispose the module-level engine. Useful for app shutdown / tests."""

    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
