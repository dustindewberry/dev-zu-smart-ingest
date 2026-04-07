"""Domain entities (transitional stub).

Only entities required by the database repository module are defined here.
Canonical entities are produced by task-2 (contracts-and-types-definition);
on merge, the canonical versions must overwrite this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from zubot_ingestion.domain.enums import (
    BatchStatus,
    ConfidenceTier,
    ExtractionMode,
    JobStatus,
    ReviewActionType,
)


@dataclass
class Batch:
    id: UUID
    deployment_id: str | None
    node_id: str | None
    callback_url: str | None
    mode: ExtractionMode
    created_by: str
    total_jobs: int
    status: BatchStatus
    created_at: datetime
    updated_at: datetime


@dataclass
class Job:
    id: UUID
    batch_id: UUID
    filename: str
    file_hash: str
    file_size: int
    status: JobStatus
    mode: ExtractionMode
    result: dict[str, Any] | None = None
    confidence_tier: ConfidenceTier | None = None
    confidence_score: float | None = None
    processing_time_ms: int | None = None
    otel_trace_id: str | None = None
    error_message: str | None = None
    pipeline_trace: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass
class ReviewAction:
    id: UUID
    job_id: UUID
    action: ReviewActionType
    corrections: dict[str, Any] | None
    reviewed_by: str
    reason: str | None
    created_at: datetime


@dataclass
class BatchWithJobs:
    batch: Batch
    jobs: list[Job] = field(default_factory=list)


@dataclass
class JobDetail:
    job: Job
    batch: Batch | None = None
