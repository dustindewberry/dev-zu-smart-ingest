"""Unit tests for the authentication middleware (CAP-026, step-10).

Covers every branch of :class:`zubot_ingestion.api.middleware.auth.AuthMiddleware`
plus the :func:`get_auth_context` FastAPI dependency:

    1. Valid API key  -> 200 + AuthContext{auth_method='api_key'}
    2. Invalid API key -> 401
    3. Valid JWT      -> 200 + AuthContext{auth_method='wod_token', user_id, ...}
    4. Expired JWT    -> 401
    5. Malformed JWT  -> 401 (also covers wrong-signature, wrong-alg, junk)
    6. No auth headers -> 401
    7. Exempt paths   -> 200 (no auth required)

The tests build an isolated FastAPI app per session via a fixture so they
do not depend on the application factory in :mod:`zubot_ingestion.api.app`
beyond importing the middleware itself, and they patch ``get_settings``
to inject deterministic secrets without touching the real environment.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from zubot_ingestion import config as config_module
from zubot_ingestion.api.middleware.auth import AuthMiddleware, get_auth_context
from zubot_ingestion.config import Settings
from zubot_ingestion.shared.types import AuthContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

API_KEY = "test-api-key-value-which-is-comfortably-long"
JWT_SECRET = "test-jwt-secret-value-which-is-at-least-thirty-two-bytes-long-for-hs256"


@pytest.fixture(autouse=True)
def _override_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Replace ``get_settings`` with a deterministic Settings instance.

    Clears the lru_cache around each test so the override never leaks into
    other test modules. The original ``get_settings`` (an ``lru_cache``
    wrapper) is preserved by ``monkeypatch`` and restored automatically
    after the test.
    """
    original_get_settings = config_module.get_settings
    original_get_settings.cache_clear()

    def _fake_get_settings() -> Settings:
        return Settings(
            ZUBOT_INGESTION_API_KEY=API_KEY,
            WOD_JWT_SECRET=JWT_SECRET,
        )

    monkeypatch.setattr(config_module, "get_settings", _fake_get_settings)
    # Also patch the symbol the middleware imported by name at import time.
    from zubot_ingestion.api.middleware import auth as auth_module

    monkeypatch.setattr(auth_module, "get_settings", _fake_get_settings)

    try:
        yield
    finally:
        original_get_settings.cache_clear()


@pytest.fixture()
def app() -> FastAPI:
    """Build a minimal FastAPI app with the AuthMiddleware installed.

    The app exposes:
        * GET /health         — exempt
        * GET /metrics        — exempt
        * GET /docs/anything  — exempt (prefix match)
        * GET /openapi.json   — exempt
        * GET /protected      — requires auth, returns auth context
    """
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> dict[str, str]:
        return {"metrics": "stub"}

    @app.get("/openapi.json")
    async def openapi_json() -> dict[str, str]:
        return {"openapi": "stub"}

    @app.get("/docs/oauth2-redirect")
    async def docs_redirect() -> dict[str, str]:
        return {"redirect": "stub"}

    @app.get("/protected")
    async def protected(
        ctx: AuthContext = Depends(get_auth_context),
    ) -> dict[str, object]:
        return {
            "user_id": ctx.user_id,
            "auth_method": ctx.auth_method,
            "deployment_id": ctx.deployment_id,
            "node_id": ctx.node_id,
        }

    return app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jwt(
    *,
    user_id: str = "user-42",
    deployment_id: int | None = 7,
    node_id: int | None = 13,
    secret: str = JWT_SECRET,
    algorithm: str = "HS256",
    expires_in: dt.timedelta = dt.timedelta(minutes=5),
    extra_claims: dict[str, object] | None = None,
) -> str:
    now = dt.datetime.now(tz=dt.timezone.utc)
    payload: dict[str, object] = {
        "user_id": user_id,
        "deployment_id": deployment_id,
        "node_id": node_id,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, secret, algorithm=algorithm)


# ===========================================================================
# 1. Valid API key
# ===========================================================================


def test_valid_api_key_returns_200_and_attaches_context(
    client: TestClient,
) -> None:
    response = client.get("/protected", headers={"X-API-Key": API_KEY})
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "user_id": "api",
        "auth_method": "api_key",
        "deployment_id": None,
        "node_id": None,
    }


# ===========================================================================
# 2. Invalid API key
# ===========================================================================


def test_invalid_api_key_returns_401(client: TestClient) -> None:
    response = client.get("/protected", headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_empty_api_key_returns_401(client: TestClient) -> None:
    response = client.get("/protected", headers={"X-API-Key": ""})
    assert response.status_code == 401


# ===========================================================================
# 3. Valid JWT
# ===========================================================================


def test_valid_jwt_returns_200_and_extracts_claims(client: TestClient) -> None:
    token = _make_jwt(user_id="alice", deployment_id=99, node_id=1)
    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "user_id": "alice",
        "auth_method": "wod_token",
        "deployment_id": 99,
        "node_id": 1,
    }


def test_valid_jwt_with_string_ids_coerces_to_int(client: TestClient) -> None:
    """JWT claims that arrive as numeric strings should be coerced to int."""
    token = _make_jwt(user_id="bob")
    # rebuild with string ids — re-encode manually so the type is preserved
    payload = jwt.decode(
        token, JWT_SECRET, algorithms=["HS256"], options={"verify_exp": False}
    )
    payload["deployment_id"] = "42"
    payload["node_id"] = "7"
    string_token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")

    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {string_token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["deployment_id"] == 42
    assert body["node_id"] == 7


def test_valid_jwt_with_missing_optional_ids(client: TestClient) -> None:
    """deployment_id / node_id are optional and may be absent from claims."""
    token = _make_jwt(deployment_id=None, node_id=None)
    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["deployment_id"] is None
    assert body["node_id"] is None


# ===========================================================================
# 4. Expired JWT
# ===========================================================================


def test_expired_jwt_returns_401(client: TestClient) -> None:
    token = _make_jwt(expires_in=dt.timedelta(seconds=-30))
    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


# ===========================================================================
# 5. Malformed JWT
# ===========================================================================


def test_garbage_jwt_returns_401(client: TestClient) -> None:
    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer not.a.jwt"},
    )
    assert response.status_code == 401


def test_jwt_signed_with_wrong_secret_returns_401(client: TestClient) -> None:
    token = _make_jwt(secret="some-other-secret")
    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


def test_jwt_with_wrong_algorithm_returns_401(client: TestClient) -> None:
    """A token signed with HS512 should be rejected by the HS256-only decoder."""
    token = _make_jwt(algorithm="HS512")
    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


def test_jwt_missing_user_id_claim_returns_401(client: TestClient) -> None:
    """A well-formed token without ``user_id`` is treated as malformed."""
    now = dt.datetime.now(tz=dt.timezone.utc)
    payload = {
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
        # NOTE: no user_id
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    response = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


# ===========================================================================
# 6. No auth
# ===========================================================================


def test_no_auth_headers_returns_401(client: TestClient) -> None:
    response = client.get("/protected")
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_authorization_header_without_bearer_prefix_returns_401(
    client: TestClient,
) -> None:
    response = client.get(
        "/protected",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert response.status_code == 401


# ===========================================================================
# 7. Exempt paths
# ===========================================================================


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/metrics",
        "/openapi.json",
        "/docs/oauth2-redirect",
    ],
)
def test_exempt_paths_bypass_auth(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200


def test_exempt_paths_still_work_with_invalid_api_key(client: TestClient) -> None:
    """Even a clearly bad credential should not block an exempt route."""
    response = client.get("/health", headers={"X-API-Key": "garbage"})
    assert response.status_code == 200


# ===========================================================================
# get_auth_context safety net
# ===========================================================================


def test_get_auth_context_raises_500_when_middleware_missing() -> None:
    """If a route uses the dependency without middleware installed, fail loudly."""
    app = FastAPI()  # NOTE: no AuthMiddleware

    @app.get("/oops")
    async def oops(ctx: AuthContext = Depends(get_auth_context)) -> dict[str, str]:
        return {"user_id": ctx.user_id}

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/oops")
    assert response.status_code == 500


# ===========================================================================
# Precedence — API key wins over JWT when both are present
# ===========================================================================


def test_api_key_takes_precedence_over_jwt(client: TestClient) -> None:
    token = _make_jwt(user_id="should-be-ignored")
    response = client.get(
        "/protected",
        headers={
            "X-API-Key": API_KEY,
            "Authorization": f"Bearer {token}",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["auth_method"] == "api_key"
    assert body["user_id"] == "api"


# ===========================================================================
# create_app() wiring smoke test
# ===========================================================================


def test_create_app_registers_auth_middleware() -> None:
    """``create_app`` MUST install ``AuthMiddleware`` in the middleware stack.

    Regression guard: if a future refactor of ``api/app.py`` drops the
    ``app.add_middleware(AuthMiddleware)`` line, this test will fail loudly.
    """
    from zubot_ingestion.api.app import create_app

    app = create_app()
    middleware_classes = [m.cls.__name__ for m in app.user_middleware]
    assert "AuthMiddleware" in middleware_classes
