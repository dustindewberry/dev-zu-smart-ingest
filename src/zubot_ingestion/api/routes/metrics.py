"""GET /metrics endpoint — Prometheus scrape target.

Implements CAP-028.

The endpoint returns the current snapshot of every metric registered
with the default :class:`prometheus_client.CollectorRegistry`, encoded
in the standard Prometheus text exposition format. The route is
intentionally unauthenticated: ``/metrics`` is listed in
:data:`zubot_ingestion.shared.constants.AUTH_EXEMPT_PATHS` so the
auth middleware skips it, which lets a Prometheus scraper poll the
endpoint without needing API credentials.

The handler is a thin wrapper around
:func:`prometheus_client.generate_latest` and does no work beyond
serialising the registry — every counter increment, histogram
observation, and gauge update happens elsewhere in the codebase
(orchestrator, ollama client, health endpoint).
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from zubot_ingestion.infrastructure.metrics.prometheus import (
    CONTENT_TYPE_LATEST,
    generate_latest,
)

__all__ = ["router", "metrics_endpoint"]

router = APIRouter()


@router.get("/metrics")
async def metrics_endpoint() -> Response:
    """Return the Prometheus text-format snapshot of all registered metrics."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
