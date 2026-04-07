"""GET /metrics route — placeholder owned by sibling task step-23.

This route is on the rate-limit exempt list (CAP-030). It is scraped by
Prometheus on a fixed cadence; throttling it would cause scrape gaps that
break alerting and historical metric continuity.

Exemption is enforced via ``@limiter.exempt`` which registers the handler
in the limiter's ``_exempt_routes`` set so ``default_limits`` are bypassed
entirely.

The canonical handler body (full Prometheus exposition format) lives in
task-23 (prometheus-metrics) and MUST overwrite this file on merge while
preserving the ``@limiter.exempt`` decorator.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from zubot_ingestion.api.middleware.rate_limit import limiter

router = APIRouter()


@router.get("/metrics", response_class=PlainTextResponse)
@limiter.exempt
async def get_metrics() -> str:
    """Return Prometheus metrics (placeholder for task-23)."""
    return "# zubot_ingestion metrics placeholder — see task step-23\n"
