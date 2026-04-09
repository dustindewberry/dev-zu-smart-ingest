"""GET /batches/{batch_id} endpoint (CAP-010).

Looks up a batch by id and returns its current aggregated status,
progress counters, and the per-job summary list. The route delegates
all real work to :meth:`JobService.get_batch` and merely:

1. Translates the path parameter into a :class:`uuid.UUID`. Malformed
   ids return HTTP 400 immediately rather than reaching the service.
2. Translates :class:`NotFoundError` into HTTP 404.
3. Serialises the :class:`BatchWithJobs` DTO into a JSON dict that
   includes the ``in_progress`` progress counter required by the
   CAP-010 contract.

The route is registered under the ``batches`` OpenAPI tag and uses the
shared :func:`get_auth_context` and :func:`get_job_service` dependencies
from sibling modules so the test suite can override them via
``app.dependency_overrides`` exactly the same way it does for
``POST /extract``.

Per CAP-030, this read endpoint is rate-limited to 100/minute via the
``@limiter.limit("100/minute")`` decorator.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from zubot_ingestion.api.middleware.auth import get_auth_context
from zubot_ingestion.api.middleware.rate_limit import limiter
from zubot_ingestion.api.routes.extract import get_job_service
from zubot_ingestion.domain.protocols import IJobService
from zubot_ingestion.services.job_service import NotFoundError
from zubot_ingestion.shared.types import AuthContext, BatchWithJobs

router = APIRouter(tags=["batches"])


def _parse_batch_id(batch_id: str) -> UUID:
    """Coerce a string path parameter to ``UUID``.

    Raises HTTP 400 for malformed UUIDs so callers receive a structured
    error rather than the framework's default 422.
    """
    try:
        return UUID(batch_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid batch_id: {batch_id!r}",
        ) from exc


def _serialize_batch(batch: BatchWithJobs) -> dict[str, Any]:
    """Convert a :class:`BatchWithJobs` to a JSON-friendly dict.

    The progress object includes the ``in_progress`` counter required
    by CAP-010. Branded NewType ids are cast to ``str`` and enums to
    their ``value`` attribute.
    """
    return {
        "batch_id": str(batch.batch_id),
        "status": batch.status.value,
        "progress": {
            "completed": batch.progress.completed,
            "queued": batch.progress.queued,
            "failed": batch.progress.failed,
            "in_progress": batch.progress.in_progress,
            "total": batch.progress.total,
        },
        "callback_url": batch.callback_url,
        "deployment_id": batch.deployment_id,
        "node_id": batch.node_id,
        "created_at": batch.created_at.isoformat(),
        "updated_at": batch.updated_at.isoformat(),
        "jobs": [
            {
                "job_id": str(j.job_id),
                "filename": j.filename,
                "status": j.status.value,
                "file_hash": str(j.file_hash),
                "result": j.result,
            }
            for j in batch.jobs
        ],
    }


@router.get(
    "/batches/{batch_id}",
    summary="Get a batch with its jobs and aggregated progress",
    response_description="Batch with progress and job list",
)
@limiter.limit("100/minute")
async def get_batch_endpoint(
    request: Request,  # noqa: ARG001 — required by slowapi for rate-limit key resolution
    batch_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    service: IJobService = Depends(get_job_service),
) -> JSONResponse:
    """Handle GET /batches/{batch_id}.

    Returns a JSON representation of the :class:`BatchWithJobs` DTO,
    or HTTP 404 if no such batch exists.

    The return type is :class:`fastapi.responses.JSONResponse` (not a
    plain ``dict``) so slowapi's ``_inject_headers`` hook can attach
    rate-limit headers — it rejects non-Response return types.
    """
    parsed_id = _parse_batch_id(batch_id)
    try:
        result = await service.get_batch(parsed_id, auth_context)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc) or f"Batch {batch_id} not found",
        ) from exc

    # Defensive: a stale adapter that pre-dates CAP-010 may still return
    # ``None`` instead of raising. Treat that as a 404 too.
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch {batch_id} not found",
        )

    return JSONResponse(content=_serialize_batch(result))


__all__ = ["get_batch_endpoint", "router"]
