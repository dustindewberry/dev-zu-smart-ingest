"""POST /extract endpoint (CAP-009).

Accepts a multipart/form-data submission containing one or more PDF files
plus optional batch parameters, hands the request off to
:class:`JobService.submit_batch`, and returns ``202 Accepted`` with the
resulting :class:`BatchSubmissionResult` as JSON.

The route body is intentionally thin:

1. Translate the incoming :class:`fastapi.UploadFile` instances into the
   layer-agnostic :class:`UploadedFile` DTO from ``shared.types``.
2. Translate form fields into a :class:`SubmissionParams` DTO, validating
   the ``mode`` string against the :class:`ExtractionMode` enum and
   coercing the optional ``deployment_id`` / ``node_id`` strings to ints.
3. Call :meth:`JobService.submit_batch` and serialise the result.
4. Translate :class:`InvalidPDFError` into HTTP 400 and
   :class:`OversizeFileError` into HTTP 413.

Concrete adapter wiring (repository, task queue, PDF processor) is
performed by the :func:`get_job_service` FastAPI dependency factory at
the bottom of this module. Tests can override it via
``app.dependency_overrides`` to inject a fake service.

Per CAP-030, this expensive write endpoint is rate-limited to 20/minute
via the ``@limiter.limit("20/minute")`` decorator.
"""

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse

from zubot_ingestion.api.middleware.auth import get_auth_context
from zubot_ingestion.api.middleware.rate_limit import limiter
from zubot_ingestion.domain.enums import ExtractionMode
from zubot_ingestion.domain.protocols import IJobService
from zubot_ingestion.services.job_service import (
    InvalidPDFError,
    JobService,
    OversizeFileError,
)
from zubot_ingestion.shared.types import (
    AuthContext,
    BatchSubmissionResult,
    SubmissionParams,
    UploadedFile,
)

router = APIRouter(tags=["extract"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_mode(mode: str) -> ExtractionMode:
    """Coerce a string form field to an :class:`ExtractionMode`.

    Raises HTTP 400 for values outside the controlled vocabulary.
    """
    try:
        return ExtractionMode(mode)
    except ValueError as exc:
        valid = ", ".join(m.value for m in ExtractionMode)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid mode '{mode}'. Must be one of: {valid}",
        ) from exc


def _parse_int_or_none(value: str | None) -> int | None:
    """Coerce an optional string form field to ``int | None``.

    Empty strings and ``None`` become ``None``. Non-numeric strings raise
    HTTP 400.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Expected integer, got: {value!r}",
        ) from exc


async def _to_uploaded_file(upload: UploadFile) -> UploadedFile:
    """Read an :class:`UploadFile` fully into memory.

    The service layer operates on raw ``bytes`` rather than async file
    objects so it can hash and validate in a single pass. Batches are
    size-bounded (max 100 MB per file by default) so reading upfront is
    acceptable; oversize protection is the service's job, not the
    framework's.
    """
    content = await upload.read()
    return UploadedFile(
        filename=upload.filename or "unnamed.pdf",
        content=content,
        content_type=upload.content_type or "application/octet-stream",
    )


def _serialize_result(result: BatchSubmissionResult) -> dict[str, Any]:
    """Convert the frozen :class:`BatchSubmissionResult` to a JSON dict.

    Branded ``NewType`` aliases (``BatchId``, ``JobId``, ``FileHash``) are
    runtime-transparent so we cast to ``str`` when rendering. Enums are
    rendered using their ``value`` attribute.
    """
    return {
        "batch_id": str(result.batch_id),
        "total": result.total,
        "poll_url": result.poll_url,
        "jobs": [
            {
                "job_id": str(j.job_id),
                "filename": j.filename,
                "status": j.status.value,
                "file_hash": str(j.file_hash),
                "result": j.result,
            }
            for j in result.jobs
        ],
    }


# ---------------------------------------------------------------------------
# DI factory
# ---------------------------------------------------------------------------


async def get_job_service(request: Request) -> IJobService:
    """FastAPI dependency that constructs a :class:`JobService`.

    The factory is split into a thin body so tests can override it via
    ``app.dependency_overrides[get_job_service]`` without needing to spin
    up real Postgres, Celery, or Ollama.

    At production runtime, the factory lazily wires:

    - :class:`JobRepository` bound to a fresh async DB session
    - :class:`CeleryTaskQueue` that pushes onto the default queue
    - :class:`PyMuPDFProcessor` for PDF loading and page rendering

    NOTE: the async session yielded by ``get_session()`` needs to stay
    open for the duration of the request so that the repository can
    commit. FastAPI's dependency injection closes generators after the
    request completes, so this factory intentionally pairs the session
    lifetime to the FastAPI request scope via ``yield``.
    """
    # Local imports prevent the API module from taking a hard dependency
    # on infrastructure at import time; this keeps the import graph clean
    # for the lint-import rules (api → infrastructure is forbidden except
    # at DI wiring sites).
    from zubot_ingestion.infrastructure.database.repository import JobRepository
    from zubot_ingestion.infrastructure.database.session import get_session
    from zubot_ingestion.infrastructure.pdf.processor import PyMuPDFProcessor
    from zubot_ingestion.services.task_queue import CeleryTaskQueue

    async with get_session() as session:
        repository = JobRepository(session)
        task_queue = CeleryTaskQueue()
        pdf_processor = PyMuPDFProcessor()
        yield JobService(
            repository=repository,
            task_queue=task_queue,
            pdf_processor=pdf_processor,
        )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post(
    "/extract",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a batch of PDFs for extraction",
    response_description="Batch queued for extraction",
)
@limiter.limit("20/minute")
async def submit_extract_batch(
    request: Request,  # noqa: ARG001 — required by slowapi for rate-limit key resolution
    files: list[UploadFile] = File(..., description="One or more PDF files to extract"),
    mode: str = Form("auto", description="Extraction mode: auto | drawing | title"),
    callback_url: str | None = Form(None, description="Optional webhook URL"),
    deployment_id: str | None = Form(None, description="Optional deployment id"),
    node_id: str | None = Form(None, description="Optional node id"),
    auth_context: AuthContext = Depends(get_auth_context),
    job_service: IJobService = Depends(get_job_service),
) -> JSONResponse:
    """Handle POST /extract.

    Returns a JSON ``BatchSubmissionResult`` with HTTP 202. Error
    translation:

    * :class:`InvalidPDFError` → HTTP 400
    * :class:`OversizeFileError` → HTTP 413
    * Invalid ``mode`` / non-numeric ids → HTTP 400 (raised by the helpers)
    """
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one file is required",
        )

    params = SubmissionParams(
        mode=_parse_mode(mode),
        callback_url=callback_url,
        deployment_id=_parse_int_or_none(deployment_id),
        node_id=_parse_int_or_none(node_id),
    )

    uploaded_files = [await _to_uploaded_file(f) for f in files]

    try:
        result = await job_service.submit_batch(
            files=uploaded_files,
            params=params,
            auth_context=auth_context,
        )
    except OversizeFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except InvalidPDFError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=_serialize_result(result),
    )


__all__ = ["get_job_service", "router", "submit_extract_batch"]
