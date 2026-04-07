"""Extraction orchestrator — implements IOrchestrator (CAP-020).

The :class:`ExtractionOrchestrator` is the application-layer coordinator
that drives a single :class:`Job` through the three-stage extraction
pipeline:

    Stage 1 — multi-source extraction (drawing number, title, document type)
    Stage 2 — companion description (CAP-017, owned by task-18)
    Stage 3 — sidecar assembly (CAP-019)

It then runs the :class:`IConfidenceCalculator` to derive the overall score
and tier, attaches the score and tier to a refreshed
:class:`ExtractionResult`, records a per-stage timing trace, and returns a
:class:`PipelineResult`.

The orchestrator is deliberately tolerant of stage-level failures:

* If Stage 1 raises (or any of the three concurrent extractors raise) the
  failure is captured into ``pipeline_trace['errors']`` and a
  best-effort empty :class:`ExtractionResult` is used so Stages 3 and the
  confidence calculator can still produce a downstream sidecar/assessment.
  This guarantees ``run_pipeline`` always returns a structurally valid
  :class:`PipelineResult` even on partial failure — callers (Celery task,
  review queue) can then inspect ``pipeline_trace`` and decide what to do.

* Stage 3 (sidecar build) and the confidence step are run inside their
  own try/except blocks for the same reason.

Layering: this module lives in the application (services) layer. It
imports only from :mod:`zubot_ingestion.shared`, :mod:`zubot_ingestion.domain`,
and stdlib. It MUST NOT import from infrastructure or api layers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any

from zubot_ingestion.domain.entities import (
    CompanionResult,
    ConfidenceAssessment,
    ExtractionResult,
    Job,
    PDFData,
    PipelineContext,
    PipelineResult,
    SidecarDocument,
)
from zubot_ingestion.domain.enums import ConfidenceTier
from zubot_ingestion.domain.protocols import (
    ICompanionGenerator,
    IConfidenceCalculator,
    IExtractor,
    IMetadataWriter,
    IOrchestrator,
    IPDFProcessor,
    ISidecarBuilder,
)
from zubot_ingestion.shared.types import PipelineError

__all__ = ["ExtractionOrchestrator"]

_LOG = logging.getLogger(__name__)


def _empty_extraction_result() -> ExtractionResult:
    """Return a zero-confidence :class:`ExtractionResult` placeholder.

    Used as the fallback value when Stage 1 fails entirely so downstream
    stages still have a structurally valid object to work with.
    """
    return ExtractionResult(
        drawing_number=None,
        drawing_number_confidence=0.0,
        title=None,
        title_confidence=0.0,
        document_type=None,
        document_type_confidence=0.0,
    )


def _merge_extraction_results(
    drawing: ExtractionResult,
    title: ExtractionResult,
    document_type: ExtractionResult,
) -> ExtractionResult:
    """Combine the per-extractor outputs into a single :class:`ExtractionResult`.

    Each Stage 1 extractor returns a partial ``ExtractionResult`` populated
    only for the field it owns. The orchestrator's job is to take the
    populated field from each one and assemble a single result. We also
    union the ``sources_used`` lists so the merged result accurately
    reflects every source that contributed.
    """
    sources: list[str] = []
    for partial in (drawing, title, document_type):
        for src in partial.sources_used:
            if src not in sources:
                sources.append(src)

    return ExtractionResult(
        drawing_number=drawing.drawing_number,
        drawing_number_confidence=drawing.drawing_number_confidence,
        title=title.title,
        title_confidence=title.title_confidence,
        document_type=document_type.document_type,
        document_type_confidence=document_type.document_type_confidence,
        discipline=(
            drawing.discipline
            or title.discipline
            or document_type.discipline
        ),
        revision=(
            drawing.revision or title.revision or document_type.revision
        ),
        building_zone=(
            drawing.building_zone
            or title.building_zone
            or document_type.building_zone
        ),
        project=(
            drawing.project or title.project or document_type.project
        ),
        sources_used=sources,
        raw_vision_response=(
            drawing.raw_vision_response
            or title.raw_vision_response
            or document_type.raw_vision_response
        ),
        raw_text_response=(
            drawing.raw_text_response
            or title.raw_text_response
            or document_type.raw_text_response
        ),
    )


class ExtractionOrchestrator(IOrchestrator):
    """Application-layer pipeline orchestrator (CAP-020)."""

    def __init__(
        self,
        drawing_number_extractor: IExtractor,
        title_extractor: IExtractor,
        document_type_extractor: IExtractor,
        sidecar_builder: ISidecarBuilder,
        confidence_calculator: IConfidenceCalculator,
        pdf_processor: IPDFProcessor,
        companion_generator: ICompanionGenerator,
        metadata_writer: IMetadataWriter,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._drawing_number_extractor = drawing_number_extractor
        self._title_extractor = title_extractor
        self._document_type_extractor = document_type_extractor
        self._sidecar_builder = sidecar_builder
        self._confidence_calculator = confidence_calculator
        self._pdf_processor = pdf_processor
        self._companion_generator = companion_generator
        self._metadata_writer = metadata_writer
        self._log = logger or _LOG

    async def run_pipeline(
        self,
        job: Job,
        pdf_bytes: bytes,
        *,
        deployment_id: int | None = None,
        node_id: int | None = None,
    ) -> PipelineResult:
        """Execute the full extraction pipeline for a single job.

        Args:
            job: The persisted :class:`Job` entity.
            pdf_bytes: Raw PDF file bytes loaded from temporary storage.

        Returns:
            A :class:`PipelineResult` containing the merged extraction
            result (with ``confidence_score`` and ``confidence_tier``
            populated), the assembled sidecar (or a best-effort empty one
            if Stage 3 failed), the confidence assessment, and a
            per-stage timing trace.
        """
        pipeline_trace: dict[str, Any] = {"stages": {}, "errors": []}
        errors: list[PipelineError] = []

        # ------------------------------------------------------------------
        # Load PDF (zero stage)
        # ------------------------------------------------------------------
        load_start = time.perf_counter()
        try:
            pdf_data: PDFData = self._pdf_processor.load(pdf_bytes)
            pipeline_trace["stages"]["pdf_load"] = {
                "duration_ms": _elapsed_ms(load_start),
                "ok": True,
            }
        except Exception as exc:  # noqa: BLE001 - we degrade gracefully
            self._log.exception("pdf_load_failed", extra={"job_id": str(job.job_id)})
            pipeline_trace["stages"]["pdf_load"] = {
                "duration_ms": _elapsed_ms(load_start),
                "ok": False,
                "error": _format_error(exc),
            }
            pipeline_trace["errors"].append(
                {"stage": "pdf_load", "error": _format_error(exc)}
            )
            errors.append(
                PipelineError(
                    stage="pdf_load",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=False,
                )
            )
            pdf_data = None  # type: ignore[assignment]

        context = PipelineContext(
            job=job,
            pdf_bytes=pdf_bytes,
            pdf_data=pdf_data,
        )

        # ------------------------------------------------------------------
        # Stage 1 — concurrent multi-source extractors
        # ------------------------------------------------------------------
        stage1_start = time.perf_counter()
        try:
            drawing_task = self._drawing_number_extractor.extract(context)
            title_task = self._title_extractor.extract(context)
            doctype_task = self._document_type_extractor.extract(context)

            results = await asyncio.gather(
                drawing_task,
                title_task,
                doctype_task,
                return_exceptions=True,
            )

            # Detect partial failures: gather() will return either an
            # ExtractionResult or an Exception per slot. Capture the
            # exceptions into the trace and substitute the empty fallback
            # for failed slots so the merge step still has three values.
            normalized: list[ExtractionResult] = []
            for label, result in zip(
                ("drawing_number", "title", "document_type"),
                results,
            ):
                if isinstance(result, Exception):
                    self._log.warning(
                        "stage1_extractor_failed",
                        extra={"extractor": label, "job_id": str(job.job_id)},
                    )
                    pipeline_trace["errors"].append(
                        {
                            "stage": f"stage1.{label}",
                            "error": _format_error(result),
                        }
                    )
                    errors.append(
                        PipelineError(
                            stage=f"stage1.{label}",
                            error_type=type(result).__name__,
                            message=str(result),
                            recoverable=True,
                        )
                    )
                    normalized.append(_empty_extraction_result())
                else:
                    normalized.append(result)

            extraction_result = _merge_extraction_results(*normalized)
            pipeline_trace["stages"]["stage1"] = {
                "duration_ms": _elapsed_ms(stage1_start),
                "ok": all(not isinstance(r, Exception) for r in results),
                "extractors_run": ["drawing_number", "title", "document_type"],
            }
        except Exception as exc:  # noqa: BLE001 - last-resort safety net
            self._log.exception("stage1_failed", extra={"job_id": str(job.job_id)})
            pipeline_trace["stages"]["stage1"] = {
                "duration_ms": _elapsed_ms(stage1_start),
                "ok": False,
                "error": _format_error(exc),
            }
            pipeline_trace["errors"].append(
                {"stage": "stage1", "error": _format_error(exc)}
            )
            errors.append(
                PipelineError(
                    stage="stage1",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=False,
                )
            )
            extraction_result = _empty_extraction_result()

        # ------------------------------------------------------------------
        # Stage 2 — companion document generation (CAP-017)
        # ------------------------------------------------------------------
        stage2_start = time.perf_counter()
        companion_result: CompanionResult | None = None
        try:
            companion_result = await self._companion_generator.generate(
                context, extraction_result
            )
            pipeline_trace["stages"]["stage2"] = {
                "duration_ms": _elapsed_ms(stage2_start),
                "ok": True,
                "companion_generated": companion_result.companion_generated,
                "pages_described": companion_result.pages_described,
            }
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            self._log.exception(
                "stage2_failed", extra={"job_id": str(job.job_id)}
            )
            pipeline_trace["stages"]["stage2"] = {
                "duration_ms": _elapsed_ms(stage2_start),
                "ok": False,
                "error": _format_error(exc),
            }
            pipeline_trace["errors"].append(
                {"stage": "stage2", "error": _format_error(exc)}
            )
            errors.append(
                PipelineError(
                    stage="stage2",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=True,
                )
            )
            companion_result = None

        # Companion validation is owned by task-19; until that lands the
        # confidence calculator receives ``None`` for the validation result.
        validation_result = None

        # ------------------------------------------------------------------
        # Stage 3 — sidecar build
        # ------------------------------------------------------------------
        stage3_start = time.perf_counter()
        sidecar: SidecarDocument | None = None
        try:
            sidecar = self._sidecar_builder.build(
                extraction_result, companion_result, job
            )
            pipeline_trace["stages"]["stage3"] = {
                "duration_ms": _elapsed_ms(stage3_start),
                "ok": True,
            }
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            self._log.exception("stage3_failed", extra={"job_id": str(job.job_id)})
            pipeline_trace["stages"]["stage3"] = {
                "duration_ms": _elapsed_ms(stage3_start),
                "ok": False,
                "error": _format_error(exc),
            }
            pipeline_trace["errors"].append(
                {"stage": "stage3", "error": _format_error(exc)}
            )
            errors.append(
                PipelineError(
                    stage="stage3",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=False,
                )
            )
            sidecar = _empty_sidecar(job)

        # ------------------------------------------------------------------
        # Confidence calculation
        # ------------------------------------------------------------------
        confidence_start = time.perf_counter()
        try:
            assessment: ConfidenceAssessment = (
                self._confidence_calculator.calculate(
                    extraction_result, validation_result
                )
            )
            pipeline_trace["stages"]["confidence"] = {
                "duration_ms": _elapsed_ms(confidence_start),
                "ok": True,
                "score": assessment.overall_confidence,
                "tier": assessment.tier.value,
            }
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            self._log.exception(
                "confidence_failed", extra={"job_id": str(job.job_id)}
            )
            pipeline_trace["stages"]["confidence"] = {
                "duration_ms": _elapsed_ms(confidence_start),
                "ok": False,
                "error": _format_error(exc),
            }
            pipeline_trace["errors"].append(
                {"stage": "confidence", "error": _format_error(exc)}
            )
            errors.append(
                PipelineError(
                    stage="confidence",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=False,
                )
            )
            assessment = ConfidenceAssessment(
                overall_confidence=0.0,
                tier=ConfidenceTier.REVIEW,
                breakdown={},
                validation_adjustment=0.0,
            )

        # Attach confidence_score and confidence_tier to a refreshed
        # ExtractionResult. ExtractionResult is a frozen dataclass, so we
        # use dataclasses.replace() to produce a new value with the two
        # fields populated.
        extraction_result = replace(
            extraction_result,
            confidence_score=assessment.overall_confidence,
            confidence_tier=assessment.tier,
        )

        # ------------------------------------------------------------------
        # Metadata write (CAP-023)
        #
        # AUTO and SPOT-tier results are written to ChromaDB immediately so
        # they become searchable. REVIEW-tier results are NOT written here —
        # they go to the human review queue and are flushed to ChromaDB only
        # after a reviewer approves them (CAP-022, owned by task-20).
        #
        # Failures are tolerated: the writer logs and returns False on
        # error, and the orchestrator records the failure in pipeline_trace
        # without raising. This keeps the pipeline structurally valid even
        # when ChromaDB is unreachable.
        # ------------------------------------------------------------------
        metadata_write_start = time.perf_counter()
        if assessment.tier != ConfidenceTier.REVIEW:
            try:
                wrote = await self._metadata_writer.write_metadata(
                    document_id=str(job.file_hash),
                    sidecar=sidecar,
                    deployment_id=deployment_id,
                    node_id=node_id,
                )
                pipeline_trace["stages"]["metadata_write"] = {
                    "duration_ms": _elapsed_ms(metadata_write_start),
                    "ok": wrote,
                    "skipped": False,
                }
                if not wrote:
                    pipeline_trace["errors"].append(
                        {
                            "stage": "metadata_write",
                            "error": "metadata_writer.write_metadata returned False",
                        }
                    )
                    errors.append(
                        PipelineError(
                            stage="metadata_write",
                            error_type="MetadataWriteFailed",
                            message="metadata_writer.write_metadata returned False",
                            recoverable=True,
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - degrade gracefully
                self._log.exception(
                    "metadata_write_failed", extra={"job_id": str(job.job_id)}
                )
                pipeline_trace["stages"]["metadata_write"] = {
                    "duration_ms": _elapsed_ms(metadata_write_start),
                    "ok": False,
                    "skipped": False,
                    "error": _format_error(exc),
                }
                pipeline_trace["errors"].append(
                    {"stage": "metadata_write", "error": _format_error(exc)}
                )
                errors.append(
                    PipelineError(
                        stage="metadata_write",
                        error_type=type(exc).__name__,
                        message=str(exc),
                        recoverable=True,
                    )
                )
        else:
            pipeline_trace["stages"]["metadata_write"] = {
                "duration_ms": 0,
                "ok": True,
                "skipped": True,
                "reason": "review_tier_routes_to_review_queue",
            }

        return PipelineResult(
            extraction_result=extraction_result,
            companion_result=companion_result,
            sidecar=sidecar,
            confidence_assessment=assessment,
            errors=errors,
            pipeline_trace=pipeline_trace,
            otel_trace_id=None,
        )


def _elapsed_ms(start: float) -> int:
    """Return the integer milliseconds elapsed since ``start``."""
    return int((time.perf_counter() - start) * 1000)


def _format_error(exc: BaseException) -> str:
    """Render ``exc`` as ``'ExceptionType: message'`` for trace storage."""
    return f"{type(exc).__name__}: {exc}"


def _empty_sidecar(job: Job) -> SidecarDocument:
    """Build an empty fallback :class:`SidecarDocument`.

    Used when Stage 3 raises so the orchestrator can still return a
    structurally valid :class:`PipelineResult`. The metadata_attributes
    dict is intentionally minimal: it carries only the source filename
    and a literal ``'unknown'`` document_type to remain identifiable in
    downstream logs.
    """
    return SidecarDocument(
        metadata_attributes={
            "source_filename": job.filename,
            "document_type": "unknown",
            "extraction_confidence": 0.0,
        },
        companion_text=None,
        source_filename=job.filename,
        file_hash=job.file_hash,
    )
