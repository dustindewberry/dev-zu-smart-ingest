"""FastAPI request-logging middleware (CAP-029).

For every incoming HTTP request:
- generate a request_id (uuid4)
- bind request context (request_id, user_id, method, path)
- log "request.start"
- call the downstream handler
- log "request.end" with status_code and duration_ms
- always clear context, even on exceptions

The X-Request-ID response header is set so callers can correlate
client-side logs to server-side traces.
"""

from __future__ import annotations

import time
import uuid
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from zubot_ingestion.infrastructure.logging.config import (
    bind_request_context,
    clear_context,
    get_logger,
)

_logger = get_logger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Bind a request_id, log start/end, and clear context on completion."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Honour an inbound X-Request-ID if a caller / API gateway provided one;
        # otherwise generate a fresh uuid4.
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())

        # user_id may be set by an upstream auth middleware on request.state.
        user_id = getattr(request.state, "user_id", None)

        bind_request_context(
            request_id=request_id,
            user_id=user_id,
            method=request.method,
            path=request.url.path,
        )

        start = time.perf_counter()
        _logger.info("request.start")

        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000.0
            _logger.exception(
                "request.error",
                duration_ms=round(duration_ms, 2),
            )
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            _logger.info(
                "request.end",
                status_code=status_code,
                duration_ms=round(duration_ms, 2),
            )
            # The X-Request-ID header is set by the downstream handler if the
            # request succeeded; we attach it here for the success path.
            try:
                if "response" in locals() and response is not None:  # type: ignore[has-type]
                    response.headers["X-Request-ID"] = request_id  # type: ignore[has-type]
            except Exception:  # pragma: no cover - defensive
                pass
            clear_context()
