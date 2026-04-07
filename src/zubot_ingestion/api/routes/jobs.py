"""GET /jobs/{job_id} route — placeholder owned by sibling task step-12.

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


@router.get("/jobs/{job_id}")
@limiter.limit("100/minute")
async def get_job(request: Request, job_id: str) -> JSONResponse:  # noqa: ARG001
    """Return job detail (placeholder for task-12)."""
    return JSONResponse({"job_id": job_id, "status": "placeholder"})
