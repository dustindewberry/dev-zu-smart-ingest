"""Domain entities for the Zubot Smart Ingestion Service.

Entities model the core domain concepts and are the values that flow through
the extraction pipeline and across the application layer. They are defined as
frozen dataclasses and have no infrastructure dependencies.

Per the dependency rules (boundary-contracts.md §3), this module may only
import from stdlib, `zubot_ingestion.shared.types`, and
`zubot_ingestion.domain.enums`. It MUST NOT import from api, services, or
infrastructure.
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

# ---------------------------------------------------------------------------
# Persistence entities (rows in the job repository)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Batch:
    """A batch of jobs submitted together via POST /extract."""

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
class ReviewAction:
    """An audit record for a human review decision (approve or reject)."""

    review_action_id: str
    job_id: JobId
    action: str  # "approve" | "reject"
    reviewed_by: str
    reason: str | None
    corrections: dict[str, Any] | None
    created_at: datetime


# ---------------------------------------------------------------------------
# PDF processing entities (produced by IPDFProcessor)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PDFData:
    """Basic data extracted from a PDF during IPDFProcessor.load()."""

    page_count: int
    file_hash: FileHash
    metadata: dict[str, Any]
    is_encrypted: bool = False
    has_text_layer: bool = True


@dataclass(frozen=True)
class RenderedPage:
    """A single rendered PDF page as a JPEG image."""

    page_number: int
    jpeg_bytes: bytes
    base64_jpeg: str
    width_px: int
    height_px: int
    dpi: int
    render_time_ms: int


# ---------------------------------------------------------------------------
# Ollama response entity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OllamaResponse:
    """Response returned by IOllamaClient.generate_vision/generate_text()."""

    response_text: str
    model: str
    prompt_eval_count: int | None
    eval_count: int | None
    total_duration_ns: int | None
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Filename parsing entity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilenameHints:
    """Hints extracted from a filename by IFilenameParser.parse()."""

    drawing_number_hint: str | None
    revision_hint: str | None
    confidence: float
    matched_pattern: str | None


# ---------------------------------------------------------------------------
# Stage 1: extraction result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionResult:
    """Result of Stage 1 multi-source extraction (IExtractor.extract)."""

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


# ---------------------------------------------------------------------------
# Stage 2: companion result + validation result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompanionResult:
    """Result of Stage 2 companion generation (ICompanionGenerator.generate)."""

    companion_text: str
    pages_described: int
    companion_generated: bool
    validation_passed: bool
    quality_score: float | None = None


@dataclass(frozen=True)
class ValidationResult:
    """Result of cross-checking a companion against extraction metadata.

    The ``validation_passed``/``quality_score``/``issues`` fields are the
    canonical rule-based-validator shape produced by
    :class:`zubot_ingestion.domain.pipeline.validation.CompanionValidator`.
    The legacy ``passed``/``warnings``/``confidence_adjustment`` fields are
    retained as aliases so existing consumers (notably
    :class:`zubot_ingestion.domain.pipeline.confidence.ConfidenceCalculator`)
    continue to work unchanged.
    """

    passed: bool
    warnings: list[str] = field(default_factory=list)
    confidence_adjustment: float = 0.0
    validation_passed: bool | None = None
    quality_score: float = 1.0
    issues: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Keep the legacy and canonical boolean fields in sync. When a caller
        # builds the result using the legacy ``passed`` kwarg only, mirror it
        # into ``validation_passed``; when only ``validation_passed`` is set
        # (via the new CompanionValidator), mirror it back into ``passed``.
        if self.validation_passed is None:
            object.__setattr__(self, "validation_passed", self.passed)


# ---------------------------------------------------------------------------
# Stage 3: sidecar document
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SidecarDocument:
    """A Bedrock KB sidecar document produced by ISidecarBuilder.build()."""

    metadata_attributes: dict[str, Any]
    companion_text: str | None
    source_filename: str
    file_hash: FileHash
    schema_version: str = "1.0"


# ---------------------------------------------------------------------------
# Confidence assessment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidenceAssessment:
    """Overall confidence score and tier assigned by IConfidenceCalculator."""

    overall_confidence: float
    tier: ConfidenceTier
    breakdown: dict[str, float] = field(default_factory=dict)
    validation_adjustment: float = 0.0


# ---------------------------------------------------------------------------
# Pipeline context (shared state passed between stages)
# ---------------------------------------------------------------------------


@dataclass
class PipelineContext:
    """Mutable context passed between extraction pipeline stages.

    Unlike the other domain entities this is intentionally mutable so stages
    can attach intermediate results (rendered pages, extracted text, model
    responses) without needing to rebuild the entire value.
    """

    job: Job
    pdf_bytes: bytes
    pdf_data: PDFData | None = None
    extracted_text: str | None = None
    rendered_pages: list[RenderedPage] = field(default_factory=list)
    filename_hints: FilenameHints | None = None
    errors: list[PipelineError] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pipeline result (returned by IOrchestrator.run_pipeline)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineResult:
    """Final result of an extraction pipeline run.

    Defined here (rather than in shared/types.py) because it references domain
    entities; shared/types.py must not import from domain/entities.py to avoid
    circular imports.
    """

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
    "OllamaResponse",
    "PDFData",
    "PipelineContext",
    "PipelineResult",
    "RenderedPage",
    "ReviewAction",
    "SidecarDocument",
    "ValidationResult",
]
