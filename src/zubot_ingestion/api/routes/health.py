"""Health-check endpoint (``GET /health``).

Implements **CAP-003**.

The endpoint runs a fan-out of liveness probes against every external
dependency the service relies on:

    * **PostgreSQL** — open an async session, ``SELECT 1``
    * **Redis**       — ``redis.asyncio`` client ``PING``
    * **Ollama**      — ``GET {OLLAMA_HOST}/api/tags``
    * **ChromaDB**    — ``chromadb.HttpClient.list_collections()``
    * **Elasticsearch** — ``client.cluster.health()``
    * **Celery**      — ``celery_app.control.inspect()`` to compute
      worker count and queue depth.

All probes execute concurrently via :func:`asyncio.gather`, each one
wrapped in its own try/except so a single failing dependency cannot
prevent the rest of the report from being generated. Each per-dependency
probe is also constrained by ``DEPENDENCY_CHECK_TIMEOUT_SECONDS`` so a
hung dependency cannot stall the response indefinitely.

Status aggregation rules
========================

* ``healthy``    — every critical dep (postgres, redis) AND every
  optional dep (ollama, chromadb, elasticsearch) is healthy.
* ``degraded``   — every critical dep is healthy but at least one
  optional dep is unhealthy.
* ``unhealthy``  — at least one critical dep is unhealthy.

The HTTP status code is **200** for ``healthy`` and ``degraded``,
**503** for ``unhealthy``.

This endpoint is exempt from authentication via ``AUTH_EXEMPT_PATHS``
in ``zubot_ingestion.shared.constants`` so monitoring systems can poll
it without credentials.

CAP-028 integration: every health response refreshes the
``zubot_queue_depth`` Prometheus gauge from the Celery inspect probe
result via :func:`update_queue_depth_gauge`. Updating the gauge from
the health endpoint piggybacks on the Celery RPC the probe already
issues — no additional broker traffic is required, and Prometheus
scrapes (~15 s) provide the right cadence to keep the gauge fresh.

CAP-030 integration: this route is exempt from rate limiting via
``@limiter.exempt`` so Kubernetes liveness/readiness probes are never
throttled.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Response, status

from zubot_ingestion.api.middleware.rate_limit import limiter
from zubot_ingestion.config import get_settings
from zubot_ingestion.infrastructure.metrics.prometheus import queue_depth
from zubot_ingestion.shared.constants import SERVICE_NAME, SERVICE_VERSION

router = APIRouter()

# Maximum time we are willing to wait for any single dependency probe.
# Anything slower than this counts as ``unhealthy`` for that dependency.
DEPENDENCY_CHECK_TIMEOUT_SECONDS: float = 5.0

# Critical dependencies — failure of any one of these forces the
# overall status to ``unhealthy``. Everything not in this set is
# treated as optional and only downgrades the status to ``degraded``.
CRITICAL_DEPENDENCIES: frozenset[str] = frozenset({"postgres", "redis"})


def update_queue_depth_gauge(current_celery_queue_depth: int) -> None:
    """Set the ``zubot_queue_depth`` gauge to the current Celery queue depth.

    Args:
        current_celery_queue_depth: The total number of tasks currently
            being processed or waiting in any worker's local queue,
            as reported by ``celery_app.control.inspect().active()`` +
            ``.reserved()``. Must be a non-negative integer; negative
            values are clamped to zero.
    """
    queue_depth.set(max(0, int(current_celery_queue_depth)))


# ──────────────────────────────────────────────────────────────────────
# Per-dependency probes
#
# Each probe is an ``async`` function that returns a 3-tuple
# ``(status, latency_ms, error)`` matching the dependency entry shape
# in the response body. They MUST NOT raise — any exception is
# converted to ``("unhealthy", elapsed_ms, str(exc))`` by the wrapper
# below.
# ──────────────────────────────────────────────────────────────────────


async def _probe_postgres() -> tuple[str, int, str | None]:
    """Open an async SQLAlchemy session and execute ``SELECT 1``."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    sessionmaker = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    start = time.perf_counter()
    try:
        async with sessionmaker() as session:
            await session.execute(text("SELECT 1"))
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return "healthy", elapsed_ms, None
    finally:
        await engine.dispose()


async def _probe_redis() -> tuple[str, int, str | None]:
    """Issue a Redis ``PING``."""
    import redis.asyncio as redis_async

    settings = get_settings()
    client = redis_async.from_url(settings.REDIS_URL)
    start = time.perf_counter()
    try:
        await client.ping()
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return "healthy", elapsed_ms, None
    finally:
        try:
            await client.aclose()
        except Exception:  # pragma: no cover - close-time errors are non-fatal
            pass


async def _probe_ollama() -> tuple[str, int, str | None]:
    """``GET {OLLAMA_HOST}/api/tags``."""
    import httpx

    settings = get_settings()
    url = f"{settings.OLLAMA_HOST.rstrip('/')}/api/tags"
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=DEPENDENCY_CHECK_TIMEOUT_SECONDS) as client:
        response = await client.get(url)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    if response.status_code == 200:
        return "healthy", elapsed_ms, None
    return (
        "unhealthy",
        elapsed_ms,
        f"ollama returned HTTP {response.status_code}",
    )


async def _probe_chromadb() -> tuple[str, int, str | None]:
    """Use ``chromadb.HttpClient.list_collections()``.

    The ``chromadb`` SDK is synchronous so we run the call in a worker
    thread via :func:`asyncio.to_thread` to avoid blocking the event
    loop.
    """
    import chromadb  # type: ignore[import-not-found]

    settings = get_settings()

    def _list_collections() -> Any:
        client = chromadb.HttpClient(
            host=settings.CHROMADB_HOST,
            port=settings.CHROMADB_PORT,
        )
        return client.list_collections()

    start = time.perf_counter()
    await asyncio.to_thread(_list_collections)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return "healthy", elapsed_ms, None


async def _probe_elasticsearch() -> tuple[str, int, str | None]:
    """Call ``client.cluster.health()`` on the async ES client."""
    from elasticsearch import AsyncElasticsearch  # type: ignore[import-not-found]

    settings = get_settings()
    client = AsyncElasticsearch(hosts=[settings.ELASTICSEARCH_URL])
    start = time.perf_counter()
    try:
        await client.cluster.health()
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return "healthy", elapsed_ms, None
    finally:
        try:
            await client.close()
        except Exception:  # pragma: no cover
            pass


def _probe_celery() -> tuple[int, int]:
    """Synchronously inspect the Celery cluster.

    Returns ``(active_workers, queue_depth)``. ``celery.control.inspect``
    is purely synchronous AMQP RPC and returns ``None`` if no workers
    respond, in which case both counters collapse to zero.
    """
    from zubot_ingestion.services.celery_app import app as celery_app

    inspect = celery_app.control.inspect()
    active = inspect.active() or {}
    reserved = inspect.reserved() or {}

    active_workers = len(active)
    queue_depth = sum(len(tasks) for tasks in active.values()) + sum(
        len(tasks) for tasks in reserved.values()
    )
    return active_workers, queue_depth


# ──────────────────────────────────────────────────────────────────────
# Probe wrapper
# ──────────────────────────────────────────────────────────────────────

DependencyProbe = Callable[[], Awaitable[tuple[str, int, str | None]]]


async def _safe_probe(
    name: str, probe: DependencyProbe
) -> tuple[str, dict[str, Any]]:
    """Run a probe with a hard timeout, never raising.

    The return tuple is ``(name, dependency_entry)`` so the caller can
    splat results back into the response dict in deterministic order.
    """
    start = time.perf_counter()
    try:
        result_status, latency_ms, error = await asyncio.wait_for(
            probe(), timeout=DEPENDENCY_CHECK_TIMEOUT_SECONDS
        )
        return name, {
            "status": result_status,
            "latency_ms": latency_ms,
            "error": error,
        }
    except asyncio.TimeoutError:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return name, {
            "status": "unhealthy",
            "latency_ms": elapsed_ms,
            "error": (
                f"probe exceeded {DEPENDENCY_CHECK_TIMEOUT_SECONDS}s timeout"
            ),
        }
    except Exception as exc:  # noqa: BLE001 - we deliberately swallow all
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return name, {
            "status": "unhealthy",
            "latency_ms": elapsed_ms,
            "error": f"{type(exc).__name__}: {exc}",
        }


async def _safe_celery_probe() -> dict[str, int]:
    """Run the Celery inspect probe in a worker thread.

    Failures collapse to ``{"active": 0, "queue_depth": 0}``.
    """
    try:
        active, queue_depth = await asyncio.wait_for(
            asyncio.to_thread(_probe_celery),
            timeout=DEPENDENCY_CHECK_TIMEOUT_SECONDS,
        )
        return {"active": active, "queue_depth": queue_depth}
    except Exception:  # noqa: BLE001
        return {"active": 0, "queue_depth": 0}


# ──────────────────────────────────────────────────────────────────────
# Status aggregation
# ──────────────────────────────────────────────────────────────────────


def _aggregate_status(deps: dict[str, dict[str, Any]]) -> str:
    """Apply the status-aggregation rules described in the module docstring."""
    critical_unhealthy = any(
        deps[name]["status"] != "healthy"
        for name in CRITICAL_DEPENDENCIES
        if name in deps
    )
    if critical_unhealthy:
        return "unhealthy"

    optional_unhealthy = any(
        entry["status"] != "healthy"
        for name, entry in deps.items()
        if name not in CRITICAL_DEPENDENCIES
    )
    if optional_unhealthy:
        return "degraded"

    return "healthy"


# ──────────────────────────────────────────────────────────────────────
# Route handler
# ──────────────────────────────────────────────────────────────────────


@router.get("/health")
@limiter.exempt
async def health_check(response: Response) -> dict[str, Any]:
    """Aggregate liveness probe across every external dependency.

    Returns HTTP 200 for ``healthy``/``degraded`` and HTTP 503 for
    ``unhealthy``. The body always conforms to the schema in the module
    docstring, even on failure, so monitoring systems can parse it
    unconditionally.

    This route is exempt from rate limiting (CAP-030) so it can be
    polled by Kubernetes liveness/readiness probes on a fixed cadence
    without being throttled.
    """
    probes: list[tuple[str, DependencyProbe]] = [
        ("postgres", _probe_postgres),
        ("redis", _probe_redis),
        ("ollama", _probe_ollama),
        ("chromadb", _probe_chromadb),
        ("elasticsearch", _probe_elasticsearch),
    ]

    # Run every dependency probe + the celery inspect concurrently.
    probe_tasks = [_safe_probe(name, probe) for name, probe in probes]
    celery_task = _safe_celery_probe()
    probe_results, workers = await asyncio.gather(
        asyncio.gather(*probe_tasks),
        celery_task,
    )

    dependencies: dict[str, dict[str, Any]] = {
        name: entry for name, entry in probe_results
    }
    overall = _aggregate_status(dependencies)

    # CAP-028: refresh the Prometheus queue_depth gauge from the value
    # we just computed via the Celery inspect probe. The helper clamps
    # negative values and coerces to int.
    update_queue_depth_gauge(workers.get("queue_depth", 0))

    response.status_code = (
        status.HTTP_200_OK
        if overall in ("healthy", "degraded")
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )

    return {
        "status": overall,
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "dependencies": dependencies,
        "workers": workers,
    }


__all__ = [
    "router",
    "health_check",
    "update_queue_depth_gauge",
    "DEPENDENCY_CHECK_TIMEOUT_SECONDS",
    "CRITICAL_DEPENDENCIES",
]
