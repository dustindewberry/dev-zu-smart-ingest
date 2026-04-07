"""GET /review/pending route — placeholder owned by sibling task step-20.

Carries the per-route ``@limiter.limit("100/minute")`` decorator mandated
by CAP-030 for read endpoints. The canonical review-queue endpoints (with
the approve / reject POST handlers) live in task-20 (review-queue-api)
and MUST overwrite this file on merge while preserving the decorator below.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from zubot_ingestion.api.middleware.rate_limit import limiter

router = APIRouter()


@router.get("/review/pending")
@limiter.limit("100/minute")
async def get_review_pending(request: Request) -> JSONResponse:  # noqa: ARG001
    """Return pending review jobs (placeholder for task-20)."""
    return JSONResponse({"items": []})
