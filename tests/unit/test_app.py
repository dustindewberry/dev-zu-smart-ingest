"""Unit tests for ``zubot_ingestion.api.app``.

Verifies the FastAPI skeleton:
* ``create_app`` returns a fresh ``FastAPI`` instance every call (factory).
* Title and version match the constants module.
* CORS middleware is registered.
* The unhandled-exception handler is registered against ``Exception``.
* The OpenAPI URL, docs URL, and redoc URL are present.
* No business routes are registered yet (only docs/openapi/redoc).
* The lifespan context manager runs cleanly through startup and shutdown.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from zubot_ingestion.api.app import create_app, lifespan
from zubot_ingestion.shared.constants import SERVICE_VERSION


def test_create_app_returns_fastapi_instance() -> None:
    app = create_app()
    assert isinstance(app, FastAPI)


def test_create_app_is_a_factory() -> None:
    """Each call must produce a distinct app instance."""
    a = create_app()
    b = create_app()
    assert a is not b


def test_app_title_and_version() -> None:
    app = create_app()
    assert app.title == "Zubot Ingestion Service"
    assert app.version == SERVICE_VERSION


def test_app_openapi_paths() -> None:
    app = create_app()
    assert app.openapi_url == "/openapi.json"
    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"


def test_cors_middleware_registered() -> None:
    app = create_app()
    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert len(cors) == 1


def test_global_exception_handler_registered() -> None:
    app = create_app()
    assert Exception in app.exception_handlers


def test_expected_business_routes_registered() -> None:
    """The merged codebase wires the full set of business routers: /health,
    /metrics, /extract, /batches/{batch_id}, /jobs/{job_id}, and the
    /review/* queue endpoints. This test documents that wiring so a
    regression (missing router include) is caught immediately.

    The original step-4 version of this test asserted that NO business
    routes were registered because routers were still pending; that
    assertion is obsolete after the later steps landed.
    """
    app = create_app()
    registered = {
        r.path  # type: ignore[attr-defined]
        for r in app.routes
        if hasattr(r, "path")
    }
    expected = {
        "/health",
        "/metrics",
        "/extract",
        "/batches/{batch_id}",
        "/jobs/{job_id}",
        "/review/pending",
        "/review/{job_id}/approve",
        "/review/{job_id}/reject",
    }
    missing = expected - registered
    assert not missing, f"Missing expected business routes: {missing}"


def test_docs_endpoint_serves_html() -> None:
    """The auto-generated /docs page should be reachable on a fresh app."""
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/docs")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


def test_openapi_schema_reflects_metadata() -> None:
    app = create_app()
    schema = app.openapi()
    assert schema["info"]["title"] == "Zubot Ingestion Service"
    assert schema["info"]["version"] == SERVICE_VERSION


def test_unhandled_exception_returns_json_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A route that raises ``Exception`` must be caught by the handler and
    serialized as a JSON 500 — never as a stack trace. The AuthMiddleware
    runs before the exception handler, so we must provide a valid API key
    header on the request to reach the /boom route."""
    monkeypatch.setenv("ZUBOT_INGESTION_API_KEY", "test-api-key")
    from zubot_ingestion.config import get_settings

    get_settings.cache_clear()

    app = create_app()

    @app.get("/_boom")  # registered locally for this test only
    async def _boom() -> None:
        raise RuntimeError("kaboom")

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/_boom", headers={"X-API-Key": "test-api-key"})
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "internal_server_error"
    assert "message" in body

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_lifespan_runs_cleanly() -> None:
    """Driving the lifespan manually proves startup/shutdown logging
    completes without raising. The placeholder body is logging-only, so
    the assertion is simply 'no exception'."""
    app = create_app()
    async with lifespan(app):
        pass  # startup completed if we get here; shutdown runs on exit
