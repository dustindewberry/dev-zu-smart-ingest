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

CAP-030 integration: this route is exempt from rate limiting via
``@limiter.exempt`` so Prometheus scrapes are never throttled and
historical metric continuity is preserved.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from zubot_ingestion.api.middleware.rate_limit import limiter
from zubot_ingestion.infrastructure.metrics.prometheus import (
    CONTENT_TYPE_LATEST,
    generate_latest,
)

__all__ = ["router", "metrics_endpoint"]

router = APIRouter()


@router.get("/metrics")
@limiter.exempt
async def metrics_endpoint() -> Response:
    """Return the Prometheus text-format snapshot of all registered metrics.

    This route is exempt from rate limiting (CAP-030) so Prometheus can
    scrape it on a fixed cadence without producing scrape gaps.
    """
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
