"""Authentication middleware (CAP-026).

Validates inbound HTTP requests against one of two supported credential
mechanisms:

1. **Static API key** — the ``X-API-Key`` header is compared against
   ``settings.ZUBOT_INGESTION_API_KEY``. This is intended for trusted
   service-to-service callers (Celery workers, the WOD orchestrator, etc.).

2. **WOD JWT bearer token** — the ``Authorization: Bearer <token>`` header is
   decoded with ``settings.WOD_JWT_SECRET`` using the HS256 algorithm. The
   ``exp`` claim is enforced by PyJWT automatically. The middleware extracts
   ``user_id``, ``deployment_id`` and ``node_id`` from the claim payload.

If neither mechanism succeeds the middleware short-circuits the request with
a JSON ``401 Unauthorized`` response. On success an :class:`AuthContext` is
attached to ``request.state.auth_context`` so that downstream route handlers
(or the :func:`get_auth_context` dependency) can read it.

A small set of paths bypass authentication entirely — health checks,
metrics, and the OpenAPI / Swagger / ReDoc documentation endpoints. The list
is sourced from :data:`zubot_ingestion.shared.constants.AUTH_EXEMPT_PATHS`
plus an explicit prefix check for ``/docs`` and ``/openapi`` so that any
sub-paths Swagger may serve (e.g. ``/docs/oauth2-redirect``) also bypass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import jwt
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from zubot_ingestion.config import get_settings
from zubot_ingestion.shared.constants import AUTH_EXEMPT_PATHS
from zubot_ingestion.shared.types import AuthContext

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Awaitable, Callable

    from starlette.responses import Response

    RequestResponseEndpoint = Callable[[Request], Awaitable[Response]]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BEARER_PREFIX = "Bearer "
_JWT_ALGORITHMS = ["HS256"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_exempt(path: str) -> bool:
    """Return True if ``path`` is exempt from authentication.

    A path is exempt when:
        * it is exactly listed in :data:`AUTH_EXEMPT_PATHS`, OR
        * it begins with ``/docs`` (Swagger UI and its sub-routes), OR
        * it begins with ``/openapi`` (the OpenAPI JSON schema endpoint).
    """
    if path in AUTH_EXEMPT_PATHS:
        return True
    if path.startswith("/docs"):
        return True
    if path.startswith("/openapi"):
        return True
    return False


def _coerce_id(value: Any) -> int | None:
    """Best-effort coercion of a JWT claim value to ``int | None``.

    The canonical ``AuthContext.deployment_id`` and ``node_id`` fields are
    typed as ``int | None``. Tokens issued by upstream services may carry
    these values as either JSON numbers or numeric strings; this helper
    normalises both shapes and returns ``None`` for missing or invalid
    values rather than raising. Returning ``None`` causes the auth context
    to fall back to the deployment-agnostic defaults used by ChromaDB and
    Elasticsearch index naming.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is a subclass of int — explicitly disallow it.
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class AuthMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that authenticates every non-exempt request.

    On success, attaches an :class:`AuthContext` to ``request.state``. On
    failure, returns a JSON ``401`` response without invoking the next
    middleware in the chain.
    """

    async def dispatch(  # type: ignore[override]
        self,
        request: Request,
        call_next: "RequestResponseEndpoint",
    ) -> "Response":
        if _is_exempt(request.url.path):
            return await call_next(request)

        auth_context = await self._authenticate(request)
        if auth_context is None:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Unauthorized"},
            )

        request.state.auth_context = auth_context
        return await call_next(request)

    async def _authenticate(self, request: Request) -> AuthContext | None:
        """Try API key first, then JWT. Return ``None`` if both fail."""
        settings = get_settings()

        # ----- 1. API key ------------------------------------------------ #
        api_key = request.headers.get("X-API-Key")
        if api_key and api_key == settings.ZUBOT_INGESTION_API_KEY:
            return AuthContext(
                user_id="api",
                auth_method="api_key",
                deployment_id=None,
                node_id=None,
            )

        # ----- 2. WOD JWT bearer token ----------------------------------- #
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith(_BEARER_PREFIX):
            token = auth_header[len(_BEARER_PREFIX) :]
            try:
                claims = jwt.decode(
                    token,
                    settings.WOD_JWT_SECRET,
                    algorithms=_JWT_ALGORITHMS,
                )
            except jwt.PyJWTError:
                return None

            # ``user_id`` is the only required claim. If it is missing the
            # token is treated as malformed and authentication fails.
            user_id = claims.get("user_id")
            if user_id is None:
                return None

            return AuthContext(
                user_id=str(user_id),
                auth_method="wod_token",
                deployment_id=_coerce_id(claims.get("deployment_id")),
                node_id=_coerce_id(claims.get("node_id")),
            )

        return None


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def get_auth_context(request: Request) -> AuthContext:
    """Return the :class:`AuthContext` populated by :class:`AuthMiddleware`.

    Use this as a FastAPI ``Depends()`` parameter on any route handler that
    needs the calling user's identity::

        @router.post("/extract")
        async def extract(
            auth: AuthContext = Depends(get_auth_context),
        ):
            ...

    If the dependency is invoked on a request that the middleware did not
    authenticate (for example because the route was incorrectly registered
    inside ``AUTH_EXEMPT_PATHS``) it raises a ``500`` error rather than
    silently returning ``None``, which surfaces the misconfiguration loudly
    during development.
    """
    auth_context = getattr(request.state, "auth_context", None)
    if auth_context is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="auth_context missing — AuthMiddleware not installed?",
        )
    return auth_context


__all__ = ["AuthMiddleware", "get_auth_context"]
