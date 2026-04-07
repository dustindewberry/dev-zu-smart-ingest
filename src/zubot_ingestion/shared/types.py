"""Shared types for cross-layer communication.

This module is the ONLY cross-layer type package. Every layer may import from
here, and new DTOs that cross boundaries must be defined here rather than in
layer-local modules.

All definitions follow boundary-contracts.md §5 (Shared Types Catalog) exactly.
Use frozen dataclasses so values are immutable once created.

Per the dependency rules, this module may only import from stdlib and
zubot_ingestion.domain.enums. It must NOT import from api, services, or
infrastructure layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, Literal, NewType, TypeVar
from uuid import UUID

from zubot_ingestion.domain.enums import ExtractionMode, JobStatus

# ---------------------------------------------------------------------------
# Branded type aliases
# ---------------------------------------------------------------------------

JobId = NewType("JobId", UUID)
BatchId = NewType("BatchId", UUID)
FileHash = NewType("FileHash", str)  # SHA-256 hex string


# ---------------------------------------------------------------------------
# Generic type variable for paginated results
# ---------------------------------------------------------------------------

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Auth + submission DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthContext:
    """Authenticated user context attached to every request by AuthMiddleware."""

    user_id: str
    auth_method: Literal["api_key", "wod_token"]
    deployment_id: int | None
    node_id: int | None
    permissions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SubmissionParams:
    """Parameters for a batch submission request."""

    mode: ExtractionMode = ExtractionMode.AUTO
    callback_url: str | None = None
    deployment_id: int | None = None
    node_id: int | None = None


@dataclass(frozen=True)
class UploadedFile:
    """An uploaded PDF file with raw bytes and filename."""

    filename: str
    content: bytes
    content_type: str


# ---------------------------------------------------------------------------
# Job summary + batch submission result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobSummary:
    """Summary of a job used in list responses."""

    job_id: JobId
    filename: str
    status: JobStatus
    file_hash: FileHash
    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class BatchSubmissionResult:
    """Result returned from IJobService.submit_batch()."""

    batch_id: BatchId
    jobs: list[JobSummary]
    total: int
    poll_url: str


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaginatedResult(Generic[T]):
    """Generic paginated list result."""

    items: list[T]
    total: int
    page: int
    per_page: int
    total_pages: int


# ---------------------------------------------------------------------------
# Extraction summary used by review queue items
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionSummary:
    """Summary of extraction results shown to human reviewers."""

    drawing_number: str | None
    drawing_number_confidence: float
    title: str | None
    title_confidence: float
    document_type: str | None
    document_type_confidence: float


# ---------------------------------------------------------------------------
# Review workflow DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewQueueItem:
    """An item in the human review queue."""

    job_id: JobId
    filename: str
    extracted: ExtractionSummary
    preview_url: str
    created_at: datetime


@dataclass(frozen=True)
class ReviewCorrections:
    """Corrections a reviewer may apply when approving a job."""

    drawing_number: str | None = None
    title: str | None = None
    document_type: str | None = None
    discipline: str | None = None
    revision: str | None = None
    building_zone: str | None = None


@dataclass(frozen=True)
class ReviewResult:
    """Result returned from IReviewService.approve() and .reject()."""

    job_id: JobId
    status: JobStatus
    result: dict[str, Any] | None
    reviewed_at: datetime
    reviewed_by: str


# ---------------------------------------------------------------------------
# Job detail + batch-with-jobs DTOs (exposed by IJobService)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobDetail:
    """Full job record returned by IJobService.get_job().

    Mirrors the stored Job row plus fields the API contract exposes.
    """

    job_id: JobId
    batch_id: BatchId
    filename: str
    file_hash: FileHash
    status: JobStatus
    result: dict[str, Any] | None
    error_message: str | None
    pipeline_trace: dict[str, Any] | None
    otel_trace_id: str | None
    processing_time_ms: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class BatchProgress:
    """Progress counters for a batch."""

    completed: int
    queued: int
    failed: int
    total: int


@dataclass(frozen=True)
class BatchWithJobs:
    """Batch retrieved together with all associated jobs."""

    batch_id: BatchId
    status: JobStatus
    progress: BatchProgress
    jobs: list[JobSummary]
    callback_url: str | None
    deployment_id: int | None
    node_id: int | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Pipeline DTOs (returned by IOrchestrator)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineError:
    """An error that occurred during a pipeline stage."""

    stage: str
    error_type: str
    message: str
    recoverable: bool


# NOTE: PipelineResult references domain entities (ExtractionResult,
# CompanionResult, SidecarDocument, ConfidenceAssessment). To avoid a circular
# import between shared.types and domain.entities, PipelineResult is defined
# in domain/entities.py per the project architecture. Re-export it here for
# consumers that import from shared.types.


# ---------------------------------------------------------------------------
# Celery task status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskStatus:
    """Status of a Celery task, returned by ITaskQueue.get_task_status()."""

    state: Literal["PENDING", "STARTED", "SUCCESS", "FAILURE", "RETRY"]
    result: Any | None = None
    traceback: str | None = None


# ---------------------------------------------------------------------------
# Rate limiter DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateLimitResult:
    """Result of a rate-limit check, returned by IRateLimiter.check_limit()."""

    allowed: bool
    remaining: int
    reset_at: datetime


# ---------------------------------------------------------------------------
# Health check DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthStatus:
    """Aggregated service health returned by the /health endpoint."""

    status: Literal["ok", "degraded", "unhealthy"]
    version: str
    services: dict[str, Literal["connected", "disconnected"]]
    worker_count: int
    queue_depth: int


# ---------------------------------------------------------------------------
# Standard error response
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorResponse:
    """Standard error payload returned by API error handlers."""

    code: str
    message: str
    details: dict[str, Any] | None = None
    trace_id: str | None = None


__all__ = [
    "AuthContext",
    "BatchId",
    "BatchProgress",
    "BatchSubmissionResult",
    "BatchWithJobs",
    "ErrorResponse",
    "ExtractionSummary",
    "FileHash",
    "HealthStatus",
    "JobDetail",
    "JobId",
    "JobSummary",
    "PaginatedResult",
    "PipelineError",
    "RateLimitResult",
    "ReviewCorrections",
    "ReviewQueueItem",
    "ReviewResult",
    "SubmissionParams",
    "T",
    "TaskStatus",
    "UploadedFile",
]
