"""Human review queue endpoints (CAP-022, step-20).

Three endpoints for reviewers to triage jobs whose confidence tier landed
in ``review`` (i.e. below the auto / spot thresholds):

* ``GET  /review/pending``         — paginated list of pending-review jobs
* ``POST /review/{job_id}/approve`` — mark a review job COMPLETED with reviewer metadata
* ``POST /review/{job_id}/reject``  — mark a review job FAILED with a rejection reason

Design notes
------------

* **Repository acquisition.** All three endpoints use the
  ``async with get_job_repository() as repo:`` pattern exported from
  :mod:`zubot_ingestion.services`. That factory is an
  ``@asynccontextmanager`` that yields a :class:`JobRepository` bound to a
  freshly-opened :class:`AsyncSession` for the duration of the request.
  The sync ``repo = get_job_repository()`` form does not work and MUST
  NOT be reintroduced.

* **Pagination mapping.** Callers pass ``limit`` and ``offset`` query
  params (the idiomatic HTTP pagination shape), but the canonical
  :class:`IJobRepository.get_pending_reviews` protocol method takes
  ``page`` and ``per_page``. The route converts between the two so the
  HTTP contract stays ergonomic without breaking the protocol.

* **State transitions.** Approve flips ``status`` to
  :attr:`JobStatus.COMPLETED` and stamps reviewer metadata
  (``reviewer_id``, ``notes``, ``reviewed_at``) into the ``result`` JSONB
  blob. Reject flips ``status`` to :attr:`JobStatus.FAILED` and writes a
  rejection reason into ``error_message``. Both guard-rail on the
  ``confidence_tier == review`` invariant so the endpoints cannot be
  used to force-fail / force-complete an auto-tier job.

* **Rate limits.** Per CAP-030:
  - ``GET /review/pending`` → ``100/minute`` (read endpoint).
  - ``POST /review/{job_id}/approve`` → ``50/minute`` (mutation).
  - ``POST /review/{job_id}/reject`` → ``50/minute`` (mutation).

  Each decorated handler accepts a ``request: Request`` parameter so
  slowapi's ``key_func`` can resolve the per-caller bucket; the parameter
  is unused in the handler body and is annotated with
  ``# noqa: ARG001`` accordingly.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from zubot_ingestion.api.middleware.rate_limit import limiter
from zubot_ingestion.domain.enums import ConfidenceTier, JobStatus
from zubot_ingestion.services import get_job_repository

router = APIRouter(tags=["review"])


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ApproveRequest(BaseModel):
    """Body for ``POST /review/{job_id}/approve``."""

    reviewer_id: str = Field(..., min_length=1, description="Identifier of the reviewer approving the job")
    notes: str | None = Field(None, description="Optional free-text reviewer notes")


class RejectRequest(BaseModel):
    """Body for ``POST /review/{job_id}/reject``."""

    reviewer_id: str = Field(..., min_length=1, description="Identifier of the reviewer rejecting the job")
    reason: str = Field(..., min_length=1, description="Human-readable rejection reason")


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _extract_field(result: dict[str, Any] | None, key: str) -> Any:
    """Return ``result[key]`` if the result blob has one, else ``None``.

    The orchestrator stores the final extraction payload as a nested dict
    keyed by field name. For the review-queue list endpoint we only need
    a handful of top-level summary fields so reviewers can triage at a
    glance without loading the full extraction result.
    """
    if not isinstance(result, dict):
        return None
    value = result.get(key)
    # Some orchestrator versions wrap each field in {"value": ..., "confidence": ...}.
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _serialize_job_summary(job: Any) -> dict[str, Any]:
    """Convert a :class:`Job` entity to the minimal list-item dict.

    Used by the paginated ``/review/pending`` response. Mirrors the
    fields described in the task spec: ``job_id``, ``drawing_number``,
    ``title``, ``confidence_tier``, ``confidence_score``, ``created_at``.
    """
    return {
        "job_id": str(job.job_id),
        "drawing_number": _extract_field(job.result, "drawing_number"),
        "title": _extract_field(job.result, "title"),
        "confidence_tier": (
            job.confidence_tier.value if job.confidence_tier is not None else None
        ),
        "confidence_score": (
            float(job.overall_confidence)
            if job.overall_confidence is not None
            and not math.isnan(job.overall_confidence)
            else None
        ),
        "created_at": job.created_at.isoformat() if job.created_at is not None else None,
    }


def _serialize_job_full(job: Any) -> dict[str, Any]:
    """Convert a :class:`Job` to the full JSON dict returned by approve / reject."""
    return {
        "job_id": str(job.job_id),
        "batch_id": str(job.batch_id),
        "filename": job.filename,
        "file_hash": str(job.file_hash),
        "status": job.status.value if hasattr(job.status, "value") else str(job.status),
        "result": job.result,
        "error_message": job.error_message,
        "pipeline_trace": job.pipeline_trace,
        "otel_trace_id": job.otel_trace_id,
        "processing_time_ms": job.processing_time_ms,
        "confidence_tier": (
            job.confidence_tier.value if job.confidence_tier is not None else None
        ),
        "confidence_score": (
            float(job.overall_confidence)
            if job.overall_confidence is not None
            and not math.isnan(job.overall_confidence)
            else None
        ),
        "created_at": job.created_at.isoformat() if job.created_at is not None else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at is not None else None,
    }


def _parse_job_id(job_id: str) -> UUID:
    """Coerce a path-parameter job id string to ``UUID``.

    Returns HTTP 400 (rather than the framework's default 422) on a
    malformed id so the client receives a structured error.
    """
    try:
        return UUID(job_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid job_id: {job_id!r}",
        ) from exc


# ---------------------------------------------------------------------------
# GET /review/pending
# ---------------------------------------------------------------------------


@router.get(
    "/review/pending",
    summary="List jobs awaiting human review",
    response_description="Paginated list of review-tier jobs",
)
@limiter.limit("100/minute")
async def get_review_pending(
    request: Request,  # noqa: ARG001 — required by slowapi for rate-limit key resolution
    limit: int = Query(50, ge=1, le=500, description="Max items to return"),
    offset: int = Query(0, ge=0, description="Items to skip before returning results"),
) -> JSONResponse:
    """Return a paginated slice of jobs whose ``confidence_tier == review``.

    The repository protocol uses ``(page, per_page)`` pagination, so this
    handler converts ``(limit, offset)`` into the equivalent page number
    before calling ``repo.get_pending_reviews``.
    """
    per_page = max(1, limit)
    # ``offset // per_page`` maps offset 0 → page 1, offset per_page → page 2, ...
    page = (offset // per_page) + 1

    async with get_job_repository() as repo:
        paginated = await repo.get_pending_reviews(page=page, per_page=per_page)

    items = [_serialize_job_summary(j) for j in paginated.items]
    return JSONResponse(
        content={
            "items": items,
            "limit": limit,
            "offset": offset,
            "total": paginated.total,
        }
    )


# ---------------------------------------------------------------------------
# POST /review/{job_id}/approve
# ---------------------------------------------------------------------------


def _ensure_review_tier(job: Any, job_id: str) -> None:
    """Raise HTTP 409 if the job is not in the review tier.

    The guard runs after a successful ``get_job`` lookup so the 404 path
    has already been handled. We compare against
    :attr:`ConfidenceTier.REVIEW` so the check is robust whether the row
    stores the string ``"review"`` or the enum instance.
    """
    if job.confidence_tier != ConfidenceTier.REVIEW:
        actual = (
            job.confidence_tier.value
            if job.confidence_tier is not None
            else "none"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Job {job_id} is not in the review tier "
                f"(confidence_tier={actual}); only review-tier jobs may be "
                "approved or rejected"
            ),
        )


@router.post(
    "/review/{job_id}/approve",
    summary="Approve a review-tier job and transition it to completed",
    response_description="Updated job record",
)
@limiter.limit("50/minute")
async def approve_review(
    request: Request,  # noqa: ARG001 — required by slowapi for rate-limit key resolution
    job_id: str,
    body: ApproveRequest,
) -> JSONResponse:
    """Mark a review-tier job as ``COMPLETED`` with reviewer metadata.

    The reviewer's identifier, optional notes, and timestamp are merged
    into the existing ``result`` JSONB blob under a ``review`` sub-key so
    downstream consumers can see both the extraction output and the
    reviewer's decision audit trail in a single document.
    """
    parsed_id = _parse_job_id(job_id)

    async with get_job_repository() as repo:
        job = await repo.get_job(parsed_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job {job_id} not found",
            )
        _ensure_review_tier(job, job_id)

        # Merge reviewer metadata into the result blob without clobbering
        # the orchestrator's extraction output.
        existing_result = dict(job.result) if isinstance(job.result, dict) else {}
        existing_result["review"] = {
            "action": "approved",
            "reviewer_id": body.reviewer_id,
            "notes": body.notes,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }

        await repo.update_job_status(
            job_id=parsed_id,
            status=JobStatus.COMPLETED,
            result=existing_result,
        )
        updated = await repo.get_job(parsed_id)

    if updated is None:  # pragma: no cover - defensive, update_job_status should not delete
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found after update",
        )
    return JSONResponse(content=_serialize_job_full(updated))


# ---------------------------------------------------------------------------
# POST /review/{job_id}/reject
# ---------------------------------------------------------------------------


@router.post(
    "/review/{job_id}/reject",
    summary="Reject a review-tier job and transition it to failed",
    response_description="Updated job record",
)
@limiter.limit("50/minute")
async def reject_review(
    request: Request,  # noqa: ARG001 — required by slowapi for rate-limit key resolution
    job_id: str,
    body: RejectRequest,
) -> JSONResponse:
    """Mark a review-tier job as ``FAILED`` with a reviewer rejection reason.

    The ``error_message`` column is overwritten with a human-readable
    string of the form ``"Rejected by {reviewer_id}: {reason}"`` so the
    rejection reason is visible on every job-detail GET without needing
    to load the review-action audit table.
    """
    parsed_id = _parse_job_id(job_id)

    async with get_job_repository() as repo:
        job = await repo.get_job(parsed_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job {job_id} not found",
            )
        _ensure_review_tier(job, job_id)

        error_message = f"Rejected by {body.reviewer_id}: {body.reason}"
        await repo.update_job_status(
            job_id=parsed_id,
            status=JobStatus.FAILED,
            error_message=error_message,
        )
        updated = await repo.get_job(parsed_id)

    if updated is None:  # pragma: no cover - defensive, update_job_status should not delete
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found after update",
        )
    return JSONResponse(content=_serialize_job_full(updated))


__all__ = [
    "ApproveRequest",
    "RejectRequest",
    "approve_review",
    "get_review_pending",
    "reject_review",
    "router",
]
