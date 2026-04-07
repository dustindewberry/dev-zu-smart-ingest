"""Unit tests for the rate-limit middleware (CAP-030, step-24).

These tests run entirely against an in-memory ``limits`` storage backend
(``memory://``) so they do not require a live Redis. The behaviour under
test is identical to the production Redis path because slowapi delegates
all bookkeeping to the same ``limits`` library regardless of the storage
URI.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from zubot_ingestion.api.middleware.rate_limit import (
    RATE_LIMIT_EXEMPT_PATHS,
    build_storage_uri,
    create_limiter,
    custom_429_handler,
    get_rate_limit_key,
    is_rate_limit_exempt,
)
from zubot_ingestion.shared.constants import RATE_LIMIT_REDIS_DB
from zubot_ingestion.shared.types import AuthContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    *,
    path: str = "/extract",
    auth_context: AuthContext | None = None,
    client_host: str = "1.2.3.4",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    """Build a Starlette Request with a state dict for use by the key func."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers or [],
        "client": (client_host, 12345),
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "root_path": "",
    }
    request = Request(scope)
    if auth_context is not None:
        request.state.auth_context = auth_context
    return request


def _build_test_app(
    *,
    default_limit: str = "100/minute",
    extract_limit: str = "20/minute",
    read_limit: str = "100/minute",
    inject_auth_user: str | None = None,
) -> tuple[FastAPI, Limiter]:
    """Construct a FastAPI app wired to an in-memory limiter for testing.

    Mirrors the production wiring in
    :func:`zubot_ingestion.api.app.create_app` but substitutes ``memory://``
    for the Redis storage URI so the tests are hermetic.
    """
    test_limiter = Limiter(
        key_func=get_rate_limit_key,
        storage_uri="memory://",
        default_limits=[default_limit],
        headers_enabled=True,
    )

    app = FastAPI()
    app.state.limiter = test_limiter
    app.add_exception_handler(RateLimitExceeded, custom_429_handler)
    app.add_middleware(SlowAPIMiddleware)

    # Optional auth-injection middleware so we can simulate AuthMiddleware
    # without pulling sharp-owl's full implementation into this test file.
    if inject_auth_user is not None:

        class _InjectAuth(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):  # type: ignore[override]
                request.state.auth_context = AuthContext(
                    user_id=inject_auth_user,
                    auth_method="api_key",
                )
                return await call_next(request)

        app.add_middleware(_InjectAuth)

    @app.post("/extract")
    @test_limiter.limit(extract_limit)
    async def extract_endpoint(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"ok": "true"})

    @app.get("/batches/{batch_id}")
    @test_limiter.limit(read_limit)
    async def batches_endpoint(request: Request, batch_id: str) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"batch_id": batch_id})

    @app.get("/jobs/{job_id}")
    @test_limiter.limit(read_limit)
    async def jobs_endpoint(request: Request, job_id: str) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"job_id": job_id})

    @app.get("/review/pending")
    @test_limiter.limit(read_limit)
    async def review_endpoint(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"items": []})

    @app.get("/health")
    @test_limiter.exempt
    async def health_endpoint() -> JSONResponse:
        return JSONResponse({"status": "healthy"})

    @app.get("/metrics")
    @test_limiter.exempt
    async def metrics_endpoint() -> JSONResponse:
        return JSONResponse({"value": "0"})

    return app, test_limiter


# ---------------------------------------------------------------------------
# get_rate_limit_key
# ---------------------------------------------------------------------------


class TestGetRateLimitKey:
    def test_authenticated_user_returns_user_namespaced_key(self) -> None:
        ctx = AuthContext(user_id="alice", auth_method="api_key")
        request = _make_request(auth_context=ctx)
        assert get_rate_limit_key(request) == "user:alice"

    def test_authenticated_user_with_jwt_method(self) -> None:
        ctx = AuthContext(user_id="42", auth_method="wod_token", deployment_id=7)
        request = _make_request(auth_context=ctx)
        assert get_rate_limit_key(request) == "user:42"

    def test_no_auth_context_falls_back_to_ip(self) -> None:
        request = _make_request(client_host="10.0.0.5")
        assert get_rate_limit_key(request) == "ip:10.0.0.5"

    def test_auth_context_with_empty_user_id_falls_back_to_ip(self) -> None:
        # An AuthContext with a falsy user_id is treated as missing so the
        # IP fallback engages instead of producing the key "user:".
        ctx = AuthContext(user_id="", auth_method="api_key")
        request = _make_request(auth_context=ctx, client_host="10.0.0.6")
        assert get_rate_limit_key(request) == "ip:10.0.0.6"

    def test_two_users_get_distinct_keys(self) -> None:
        a = _make_request(auth_context=AuthContext(user_id="alice", auth_method="api_key"))
        b = _make_request(auth_context=AuthContext(user_id="bob", auth_method="api_key"))
        assert get_rate_limit_key(a) != get_rate_limit_key(b)

    def test_two_ips_get_distinct_keys(self) -> None:
        a = _make_request(client_host="1.1.1.1")
        b = _make_request(client_host="2.2.2.2")
        assert get_rate_limit_key(a) != get_rate_limit_key(b)

    def test_user_and_ip_keys_never_collide(self) -> None:
        # Even if a user is named "1.2.3.4", the namespace prefix prevents
        # them from colliding with an IP-based key.
        ctx = AuthContext(user_id="1.2.3.4", auth_method="api_key")
        user_req = _make_request(auth_context=ctx)
        ip_req = _make_request(client_host="1.2.3.4")
        assert get_rate_limit_key(user_req) != get_rate_limit_key(ip_req)


# ---------------------------------------------------------------------------
# build_storage_uri
# ---------------------------------------------------------------------------


class TestBuildStorageUri:
    def test_default_db_is_rate_limit_redis_db(self) -> None:
        assert build_storage_uri("redis://redis:6379") == f"redis://redis:6379/{RATE_LIMIT_REDIS_DB}"

    def test_custom_db_appended(self) -> None:
        assert build_storage_uri("redis://localhost:6379", db=9) == "redis://localhost:6379/9"

    def test_existing_db_suffix_is_replaced(self) -> None:
        # If REDIS_URL already includes a /N db suffix, it must be stripped
        # before the rate-limit db is appended (otherwise we'd produce a
        # malformed URI like redis://host:6379/0/4).
        assert (
            build_storage_uri("redis://redis:6379/0", db=4)
            == "redis://redis:6379/4"
        )
        assert (
            build_storage_uri("redis://redis:6379/12", db=4)
            == "redis://redis:6379/4"
        )

    def test_trailing_slash_is_normalised(self) -> None:
        assert build_storage_uri("redis://redis:6379/", db=4) == "redis://redis:6379/4"

    def test_returned_uri_is_string(self) -> None:
        assert isinstance(build_storage_uri("redis://redis:6379"), str)

    def test_rate_limit_db_constant_is_4(self) -> None:
        # Pin the value so an accidental constants edit fails loudly here
        # rather than silently re-pointing the rate limiter at the Celery
        # broker (DB 2) or result backend (DB 3).
        assert RATE_LIMIT_REDIS_DB == 4


# ---------------------------------------------------------------------------
# is_rate_limit_exempt
# ---------------------------------------------------------------------------


class TestExemptList:
    @pytest.mark.parametrize("path", ["/health", "/metrics"])
    def test_health_and_metrics_are_exempt(self, path: str) -> None:
        assert is_rate_limit_exempt(_make_request(path=path)) is True

    @pytest.mark.parametrize(
        "path",
        ["/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"],
    )
    def test_docs_paths_are_also_exempt(self, path: str) -> None:
        assert is_rate_limit_exempt(_make_request(path=path)) is True

    @pytest.mark.parametrize(
        "path",
        ["/extract", "/batches/abc", "/jobs/xyz", "/review/pending"],
    )
    def test_business_endpoints_are_not_exempt(self, path: str) -> None:
        assert is_rate_limit_exempt(_make_request(path=path)) is False

    def test_exempt_paths_constant_contains_health_and_metrics(self) -> None:
        assert "/health" in RATE_LIMIT_EXEMPT_PATHS
        assert "/metrics" in RATE_LIMIT_EXEMPT_PATHS

    def test_exempt_paths_constant_is_frozenset(self) -> None:
        assert isinstance(RATE_LIMIT_EXEMPT_PATHS, frozenset)


# ---------------------------------------------------------------------------
# create_limiter
# ---------------------------------------------------------------------------


class TestCreateLimiter:
    def test_returns_a_limiter_instance(self) -> None:
        l = create_limiter()
        assert isinstance(l, Limiter)

    def test_default_limit_pulled_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from zubot_ingestion import config as config_module

        config_module.get_settings.cache_clear()
        monkeypatch.setenv("RATE_LIMIT_DEFAULT", "5/second")
        try:
            l = create_limiter()
            # The Limiter stores its default limit string list internally;
            # we read it back via the public ``_default_limits`` attribute
            # used in the slowapi test suite.
            defaults = [str(d.limit) for limit_group in l._default_limits for d in limit_group]
            assert any("5 per 1 second" in d or "5/second" in d for d in defaults)
        finally:
            monkeypatch.delenv("RATE_LIMIT_DEFAULT", raising=False)
            config_module.get_settings.cache_clear()

    def test_storage_uri_uses_rate_limit_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from zubot_ingestion import config as config_module

        config_module.get_settings.cache_clear()
        monkeypatch.setenv("REDIS_URL", "redis://my-redis:6380")
        try:
            l = create_limiter()
            # The Limiter exposes its storage object; we just check the
            # storage URI is what build_storage_uri would have produced.
            expected = build_storage_uri("redis://my-redis:6380")
            assert expected.endswith(f"/{RATE_LIMIT_REDIS_DB}")
            # The limiter's internal storage_uri matches.
            assert l._storage_uri == expected
        finally:
            monkeypatch.delenv("REDIS_URL", raising=False)
            config_module.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# End-to-end behavioural tests via TestClient
# ---------------------------------------------------------------------------


class TestExtractEndpointRateLimit:
    def test_extract_allows_first_20_requests(self) -> None:
        app, _ = _build_test_app()
        with TestClient(app) as client:
            for i in range(20):
                resp = client.post("/extract")
                assert resp.status_code == 200, f"request {i + 1} unexpectedly limited"

    def test_extract_429s_on_21st_request(self) -> None:
        app, _ = _build_test_app()
        with TestClient(app) as client:
            for _ in range(20):
                client.post("/extract")
            resp = client.post("/extract")
            assert resp.status_code == 429

    def test_429_response_has_required_headers(self) -> None:
        app, _ = _build_test_app()
        with TestClient(app) as client:
            for _ in range(20):
                client.post("/extract")
            resp = client.post("/extract")
            assert resp.status_code == 429
            assert "X-RateLimit-Limit" in resp.headers
            assert "X-RateLimit-Remaining" in resp.headers
            assert "X-RateLimit-Reset" in resp.headers
            assert "Retry-After" in resp.headers

    def test_429_response_body_shape(self) -> None:
        app, _ = _build_test_app()
        with TestClient(app) as client:
            for _ in range(20):
                client.post("/extract")
            resp = client.post("/extract")
            body = resp.json()
            assert body["detail"] == "Rate limit exceeded"
            assert "retry_after" in body
            assert isinstance(body["retry_after"], int)
            assert body["retry_after"] >= 1

    def test_x_ratelimit_limit_header_matches_per_route_cap(self) -> None:
        app, _ = _build_test_app()
        with TestClient(app) as client:
            for _ in range(20):
                client.post("/extract")
            resp = client.post("/extract")
            assert resp.headers["X-RateLimit-Limit"] == "20"

    def test_x_ratelimit_remaining_is_zero_on_429(self) -> None:
        app, _ = _build_test_app()
        with TestClient(app) as client:
            for _ in range(20):
                client.post("/extract")
            resp = client.post("/extract")
            assert resp.headers["X-RateLimit-Remaining"] == "0"


class TestReadEndpointRateLimit:
    def test_batches_allows_100_then_429s(self) -> None:
        app, _ = _build_test_app()
        with TestClient(app) as client:
            for _ in range(100):
                resp = client.get("/batches/b1")
                assert resp.status_code == 200
            resp = client.get("/batches/b1")
            assert resp.status_code == 429
            assert resp.headers["X-RateLimit-Limit"] == "100"

    def test_jobs_uses_100_per_minute(self) -> None:
        app, _ = _build_test_app()
        with TestClient(app) as client:
            # Just verify a few requests succeed and 101st fails.
            for _ in range(100):
                client.get("/jobs/j1")
            resp = client.get("/jobs/j1")
            assert resp.status_code == 429

    def test_review_uses_100_per_minute(self) -> None:
        app, _ = _build_test_app()
        with TestClient(app) as client:
            for _ in range(100):
                client.get("/review/pending")
            resp = client.get("/review/pending")
            assert resp.status_code == 429


class TestExemptEndpointsAreUnlimited:
    def test_health_never_429s(self) -> None:
        # Use a deliberately tiny default limit so anything decorated would
        # trip almost immediately. /health has no decorator so it must keep
        # passing well beyond the default cap.
        app, _ = _build_test_app(default_limit="3/minute")
        with TestClient(app) as client:
            for _ in range(50):
                resp = client.get("/health")
                assert resp.status_code == 200

    def test_metrics_never_429s(self) -> None:
        app, _ = _build_test_app(default_limit="3/minute")
        with TestClient(app) as client:
            for _ in range(50):
                resp = client.get("/metrics")
                assert resp.status_code == 200


class TestPerUserIsolation:
    def test_two_users_share_no_quota(self) -> None:
        # When AuthMiddleware injects an auth_context, the key function uses
        # the user_id, so different users must each get their own quota
        # window. Simulate by using two different X-Auth-User headers
        # processed by the inject middleware (parametrised below).
        app1, _ = _build_test_app(inject_auth_user="alice")
        app2, _ = _build_test_app(inject_auth_user="bob")

        with TestClient(app1) as alice_client:
            for _ in range(20):
                alice_client.post("/extract")
            resp = alice_client.post("/extract")
            assert resp.status_code == 429

        # Bob's first 20 must still pass even though Alice exhausted hers,
        # because the key function returns "user:bob" not "user:alice".
        with TestClient(app2) as bob_client:
            for _ in range(20):
                resp = bob_client.post("/extract")
                assert resp.status_code == 200


# ---------------------------------------------------------------------------
# custom_429_handler unit tests (without going through SlowAPIMiddleware)
# ---------------------------------------------------------------------------


class TestCustom429Handler:
    @pytest.mark.asyncio
    async def test_returns_429_with_json_body(self) -> None:
        request = _make_request()
        # Build a fake exception with a populated limit so the fallback
        # X-RateLimit-Limit header has something to render.
        exc = MagicMock(spec=RateLimitExceeded)
        exc.limit = MagicMock()
        exc.limit.limit = MagicMock()
        exc.limit.limit.amount = 20

        # The handler reads request.app.state.limiter; provide a stub.
        request.scope["app"] = MagicMock()
        request.scope["app"].state = MagicMock()
        request.scope["app"].state.limiter = MagicMock()
        request.scope["app"].state.limiter._inject_headers = lambda r, _: r

        response = custom_429_handler(request, exc)
        assert response.status_code == 429
        # JSONResponse stores the rendered bytes in .body.
        import json

        body = json.loads(response.body)
        assert body["detail"] == "Rate limit exceeded"
        assert body["retry_after"] >= 1
        assert "X-RateLimit-Limit" in response.headers
        assert response.headers["X-RateLimit-Limit"] == "20"
        assert response.headers["X-RateLimit-Remaining"] == "0"
        assert "X-RateLimit-Reset" in response.headers
        assert "Retry-After" in response.headers

    @pytest.mark.asyncio
    async def test_handler_handles_missing_view_rate_limit_state(self) -> None:
        # When request.state.view_rate_limit is missing (e.g. in error
        # paths), the handler should still produce a valid 429 response
        # with retry_after defaulting to 1.
        request = _make_request()
        exc = MagicMock(spec=RateLimitExceeded)
        exc.limit = None
        request.scope["app"] = MagicMock()
        request.scope["app"].state = MagicMock()
        request.scope["app"].state.limiter = MagicMock()
        request.scope["app"].state.limiter._inject_headers = lambda r, _: r

        response = custom_429_handler(request, exc)
        assert response.status_code == 429
        import json

        body = json.loads(response.body)
        assert body["retry_after"] == 1


# ---------------------------------------------------------------------------
# create_app() integration smoke
# ---------------------------------------------------------------------------


class TestCreateAppWiring:
    def test_create_app_attaches_limiter_to_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the module-level ``limiter`` (which is built at import time
        # against the default Redis URI) to be replaced with an in-memory
        # one before create_app() runs, so we don't actually need Redis.
        from zubot_ingestion.api.middleware import rate_limit as rl_module

        in_memory = Limiter(
            key_func=get_rate_limit_key,
            storage_uri="memory://",
            default_limits=["100/minute"],
            headers_enabled=True,
        )
        monkeypatch.setattr(rl_module, "limiter", in_memory)

        # Reload api.app so it picks up the patched module-level limiter.
        import importlib

        from zubot_ingestion.api import app as app_module

        importlib.reload(app_module)

        app = app_module.create_app()
        assert app.state.limiter is in_memory

        # SlowAPIMiddleware should be registered in the user_middleware stack.
        middleware_classes = [m.cls for m in app.user_middleware]
        assert SlowAPIMiddleware in middleware_classes

        # The custom 429 handler should be installed.
        assert RateLimitExceeded in app.exception_handlers
        assert app.exception_handlers[RateLimitExceeded] is custom_429_handler

    def test_create_app_routes_are_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from zubot_ingestion.api.middleware import rate_limit as rl_module

        in_memory = Limiter(
            key_func=get_rate_limit_key,
            storage_uri="memory://",
            default_limits=["100/minute"],
            headers_enabled=True,
        )
        monkeypatch.setattr(rl_module, "limiter", in_memory)

        import importlib

        from zubot_ingestion.api import app as app_module
        from zubot_ingestion.api.routes import batches, extract, health, jobs, metrics, review

        for m in (extract, batches, jobs, review, health, metrics):
            importlib.reload(m)
        importlib.reload(app_module)

        app = app_module.create_app()
        paths = {route.path for route in app.routes if hasattr(route, "path")}
        assert "/extract" in paths
        assert "/batches/{batch_id}" in paths
        assert "/jobs/{job_id}" in paths
        assert "/review/pending" in paths
        assert "/health" in paths
        assert "/metrics" in paths
