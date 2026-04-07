"""GET /batches/{batch_id} route — placeholder owned by sibling task step-12.

Carries the per-route ``@limiter.limit("100/minute")`` decorator mandated
by CAP-030 for read endpoints. The canonical handler body lives in task-12
(status-endpoints) and MUST overwrite this file on merge while preserving
the decorator below.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from zubot_ingestion.api.middleware.rate_limit import limiter

router = APIRouter()


@router.get("/batches/{batch_id}")
@limiter.limit("100/minute")
async def get_batch(request: Request, batch_id: str) -> JSONResponse:  # noqa: ARG001
    """Return batch status (placeholder for task-12).

    ``request`` is unused by the placeholder body but MUST remain in the
    signature — slowapi inspects endpoint parameters for a ``Request`` so
    it can resolve the rate-limit key. Do not remove it without updating
    task step-12's canonical implementation too.
    """
    return JSONResponse({"batch_id": batch_id, "status": "placeholder"})
