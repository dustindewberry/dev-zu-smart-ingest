"""Transitional minimal stub of zubot_ingestion.domain.entities.

Contains ONLY the entities required by this worker's task (step-15
sidecar-builder). The canonical, full-featured entities module is produced
by task-2 (bright-pike worktree) and is a strict superset of this stub.

On merge, the canonical module from task-2 MUST overwrite this stub. The
class shapes here mirror the canonical bright-pike entities byte-for-byte
for the fields this stub touches, so behavior is unchanged either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from zubot_ingestion.domain.enums import (
    ConfidenceTier,
    Discipline,
    DocumentType,
    ExtractionMode,
    JobStatus,
)
from zubot_ingestion.shared.types import BatchId, FileHash, JobId


@dataclass(frozen=True)
class Job:
    """A single-file extraction job persisted in the job repository."""

    job_id: JobId
    batch_id: BatchId
    filename: str
    file_hash: FileHash
    file_path: str
    status: JobStatus
    result: dict[str, Any] | None
    error_message: str | None
    pipeline_trace: dict[str, Any] | None
    otel_trace_id: str | None
    processing_time_ms: int | None
    created_at: datetime
    updated_at: datetime
    confidence_tier: ConfidenceTier | None = None
    overall_confidence: float | None = None


@dataclass(frozen=True)
class ExtractionResult:
    """Result of Stage 1 multi-source extraction."""

    drawing_number: str | None
    drawing_number_confidence: float
    title: str | None
    title_confidence: float
    document_type: DocumentType | None
    document_type_confidence: float
    discipline: Discipline | None = None
    revision: str | None = None
    building_zone: str | None = None
    project: str | None = None
    sources_used: list[str] = field(default_factory=list)
    raw_vision_response: str | None = None
    raw_text_response: str | None = None


@dataclass(frozen=True)
class CompanionResult:
    """Result of Stage 2 companion generation."""

    companion_text: str
    pages_described: int
    companion_generated: bool
    validation_passed: bool
    quality_score: float | None = None


@dataclass(frozen=True)
class SidecarDocument:
    """A Bedrock KB sidecar document produced by ISidecarBuilder.build()."""

    metadata_attributes: dict[str, Any]
    companion_text: str | None
    source_filename: str
    file_hash: FileHash
    schema_version: str = "1.0"


__all__ = [
    "CompanionResult",
    "ExtractionResult",
    "Job",
    "SidecarDocument",
]
