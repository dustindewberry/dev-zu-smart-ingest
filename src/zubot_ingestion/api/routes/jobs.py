"""GET /jobs/{job_id} endpoint (CAP-011).

Looks up a single job by id and returns the full :class:`JobDetail`
record (including ``result``, ``pipeline_trace``, ``otel_trace_id``,
and ``processing_time_ms``) as JSON.

The route delegates all real work to :meth:`JobService.get_job` and:

1. Translates the path parameter into a :class:`uuid.UUID`. Malformed
   ids return HTTP 400 immediately rather than reaching the service.
2. Translates :class:`NotFoundError` into HTTP 404.
3. Serialises the :class:`JobDetail` DTO into a JSON dict.

The route is registered under the ``jobs`` OpenAPI tag and uses the
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
from zubot_ingestion.shared.types import AuthContext, JobDetail

router = APIRouter(tags=["jobs"])


def _parse_job_id(job_id: str) -> UUID:
    """Coerce a string path parameter to ``UUID``.

    Raises HTTP 400 for malformed UUIDs so callers receive a structured
    error rather than the framework's default 422.
    """
    try:
        return UUID(job_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid job_id: {job_id!r}",
        ) from exc


def _serialize_job(job: JobDetail) -> dict[str, Any]:
    """Convert a :class:`JobDetail` to a JSON-friendly dict.

    Branded NewType ids are cast to ``str`` and enums to their ``value``
    attribute. The ``result``, ``pipeline_trace``, and timing fields are
    forwarded as-is so callers can reconstruct the full extraction trace
    in a single round trip.
    """
    return {
        "job_id": str(job.job_id),
        "batch_id": str(job.batch_id),
        "filename": job.filename,
        "file_hash": str(job.file_hash),
        "status": job.status.value,
        "result": job.result,
        "error_message": job.error_message,
        "pipeline_trace": job.pipeline_trace,
        "otel_trace_id": job.otel_trace_id,
        "processing_time_ms": job.processing_time_ms,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


@router.get(
    "/jobs/{job_id}",
    summary="Get a single job by id",
    response_description="Full job detail with extraction result and trace",
)
@limiter.limit("100/minute")
async def get_job_endpoint(
    request: Request,  # noqa: ARG001 — required by slowapi for rate-limit key resolution
    job_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    service: IJobService = Depends(get_job_service),
) -> JSONResponse:
    """Handle GET /jobs/{job_id}.

    Returns a JSON representation of the :class:`JobDetail` DTO, or
    HTTP 404 if no such job exists.

    The return type is :class:`fastapi.responses.JSONResponse` (not a
    plain ``dict``) so slowapi's ``_inject_headers`` hook can attach
    rate-limit headers — it rejects non-Response return types.
    """
    parsed_id = _parse_job_id(job_id)
    try:
        result = await service.get_job(parsed_id, auth_context)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc) or f"Job {job_id} not found",
        ) from exc

    # Defensive: a stale adapter that pre-dates CAP-011 may still return
    # ``None`` instead of raising. Treat that as a 404 too.
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found",
        )

    return JSONResponse(content=_serialize_job(result))


__all__ = ["get_job_endpoint", "router"]
