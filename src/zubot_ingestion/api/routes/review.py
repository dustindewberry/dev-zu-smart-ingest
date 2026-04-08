"""Review queue API (CAP-022 / step-20 canonical).

Real implementation of the human review queue endpoints. Replaces the
24-line placeholder previously owned by sibling task step-20.

Endpoints
---------

* ``GET  /review/pending``           — paginated listing of jobs whose
  ``confidence_tier`` is :attr:`ConfidenceTier.REVIEW` and which have not
  yet been approved or rejected. Pagination via ``?limit=...&offset=...``.

* ``POST /review/{job_id}/approve``  — transition a REVIEW-tier job to
  the terminal :attr:`JobStatus.COMPLETED` state. Idempotency-guarded so
  a non-REVIEW job returns 409 Conflict.

* ``POST /review/{job_id}/reject``   — transition a REVIEW-tier job to
  the terminal :attr:`JobStatus.REJECTED` state. Accepts an optional
  ``{"reason": "..."}`` JSON body that is persisted as the job's
  ``error_message``.

All three endpoints reuse the existing slowapi ``@limiter.limit(...)``
pattern wired by CAP-030 (rate limit middleware) and depend on the
``async with get_job_repository() as repo:`` context-manager shape
delivered by task-5 (composition-root-fix).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from zubot_ingestion.api.middleware.rate_limit import limiter
from zubot_ingestion.domain.enums import ConfidenceTier, JobStatus
from zubot_ingestion.services import get_job_repository

router = APIRouter(tags=["review"])


# ---------------------------------------------------------------------------
# Pydantic response / request models
# ---------------------------------------------------------------------------


class ReviewItem(BaseModel):
    """A single REVIEW-tier job surfaced in the pending queue."""

    job_id: str
    confidence_tier: str
    confidence_score: float | None
    result: dict[str, Any] | None
    created_at: str


class ReviewPendingResponse(BaseModel):
    """Paginated listing wrapper for the pending-review queue."""

    items: list[ReviewItem]
    total: int
    limit: int
    offset: int


class RejectBody(BaseModel):
    """Optional JSON body for ``POST /review/{job_id}/reject``."""

    reason: str = Field(default="", max_length=1000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_review_item(job: Any) -> ReviewItem:
    """Convert a domain :class:`Job` entity to a :class:`ReviewItem`.

    Uses ``getattr`` for the timestamp because ``created_at`` is a non-None
    field on the canonical entity but defensive coding makes the route
    resilient to stale or partially-populated rows.
    """
    tier = getattr(job, "confidence_tier", None)
    tier_value = tier.value if tier is not None else ""

    confidence_score: float | None = getattr(job, "overall_confidence", None)
    created_at = getattr(job, "created_at", None)
    created_at_iso = created_at.isoformat() if created_at is not None else ""

    return ReviewItem(
        job_id=str(job.job_id),
        confidence_tier=tier_value,
        confidence_score=confidence_score,
        result=job.result,
        created_at=created_at_iso,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/review/pending",
    response_model=ReviewPendingResponse,
    summary="List jobs awaiting human review",
)
@limiter.limit("30/minute")
async def list_pending_reviews(
    request: Request,  # noqa: ARG001 — required by slowapi for rate-limit key resolution
    limit: int = 50,
    offset: int = 0,
) -> ReviewPendingResponse:
    """Return paginated REVIEW-tier jobs.

    The repository's :meth:`list_pending_reviews` returns
    ``(items, total)`` filtered on
    ``confidence_tier == ConfidenceTier.REVIEW`` AND
    ``status != JobStatus.REJECTED`` so previously-rejected jobs are not
    re-surfaced to reviewers.

    Parameters are clamped:
        * ``limit``: 1..200 (default 50)
        * ``offset``: >= 0   (default 0)
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    # The repository's existing API is page-based (1-indexed). Translate
    # the offset/limit query semantics into the page/per_page form.
    per_page = limit
    page = (offset // per_page) + 1

    async with get_job_repository() as repo:
        items, total = await repo.list_pending_reviews(  # type: ignore[attr-defined]
            page=page,
            per_page=per_page,
        )

    return ReviewPendingResponse(
        items=[_serialize_review_item(job) for job in items],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.post(
    "/review/{job_id}/approve",
    status_code=status.HTTP_200_OK,
    summary="Approve a REVIEW-tier job (transitions to COMPLETED)",
)
@limiter.limit("10/minute")
async def approve_review(
    request: Request,  # noqa: ARG001 — required by slowapi for rate-limit key resolution
    job_id: UUID,
) -> dict[str, str]:
    """Approve a job currently awaiting human review.

    Idempotency: a job that is not in :attr:`ConfidenceTier.REVIEW` will
    return 409 Conflict instead of being silently re-approved. A
    non-existent job returns 404.

    On success the job transitions to :attr:`JobStatus.COMPLETED`. The
    ``JobStatus`` enum has no dedicated ``APPROVED`` member, so
    ``COMPLETED`` is the canonical post-review terminal state.
    """
    async with get_job_repository() as repo:
        job = await repo.get_job(job_id)  # type: ignore[arg-type]
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"job {job_id} not found",
            )
        if job.confidence_tier != ConfidenceTier.REVIEW:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"job {job_id} is not in REVIEW tier "
                    f"(current tier: "
                    f"{job.confidence_tier.value if job.confidence_tier else 'none'})"
                ),
            )
        await repo.update_job_status(  # type: ignore[call-arg]
            job_id,  # type: ignore[arg-type]
            status=JobStatus.COMPLETED,
        )

    return {"job_id": str(job_id), "status": "approved"}


@router.post(
    "/review/{job_id}/reject",
    status_code=status.HTTP_200_OK,
    summary="Reject a REVIEW-tier job (transitions to REJECTED)",
)
@limiter.limit("10/minute")
async def reject_review(
    request: Request,  # noqa: ARG001 — required by slowapi for rate-limit key resolution
    job_id: UUID,
    body: RejectBody | None = None,
) -> dict[str, str]:
    """Reject a job currently awaiting human review.

    Idempotency: a job that is not in :attr:`ConfidenceTier.REVIEW` will
    return 409 Conflict. A non-existent job returns 404. The optional
    ``reason`` field on the request body is persisted as the job's
    ``error_message`` so reviewers can audit the decision later.
    """
    reason = body.reason if body is not None else ""

    async with get_job_repository() as repo:
        job = await repo.get_job(job_id)  # type: ignore[arg-type]
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"job {job_id} not found",
            )
        if job.confidence_tier != ConfidenceTier.REVIEW:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"job {job_id} is not in REVIEW tier "
                    f"(current tier: "
                    f"{job.confidence_tier.value if job.confidence_tier else 'none'})"
                ),
            )
        await repo.update_job_status(  # type: ignore[call-arg]
            job_id,  # type: ignore[arg-type]
            status=JobStatus.REJECTED,
            error_message=reason or None,
        )

    return {"job_id": str(job_id), "status": "rejected"}


__all__ = [
    "RejectBody",
    "ReviewItem",
    "ReviewPendingResponse",
    "approve_review",
    "list_pending_reviews",
    "reject_review",
    "router",
]
