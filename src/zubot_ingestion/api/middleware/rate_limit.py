"""Rate limiting middleware (CAP-030).

Implements per-user / per-IP HTTP rate limiting using ``slowapi`` (a thin
Starlette/FastAPI wrapper around the ``limits`` library) with a Redis storage
backend so all uvicorn workers share the same counters.

Design notes
------------

* **Key function** — :func:`get_rate_limit_key` prefers the authenticated
  principal (``request.state.auth_context.user_id``) so that two users sharing
  a single egress NAT do not crowd each other out. If no auth context is
  present (e.g. on routes that bypass auth, or when AuthMiddleware did not
  run before SlowAPIMiddleware) it falls back to the remote IP address via
  ``slowapi.util.get_remote_address``.

* **Storage backend** — Redis DB 4 (``RATE_LIMIT_REDIS_DB``), distinct from
  the Celery broker (DB 2) and result backend (DB 3) so a queue purge cannot
  reset rate-limit windows. The full URI is built from
  ``settings.REDIS_URL`` plus the constant.

* **Default limit** — ``settings.RATE_LIMIT_DEFAULT`` (``100/minute``) is
  applied to every endpoint that does not declare its own ``@limiter.limit``
  decorator. Per-endpoint decorators override the default.

* **Exempt endpoints** — ``/health`` and ``/metrics`` MUST never be rate
  limited because they are scraped by Kubernetes and Prometheus on a fixed
  cadence. The :func:`is_rate_limit_exempt` predicate is exposed for use as
  the ``exempt_when=`` argument on per-route decorators (or as a custom
  ``default_limits_exempt_when`` argument when constructing the limiter).

* **429 handler** — :func:`custom_429_handler` returns a JSON body of the
  form ``{"detail": "Rate limit exceeded", "retry_after": <int seconds>}``
  with the four spec-mandated headers (``X-RateLimit-Limit``,
  ``X-RateLimit-Remaining``, ``X-RateLimit-Reset``, ``Retry-After``) injected
  by slowapi's ``Limiter._inject_headers``. The ``retry_after`` JSON field
  mirrors the ``Retry-After`` header value (delta-seconds, integer).
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from zubot_ingestion.config import get_settings
from zubot_ingestion.shared.constants import AUTH_EXEMPT_PATHS, RATE_LIMIT_REDIS_DB

if TYPE_CHECKING:  # pragma: no cover
    from starlette.responses import Response

__all__ = [
    "RATE_LIMIT_EXEMPT_PATHS",
    "SlowAPIMiddleware",
    "RateLimitExceeded",
    "build_storage_uri",
    "create_limiter",
    "custom_429_handler",
    "get_rate_limit_key",
    "is_rate_limit_exempt",
    "limiter",
]


# ---------------------------------------------------------------------------
# Exempt routes
# ---------------------------------------------------------------------------

#: Routes that MUST never be rate limited. ``/health`` is polled by Kubernetes
#: liveness/readiness probes; ``/metrics`` is scraped by Prometheus. Throttling
#: either of these would manufacture self-inflicted outages or scrape gaps.
RATE_LIMIT_EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/metrics"})


def is_rate_limit_exempt(request: Request) -> bool:
    """Return ``True`` if ``request`` targets an exempt path.

    Pass this as ``exempt_when=is_rate_limit_exempt`` on any
    ``@limiter.limit(...)`` decorator that should bypass enforcement, or use
    it on the limiter's defaults so that the global default limit is also
    skipped on health/metrics endpoints.
    """
    path = request.url.path
    if path in RATE_LIMIT_EXEMPT_PATHS:
        return True
    # Also exempt the broader auth-exempt list (docs, openapi, redoc) so that
    # interactive API exploration is never throttled.
    if path in AUTH_EXEMPT_PATHS:
        return True
    if path.startswith("/docs"):
        return True
    if path.startswith("/openapi"):
        return True
    return False


# ---------------------------------------------------------------------------
# Key function
# ---------------------------------------------------------------------------


def get_rate_limit_key(request: Request) -> str:
    """Return the bucket key used to identify the caller for rate limiting.

    Strategy:
        1. If :class:`AuthMiddleware` already attached an ``AuthContext`` to
           ``request.state``, key on the authenticated ``user_id`` so that
           two clients sharing an egress NAT cannot starve each other.
        2. Otherwise fall back to the remote address (``X-Forwarded-For``
           when present, else the socket peer address) via
           :func:`slowapi.util.get_remote_address`.

    The returned key is namespaced (``user:`` / ``ip:``) so the two pools
    cannot collide and the chosen strategy is visible in Redis when
    debugging.
    """
    auth_context = getattr(request.state, "auth_context", None)
    if auth_context is not None:
        user_id = getattr(auth_context, "user_id", None)
        if user_id:
            return f"user:{user_id}"
    return f"ip:{get_remote_address(request)}"


# ---------------------------------------------------------------------------
# Storage URI
# ---------------------------------------------------------------------------


def build_storage_uri(redis_url: str, db: int = RATE_LIMIT_REDIS_DB) -> str:
    """Compose a ``limits``-compatible storage URI from a Redis base URL.

    The function tolerates a base URL that already includes a database
    suffix (e.g. ``redis://redis:6379/0``) by stripping the trailing
    ``/<int>`` segment before appending the rate-limit DB number. This keeps
    behaviour deterministic regardless of how operators populate
    ``REDIS_URL`` in the environment.

    Examples:
        >>> build_storage_uri("redis://redis:6379")
        'redis://redis:6379/4'
        >>> build_storage_uri("redis://redis:6379/0")
        'redis://redis:6379/4'
        >>> build_storage_uri("redis://localhost:6379", db=4)
        'redis://localhost:6379/4'
    """
    base = redis_url.rstrip("/")
    # Strip an existing trailing /<int> segment if present so we don't
    # produce malformed URIs like ``redis://host:6379/0/4``.
    last_slash = base.rfind("/")
    if last_slash > base.find("//") + 1:  # ignore the // in the scheme
        tail = base[last_slash + 1 :]
        if tail.isdigit():
            base = base[:last_slash]
    return f"{base}/{db}"


# ---------------------------------------------------------------------------
# Limiter factory + module-level singleton
# ---------------------------------------------------------------------------


def create_limiter() -> Limiter:
    """Build a fresh :class:`slowapi.Limiter` from current settings.

    Exposed as a factory (rather than only the module-level
    :data:`limiter`) so unit tests can construct an isolated limiter without
    relying on import-time evaluation of the global ``Settings`` singleton.
    """
    settings = get_settings()
    return Limiter(
        key_func=get_rate_limit_key,
        storage_uri=build_storage_uri(settings.REDIS_URL),
        default_limits=[settings.RATE_LIMIT_DEFAULT],
        headers_enabled=True,
    )


#: Process-wide limiter instance imported by route modules and the
#: :func:`zubot_ingestion.api.app.create_app` factory.
limiter: Limiter = create_limiter()


# ---------------------------------------------------------------------------
# Custom 429 handler
# ---------------------------------------------------------------------------


def _retry_after_seconds(request: Request) -> int:
    """Best-effort calculation of the seconds remaining in the current window.

    Reads ``request.state.view_rate_limit`` (set by ``Limiter._check_request_limit``
    during enforcement) and asks the underlying ``limits`` storage for the
    window reset timestamp. Falls back to ``1`` second on any failure so the
    response always carries a positive integer ``retry_after``.
    """
    current_limit = getattr(request.state, "view_rate_limit", None)
    if current_limit is None:
        return 1

    try:
        app_limiter: Limiter = request.app.state.limiter
        window_stats = app_limiter.limiter.get_window_stats(
            current_limit[0], *current_limit[1]
        )
        # window_stats[0] is the reset epoch (seconds since 1970).
        delta = int(math.ceil(window_stats[0] - time.time()))
        return max(1, delta)
    except Exception:  # pragma: no cover - storage failure fallback
        return 1


def custom_429_handler(request: Request, exc: RateLimitExceeded) -> "Response":
    """Format a JSON 429 response and set the four rate-limit headers.

    Body shape::

        {"detail": "Rate limit exceeded", "retry_after": <int>}

    Headers set:
        * ``X-RateLimit-Limit``     — total requests allowed per window
        * ``X-RateLimit-Remaining`` — requests remaining (always 0 on 429)
        * ``X-RateLimit-Reset``     — epoch seconds until window resets
        * ``Retry-After``           — delta-seconds clients should wait

    The header values are populated by slowapi's ``Limiter._inject_headers``
    using the same Redis-backed window state that triggered the limit, so
    they are guaranteed to be consistent with the actual remaining quota.
    """
    retry_after = _retry_after_seconds(request)
    response = JSONResponse(
        status_code=429,
        content={
            "detail": "Rate limit exceeded",
            "retry_after": retry_after,
        },
    )
    # Pre-seed the Retry-After header so the slowapi injector preserves the
    # delta-seconds form (and not an HTTP-date) regardless of the limiter's
    # ``retry_after`` configuration.
    response.headers["Retry-After"] = str(retry_after)

    try:
        app_limiter: Limiter = request.app.state.limiter
        current_limit = getattr(request.state, "view_rate_limit", None)
        if current_limit is not None:
            app_limiter._inject_headers(response, current_limit)
    except Exception:  # pragma: no cover - injector failure should not 500
        pass

    # Ensure the four spec-mandated headers are present even if injection
    # silently failed (e.g. storage unreachable). The injector uses the
    # canonical capitalisation, so we only fill in absent values.
    if "X-RateLimit-Limit" not in response.headers and exc.limit is not None:
        response.headers["X-RateLimit-Limit"] = str(exc.limit.limit.amount)
    if "X-RateLimit-Remaining" not in response.headers:
        response.headers["X-RateLimit-Remaining"] = "0"
    if "X-RateLimit-Reset" not in response.headers:
        response.headers["X-RateLimit-Reset"] = str(int(time.time()) + retry_after)
    if "Retry-After" not in response.headers:
        response.headers["Retry-After"] = str(retry_after)

    return response
