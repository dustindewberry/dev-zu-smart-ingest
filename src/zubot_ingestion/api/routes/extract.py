"""POST /extract route — placeholder owned by sibling task step-11.

This file exists in the sharp-spark worktree ONLY so the rate-limit
decorator and unit tests have a real route to attach to. The handler body
is a placeholder that returns ``202 Accepted`` with an empty body; the
canonical implementation lives in sibling task-11 (job-submission-endpoint)
and MUST overwrite this file on merge while preserving the
``@limiter.limit("20/minute")`` decorator below.

The 20/minute cap is mandated by CAP-030 because POST /extract is the most
expensive endpoint (multipart upload + Celery enqueue + Ollama vision
inference downstream).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from zubot_ingestion.api.middleware.rate_limit import limiter

router = APIRouter()


@router.post("/extract")
@limiter.limit("20/minute")
async def submit_extract(request: Request) -> JSONResponse:
    """Submit a batch of PDFs for extraction (placeholder for task-11)."""
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"detail": "extract route placeholder — see task step-11"},
    )
