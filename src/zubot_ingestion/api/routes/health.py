"""GET /health route — placeholder owned by sibling task step-13.

This route is on the rate-limit exempt list (CAP-030). It is polled by
Kubernetes liveness/readiness probes on a fixed cadence (every few seconds),
and throttling it would cause self-inflicted outages.

Exemption is enforced by decorating the handler with ``@limiter.exempt``,
which registers the handler's ``module.function`` in the limiter's
``_exempt_routes`` set. The limiter's ``_check_request_limit`` short-circuits
exempt routes before any ``default_limits`` are applied, so /health is
unlimited regardless of the global default.

The canonical handler body lives in task-13 (health-endpoint) and MUST
overwrite this file on merge while preserving the ``@limiter.exempt``
decorator.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from zubot_ingestion.api.middleware.rate_limit import limiter

router = APIRouter()


@router.get("/health")
@limiter.exempt
async def get_health() -> JSONResponse:
    """Return service health (placeholder for task-13)."""
    return JSONResponse({"status": "healthy"})
