"""Minimal stub of zubot_ingestion.domain.entities for the OTEL task.

TRANSITIONAL stub created by silver-falcon (task-22 / step-22). The canonical
full entities module is produced by task-2 (bright-pike worktree) AND
extended by task-16 (agile-falcon worktree) with confidence_score /
confidence_tier on ExtractionResult and pipeline_trace / otel_trace_id on
PipelineResult. On merge, the canonical task-2+task-16 entities.py MUST
overwrite this file.

The dataclass shapes here are byte-identical (for the fields touched) to the
canonical entities so behavior is unchanged after merge.
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
from zubot_ingestion.shared.types import (
    BatchId,
    FileHash,
    JobId,
    PipelineError,
)


@dataclass(frozen=True)
class Batch:
    batch_id: BatchId
    status: JobStatus
    total_jobs: int
    callback_url: str | None
    deployment_id: int | None
    node_id: int | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    mode: ExtractionMode = ExtractionMode.AUTO


@dataclass(frozen=True)
class Job:
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
class PDFData:
    page_count: int
    file_hash: FileHash
    metadata: dict[str, Any]
    is_encrypted: bool = False
    has_text_layer: bool = True


@dataclass(frozen=True)
class RenderedPage:
    page_number: int
    jpeg_bytes: bytes
    base64_jpeg: str
    width_px: int
    height_px: int
    dpi: int
    render_time_ms: int


@dataclass(frozen=True)
class FilenameHints:
    drawing_number_hint: str | None
    revision_hint: str | None
    confidence: float
    matched_pattern: str | None


@dataclass(frozen=True)
class ExtractionResult:
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
    confidence_score: float | None = None
    confidence_tier: ConfidenceTier | None = None


@dataclass(frozen=True)
class CompanionResult:
    companion_text: str
    pages_described: int
    companion_generated: bool
    validation_passed: bool
    quality_score: float | None = None


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    warnings: list[str] = field(default_factory=list)
    confidence_adjustment: float = 0.0


@dataclass(frozen=True)
class SidecarDocument:
    metadata_attributes: dict[str, Any]
    companion_text: str | None
    source_filename: str
    file_hash: FileHash
    schema_version: str = "1.0"


@dataclass(frozen=True)
class ConfidenceAssessment:
    overall_confidence: float
    tier: ConfidenceTier
    breakdown: dict[str, float] = field(default_factory=dict)
    validation_adjustment: float = 0.0


@dataclass
class PipelineContext:
    job: Job
    pdf_bytes: bytes
    pdf_data: PDFData | None = None
    extracted_text: str | None = None
    rendered_pages: list[RenderedPage] = field(default_factory=list)
    filename_hints: FilenameHints | None = None
    errors: list[PipelineError] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineResult:
    extraction_result: ExtractionResult
    companion_result: CompanionResult | None
    sidecar: SidecarDocument
    confidence_assessment: ConfidenceAssessment
    errors: list[PipelineError] = field(default_factory=list)
    pipeline_trace: dict[str, Any] = field(default_factory=dict)
    otel_trace_id: str | None = None


__all__ = [
    "Batch",
    "CompanionResult",
    "ConfidenceAssessment",
    "ExtractionResult",
    "FilenameHints",
    "Job",
    "PDFData",
    "PipelineContext",
    "PipelineResult",
    "RenderedPage",
    "SidecarDocument",
    "ValidationResult",
]
