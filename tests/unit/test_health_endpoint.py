"""Unit tests for the /health endpoint (CAP-003 / step-13).

Strategy
========

Each per-dependency probe inside ``zubot_ingestion.api.routes.health``
is monkey-patched at module level to a fast async function returning a
deterministic ``(status, latency_ms, error)`` tuple. We then drive
``health_check`` directly through FastAPI's ``TestClient`` and assert
on:

* the status-aggregation rules (healthy / degraded / unhealthy);
* the response shape (top-level keys, dependencies dict, workers dict);
* the HTTP status code (200 vs 503);
* the timeout-and-exception safety net inside ``_safe_probe``;
* the celery probe's queue-depth math; and
* router wiring into ``create_app()``.

The tests deliberately avoid touching real Postgres / Redis / Ollama /
ChromaDB / Elasticsearch / Celery — every external call is mocked.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from zubot_ingestion.api import app as app_module
from zubot_ingestion.api.routes import health
from zubot_ingestion.shared.constants import (
    AUTH_EXEMPT_PATHS,
    SERVICE_NAME,
    SERVICE_VERSION,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _make_probe(state: str, latency: int = 1, error: str | None = None):
    """Build an async function that returns a deterministic probe tuple."""

    async def _probe() -> tuple[str, int, str | None]:
        return state, latency, error

    return _probe


def _make_failing_probe(message: str):
    async def _probe() -> tuple[str, int, str | None]:
        raise RuntimeError(message)

    return _probe


def _make_hanging_probe():
    async def _probe() -> tuple[str, int, str | None]:
        # Sleep longer than the dependency timeout so _safe_probe times us out.
        await asyncio.sleep(health.DEPENDENCY_CHECK_TIMEOUT_SECONDS + 1)
        return "healthy", 0, None  # pragma: no cover - unreachable

    return _probe


@pytest.fixture
def patched_health(monkeypatch):
    """Replace every dependency probe with a default-healthy stub.

    Individual tests further override specific probes via
    ``monkeypatch.setattr`` to simulate failure modes. The celery probe
    is also stubbed so we never hit a live broker.
    """
    monkeypatch.setattr(
        health, "_probe_postgres", _make_probe("healthy", 5, None)
    )
    monkeypatch.setattr(health, "_probe_redis", _make_probe("healthy", 2, None))
    monkeypatch.setattr(health, "_probe_ollama", _make_probe("healthy", 7, None))
    monkeypatch.setattr(
        health, "_probe_chromadb", _make_probe("healthy", 3, None)
    )
    monkeypatch.setattr(
        health, "_probe_elasticsearch", _make_probe("healthy", 4, None)
    )

    async def _celery_probe() -> dict[str, int]:
        return {"active": 2, "queue_depth": 5}

    monkeypatch.setattr(health, "_safe_celery_probe", _celery_probe)
    return monkeypatch


@pytest.fixture
def client(patched_health) -> TestClient:
    """A TestClient bound to a fresh ``create_app()`` instance."""
    return TestClient(app_module.create_app())


# ──────────────────────────────────────────────────────────────────────
# Happy-path tests
# ──────────────────────────────────────────────────────────────────────


def test_health_returns_200_when_all_deps_healthy(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["service"] == SERVICE_NAME
    assert body["version"] == SERVICE_VERSION


def test_response_shape_matches_blueprint(client: TestClient) -> None:
    body = client.get("/health").json()

    # Top-level shape
    assert set(body.keys()) == {
        "status",
        "service",
        "version",
        "dependencies",
        "workers",
    }

    # Dependencies block — every probed dep must be present
    deps = body["dependencies"]
    assert set(deps.keys()) == {
        "postgres",
        "redis",
        "ollama",
        "chromadb",
        "elasticsearch",
    }
    for entry in deps.values():
        assert set(entry.keys()) == {"status", "latency_ms", "error"}
        assert isinstance(entry["latency_ms"], int)

    # Workers block
    assert set(body["workers"].keys()) == {"active", "queue_depth"}
    assert isinstance(body["workers"]["active"], int)
    assert isinstance(body["workers"]["queue_depth"], int)


def test_dependency_latencies_propagate(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["dependencies"]["postgres"]["latency_ms"] == 5
    assert body["dependencies"]["redis"]["latency_ms"] == 2
    assert body["dependencies"]["ollama"]["latency_ms"] == 7
    assert body["dependencies"]["chromadb"]["latency_ms"] == 3
    assert body["dependencies"]["elasticsearch"]["latency_ms"] == 4


def test_workers_block_propagates(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["workers"] == {"active": 2, "queue_depth": 5}


# ──────────────────────────────────────────────────────────────────────
# Status aggregation
# ──────────────────────────────────────────────────────────────────────


def test_unhealthy_when_postgres_down(client: TestClient, patched_health) -> None:
    patched_health.setattr(
        health,
        "_probe_postgres",
        _make_probe("unhealthy", 12, "connection refused"),
    )
    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unhealthy"
    assert body["dependencies"]["postgres"]["status"] == "unhealthy"
    assert body["dependencies"]["postgres"]["error"] == "connection refused"


def test_unhealthy_when_redis_down(client: TestClient, patched_health) -> None:
    patched_health.setattr(
        health,
        "_probe_redis",
        _make_probe("unhealthy", 8, "ECONNREFUSED"),
    )
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"


def test_unhealthy_when_both_critical_down(
    client: TestClient, patched_health
) -> None:
    patched_health.setattr(
        health, "_probe_postgres", _make_probe("unhealthy", 1, "down")
    )
    patched_health.setattr(
        health, "_probe_redis", _make_probe("unhealthy", 1, "down")
    )
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"


def test_degraded_when_only_optional_down(
    client: TestClient, patched_health
) -> None:
    patched_health.setattr(
        health,
        "_probe_ollama",
        _make_probe("unhealthy", 1, "ollama returned HTTP 502"),
    )
    response = client.get("/health")
    # Degraded still returns HTTP 200 per the blueprint contract.
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["ollama"]["status"] == "unhealthy"
    assert body["dependencies"]["postgres"]["status"] == "healthy"
    assert body["dependencies"]["redis"]["status"] == "healthy"


def test_degraded_when_chromadb_down(
    client: TestClient, patched_health
) -> None:
    patched_health.setattr(
        health, "_probe_chromadb", _make_probe("unhealthy", 1, "boom")
    )
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_degraded_when_elasticsearch_down(
    client: TestClient, patched_health
) -> None:
    patched_health.setattr(
        health,
        "_probe_elasticsearch",
        _make_probe("unhealthy", 1, "no node available"),
    )
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_critical_failure_overrides_optional_failure(
    client: TestClient, patched_health
) -> None:
    """If postgres AND ollama are both down, status must be unhealthy."""
    patched_health.setattr(
        health, "_probe_postgres", _make_probe("unhealthy", 1, "down")
    )
    patched_health.setattr(
        health, "_probe_ollama", _make_probe("unhealthy", 1, "down")
    )
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"


# ──────────────────────────────────────────────────────────────────────
# _safe_probe error handling
# ──────────────────────────────────────────────────────────────────────


def test_probe_exception_is_caught(client: TestClient, patched_health) -> None:
    patched_health.setattr(
        health,
        "_probe_postgres",
        _make_failing_probe("oops the connection blew up"),
    )
    response = client.get("/health")
    body = response.json()
    assert response.status_code == 503
    pg = body["dependencies"]["postgres"]
    assert pg["status"] == "unhealthy"
    assert "RuntimeError" in pg["error"]
    assert "oops the connection blew up" in pg["error"]


def test_probe_timeout_is_caught(monkeypatch, patched_health) -> None:
    """A hung probe should be timed out instead of stalling the response."""
    # Shrink the timeout so the test runs in well under a second.
    monkeypatch.setattr(health, "DEPENDENCY_CHECK_TIMEOUT_SECONDS", 0.05)
    patched_health.setattr(health, "_probe_redis", _make_hanging_probe())

    client = TestClient(app_module.create_app())
    response = client.get("/health")
    body = response.json()

    assert response.status_code == 503  # redis is critical
    assert body["dependencies"]["redis"]["status"] == "unhealthy"
    assert "timeout" in body["dependencies"]["redis"]["error"].lower()


# ──────────────────────────────────────────────────────────────────────
# Celery aggregation
# ──────────────────────────────────────────────────────────────────────


def test_celery_inspect_aggregates_workers_and_queue(monkeypatch) -> None:
    """``_probe_celery`` must derive worker count from active() keys and
    queue depth from the sum of active+reserved task lists."""

    class FakeInspect:
        def active(self) -> dict[str, list[dict]]:
            return {
                "celery@host-1": [{"id": "t1"}, {"id": "t2"}],
                "celery@host-2": [{"id": "t3"}],
            }

        def reserved(self) -> dict[str, list[dict]]:
            return {
                "celery@host-1": [{"id": "t4"}],
                "celery@host-2": [{"id": "t5"}, {"id": "t6"}],
            }

    class FakeControl:
        def inspect(self) -> FakeInspect:
            return FakeInspect()

    class FakeApp:
        control = FakeControl()

    monkeypatch.setattr(
        "zubot_ingestion.services.celery_app.app", FakeApp()
    )

    active, queue_depth = health._probe_celery()
    assert active == 2
    # 3 active + 3 reserved = 6 total
    assert queue_depth == 6


def test_celery_inspect_handles_no_workers(monkeypatch) -> None:
    """When ``inspect.active()`` returns ``None``, both counters must be 0."""

    class FakeInspect:
        def active(self) -> None:
            return None

        def reserved(self) -> None:
            return None

    class FakeControl:
        def inspect(self) -> FakeInspect:
            return FakeInspect()

    class FakeApp:
        control = FakeControl()

    monkeypatch.setattr(
        "zubot_ingestion.services.celery_app.app", FakeApp()
    )
    active, queue_depth = health._probe_celery()
    assert active == 0
    assert queue_depth == 0


async def test_safe_celery_probe_swallows_errors(monkeypatch) -> None:
    """If celery_app.control.inspect blows up, return zeros, not raise."""

    def _boom():
        raise RuntimeError("redis broker unreachable")

    monkeypatch.setattr(health, "_probe_celery", _boom)
    workers = await health._safe_celery_probe()
    assert workers == {"active": 0, "queue_depth": 0}


# ──────────────────────────────────────────────────────────────────────
# Wiring into create_app()
# ──────────────────────────────────────────────────────────────────────


def test_health_router_registered_on_app() -> None:
    app = app_module.create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/health" in paths


def test_health_route_exempt_from_auth_constants() -> None:
    """Sanity check: ``/health`` is in AUTH_EXEMPT_PATHS so middleware
    in step-10 will skip authentication for it."""
    assert "/health" in AUTH_EXEMPT_PATHS


# ──────────────────────────────────────────────────────────────────────
# Aggregation helper
# ──────────────────────────────────────────────────────────────────────


def test_aggregate_status_all_healthy() -> None:
    deps = {
        "postgres": {"status": "healthy", "latency_ms": 1, "error": None},
        "redis": {"status": "healthy", "latency_ms": 1, "error": None},
        "ollama": {"status": "healthy", "latency_ms": 1, "error": None},
        "chromadb": {"status": "healthy", "latency_ms": 1, "error": None},
        "elasticsearch": {"status": "healthy", "latency_ms": 1, "error": None},
    }
    assert health._aggregate_status(deps) == "healthy"


def test_aggregate_status_degraded() -> None:
    deps = {
        "postgres": {"status": "healthy", "latency_ms": 1, "error": None},
        "redis": {"status": "healthy", "latency_ms": 1, "error": None},
        "ollama": {"status": "unhealthy", "latency_ms": 1, "error": "x"},
        "chromadb": {"status": "healthy", "latency_ms": 1, "error": None},
        "elasticsearch": {"status": "healthy", "latency_ms": 1, "error": None},
    }
    assert health._aggregate_status(deps) == "degraded"


def test_aggregate_status_unhealthy() -> None:
    deps = {
        "postgres": {"status": "unhealthy", "latency_ms": 1, "error": "x"},
        "redis": {"status": "healthy", "latency_ms": 1, "error": None},
        "ollama": {"status": "healthy", "latency_ms": 1, "error": None},
        "chromadb": {"status": "healthy", "latency_ms": 1, "error": None},
        "elasticsearch": {"status": "healthy", "latency_ms": 1, "error": None},
    }
    assert health._aggregate_status(deps) == "unhealthy"


# ──────────────────────────────────────────────────────────────────────
# Concurrency: probes really run in parallel
# ──────────────────────────────────────────────────────────────────────


def test_probes_run_concurrently(monkeypatch, patched_health) -> None:
    """Wall clock for the request should approximate max(probe_durations),
    not the sum, proving asyncio.gather is doing its job."""
    delay = 0.15  # 150 ms each

    async def _slow_probe() -> tuple[str, int, str | None]:
        await asyncio.sleep(delay)
        return "healthy", int(delay * 1000), None

    monkeypatch.setattr(health, "_probe_postgres", _slow_probe)
    monkeypatch.setattr(health, "_probe_redis", _slow_probe)
    monkeypatch.setattr(health, "_probe_ollama", _slow_probe)
    monkeypatch.setattr(health, "_probe_chromadb", _slow_probe)
    monkeypatch.setattr(health, "_probe_elasticsearch", _slow_probe)

    import time as _time

    client = TestClient(app_module.create_app())
    start = _time.perf_counter()
    response = client.get("/health")
    elapsed = _time.perf_counter() - start

    assert response.status_code == 200
    # Sequential would be 5 × 0.15 = 0.75s. Concurrent should be well under.
    assert elapsed < 0.5, f"probes did not run concurrently: elapsed={elapsed:.3f}s"
