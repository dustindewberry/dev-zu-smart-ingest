"""Extraction orchestrator with OpenTelemetry tracing (CAP-020 + CAP-027).

This is the silver-falcon (task-22 / step-22) version of the orchestrator.
It is a strict superset of the agile-falcon (task-16 / step-16) orchestrator:
the same pipeline (PDF load -> three concurrent Stage 1 extractors -> Stage 2
companion (skipped) -> Stage 3 sidecar -> confidence calc) is executed, the
same per-stage pipeline_trace dict is recorded, and the same graceful
degradation rules apply.

What this version adds (CAP-027):

* Every pipeline run is wrapped in a single ``OTEL_SPAN_BATCH`` root span,
  and every job inside the run gets its own ``OTEL_SPAN_JOB`` child span.
* Each pipeline stage is wrapped in its own child span using the
  ``OTEL_SPAN_STAGE1_*``, ``OTEL_SPAN_STAGE2_COMPANION``,
  ``OTEL_SPAN_STAGE3_SIDECAR``, and ``OTEL_SPAN_CONFIDENCE`` constants from
  :mod:`zubot_ingestion.shared.constants`.
* The job span carries ``file_hash``, ``filename``, and ``page_count``
  attributes. Each stage span carries the appropriate ``model.name``,
  ``inference.duration_ms``, and ``confidence.score`` attributes.
* The OTEL trace_id of the job span is captured (formatted as a 32-char
  lowercase hex string) and returned in :class:`PipelineResult.otel_trace_id`
  so the Celery task can persist it to ``job.otel_trace_id`` via
  ``IJobRepository.update_job_result``.

Stage failures continue to populate ``pipeline_trace['errors']`` and the
``errors`` list on :class:`PipelineResult` for backwards compatibility,
AND in addition each failing span has its status set to ``StatusCode.ERROR``
and the exception attached via :meth:`Span.record_exception` so the trace
itself reflects the failure mode.

Layering: this module lives in the application (services) layer. It imports
from :mod:`zubot_ingestion.shared`, :mod:`zubot_ingestion.domain`, and
:mod:`zubot_ingestion.infrastructure.otel`. The OTEL infrastructure import
is the ONE permitted exception to the "services may not import from
infrastructure" rule because tracing is genuinely cross-cutting and the
import-linter contract carves out
``zubot_ingestion.infrastructure.otel`` accordingly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any

from opentelemetry.trace import Span, Status, StatusCode

from zubot_ingestion.domain.entities import (
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
    IConfidenceCalculator,
    IExtractor,
    IOrchestrator,
    IPDFProcessor,
    ISidecarBuilder,
)
from zubot_ingestion.infrastructure.otel.instrumentation import get_tracer
from zubot_ingestion.shared.constants import (
    OTEL_SPAN_BATCH,
    OTEL_SPAN_CONFIDENCE,
    OTEL_SPAN_JOB,
    OTEL_SPAN_STAGE1_DOC_TYPE,
    OTEL_SPAN_STAGE1_DRAWING_NUMBER,
    OTEL_SPAN_STAGE1_TITLE,
    OTEL_SPAN_STAGE2_COMPANION,
    OTEL_SPAN_STAGE3_SIDECAR,
)
from zubot_ingestion.shared.types import PipelineError

__all__ = ["ExtractionOrchestrator"]

_LOG = logging.getLogger(__name__)

# Sentinel used to format OTEL trace_ids as 32-char lowercase hex strings.
_TRACE_ID_HEX_FORMAT = "032x"


def _empty_extraction_result() -> ExtractionResult:
    """Return a zero-confidence :class:`ExtractionResult` placeholder."""
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
    """Combine the per-extractor outputs into a single ExtractionResult."""
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
    """Application-layer pipeline orchestrator with OTEL tracing."""

    def __init__(
        self,
        drawing_number_extractor: IExtractor,
        title_extractor: IExtractor,
        document_type_extractor: IExtractor,
        sidecar_builder: ISidecarBuilder,
        confidence_calculator: IConfidenceCalculator,
        pdf_processor: IPDFProcessor,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._drawing_number_extractor = drawing_number_extractor
        self._title_extractor = title_extractor
        self._document_type_extractor = document_type_extractor
        self._sidecar_builder = sidecar_builder
        self._confidence_calculator = confidence_calculator
        self._pdf_processor = pdf_processor
        self._log = logger or _LOG
        self._tracer = get_tracer()

    async def run_pipeline(
        self,
        job: Job,
        pdf_bytes: bytes,
    ) -> PipelineResult:
        """Execute the full extraction pipeline for a single job.

        The entire run is wrapped in a single ``OTEL_SPAN_BATCH`` root span
        and a single ``OTEL_SPAN_JOB`` child span. Each individual pipeline
        stage gets its own grandchild span. The job span's trace_id is
        captured and returned in ``PipelineResult.otel_trace_id`` so callers
        (typically the Celery extract task) can persist it via
        ``IJobRepository.update_job_result``.
        """
        pipeline_trace: dict[str, Any] = {"stages": {}, "errors": []}
        errors: list[PipelineError] = []

        with self._tracer.start_as_current_span(OTEL_SPAN_BATCH):
            with self._tracer.start_as_current_span(
                OTEL_SPAN_JOB
            ) as job_span:
                # Capture the trace_id BEFORE running any stages so we can
                # surface it in the returned PipelineResult even if a later
                # stage raises (the orchestrator never re-raises — it
                # degrades). Trace IDs are 128-bit ints in the OTEL SDK; we
                # render them as 32-char lowercase hex per the W3C trace
                # context spec.
                otel_trace_id = format(
                    job_span.get_span_context().trace_id,
                    _TRACE_ID_HEX_FORMAT,
                )

                # Initial job-span attributes (file_hash and filename are
                # always available from the job entity; page_count is
                # filled in after PDF load below).
                job_span.set_attribute("file_hash", str(job.file_hash))
                job_span.set_attribute("filename", job.filename)
                job_span.set_attribute("job_id", str(job.job_id))
                job_span.set_attribute("batch_id", str(job.batch_id))

                # ----------------------------------------------------------
                # Load PDF (zero stage — outside the named OTEL stages but
                # still recorded into pipeline_trace for debugging)
                # ----------------------------------------------------------
                pdf_data: PDFData | None = None
                load_start = time.perf_counter()
                try:
                    pdf_data = self._pdf_processor.load(pdf_bytes)
                    pipeline_trace["stages"]["pdf_load"] = {
                        "duration_ms": _elapsed_ms(load_start),
                        "ok": True,
                    }
                    job_span.set_attribute(
                        "page_count", pdf_data.page_count
                    )
                except Exception as exc:  # noqa: BLE001 - degrade gracefully
                    self._log.exception(
                        "pdf_load_failed",
                        extra={"job_id": str(job.job_id)},
                    )
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
                    job_span.record_exception(exc)
                    job_span.set_status(
                        Status(StatusCode.ERROR, "pdf_load failed")
                    )

                context = PipelineContext(
                    job=job,
                    pdf_bytes=pdf_bytes,
                    pdf_data=pdf_data,
                )

                # ----------------------------------------------------------
                # Stage 1 — three concurrent multi-source extractors, each
                # in its own span
                # ----------------------------------------------------------
                stage1_start = time.perf_counter()
                try:
                    drawing_task = self._run_extractor(
                        OTEL_SPAN_STAGE1_DRAWING_NUMBER,
                        "drawing_number",
                        self._drawing_number_extractor,
                        context,
                    )
                    title_task = self._run_extractor(
                        OTEL_SPAN_STAGE1_TITLE,
                        "title",
                        self._title_extractor,
                        context,
                    )
                    doctype_task = self._run_extractor(
                        OTEL_SPAN_STAGE1_DOC_TYPE,
                        "document_type",
                        self._document_type_extractor,
                        context,
                    )

                    results = await asyncio.gather(
                        drawing_task,
                        title_task,
                        doctype_task,
                        return_exceptions=True,
                    )

                    normalized: list[ExtractionResult] = []
                    for label, result in zip(
                        ("drawing_number", "title", "document_type"),
                        results,
                    ):
                        if isinstance(result, Exception):
                            self._log.warning(
                                "stage1_extractor_failed",
                                extra={
                                    "extractor": label,
                                    "job_id": str(job.job_id),
                                },
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
                        "ok": all(
                            not isinstance(r, Exception) for r in results
                        ),
                        "extractors_run": [
                            "drawing_number",
                            "title",
                            "document_type",
                        ],
                    }
                except Exception as exc:  # noqa: BLE001 - last-resort
                    self._log.exception(
                        "stage1_failed", extra={"job_id": str(job.job_id)}
                    )
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

                # ----------------------------------------------------------
                # Stage 2 — companion (skipped, owned by task-18). The span
                # is still emitted with a "skipped" attribute so traces are
                # consistent and downstream consumers (Phoenix dashboards,
                # etc.) can filter on it.
                # ----------------------------------------------------------
                with self._tracer.start_as_current_span(
                    OTEL_SPAN_STAGE2_COMPANION
                ) as stage2_span:
                    stage2_span.set_attribute("skipped", True)
                    stage2_span.set_attribute("inference.duration_ms", 0)
                pipeline_trace["stages"]["stage2"] = {
                    "skipped": True,
                    "duration_ms": 0,
                }
                companion_result = None
                validation_result = None

                # ----------------------------------------------------------
                # Stage 3 — sidecar build
                # ----------------------------------------------------------
                stage3_start = time.perf_counter()
                sidecar: SidecarDocument | None = None
                with self._tracer.start_as_current_span(
                    OTEL_SPAN_STAGE3_SIDECAR
                ) as stage3_span:
                    try:
                        sidecar = self._sidecar_builder.build(
                            extraction_result, companion_result, job
                        )
                        duration_ms = _elapsed_ms(stage3_start)
                        pipeline_trace["stages"]["stage3"] = {
                            "duration_ms": duration_ms,
                            "ok": True,
                        }
                        stage3_span.set_attribute(
                            "inference.duration_ms", duration_ms
                        )
                    except Exception as exc:  # noqa: BLE001 - degrade
                        self._log.exception(
                            "stage3_failed",
                            extra={"job_id": str(job.job_id)},
                        )
                        duration_ms = _elapsed_ms(stage3_start)
                        pipeline_trace["stages"]["stage3"] = {
                            "duration_ms": duration_ms,
                            "ok": False,
                            "error": _format_error(exc),
                        }
                        pipeline_trace["errors"].append(
                            {
                                "stage": "stage3",
                                "error": _format_error(exc),
                            }
                        )
                        errors.append(
                            PipelineError(
                                stage="stage3",
                                error_type=type(exc).__name__,
                                message=str(exc),
                                recoverable=False,
                            )
                        )
                        stage3_span.set_attribute(
                            "inference.duration_ms", duration_ms
                        )
                        stage3_span.record_exception(exc)
                        stage3_span.set_status(
                            Status(StatusCode.ERROR, "stage3 failed")
                        )
                        sidecar = _empty_sidecar(job)

                # ----------------------------------------------------------
                # Confidence calculation
                # ----------------------------------------------------------
                confidence_start = time.perf_counter()
                with self._tracer.start_as_current_span(
                    OTEL_SPAN_CONFIDENCE
                ) as confidence_span:
                    try:
                        assessment: ConfidenceAssessment = (
                            self._confidence_calculator.calculate(
                                extraction_result, validation_result
                            )
                        )
                        duration_ms = _elapsed_ms(confidence_start)
                        pipeline_trace["stages"]["confidence"] = {
                            "duration_ms": duration_ms,
                            "ok": True,
                            "score": assessment.overall_confidence,
                            "tier": assessment.tier.value,
                        }
                        confidence_span.set_attribute(
                            "confidence.score",
                            float(assessment.overall_confidence),
                        )
                        confidence_span.set_attribute(
                            "confidence.tier", assessment.tier.value
                        )
                        confidence_span.set_attribute(
                            "inference.duration_ms", duration_ms
                        )
                    except Exception as exc:  # noqa: BLE001 - degrade
                        self._log.exception(
                            "confidence_failed",
                            extra={"job_id": str(job.job_id)},
                        )
                        duration_ms = _elapsed_ms(confidence_start)
                        pipeline_trace["stages"]["confidence"] = {
                            "duration_ms": duration_ms,
                            "ok": False,
                            "error": _format_error(exc),
                        }
                        pipeline_trace["errors"].append(
                            {
                                "stage": "confidence",
                                "error": _format_error(exc),
                            }
                        )
                        errors.append(
                            PipelineError(
                                stage="confidence",
                                error_type=type(exc).__name__,
                                message=str(exc),
                                recoverable=False,
                            )
                        )
                        confidence_span.set_attribute(
                            "inference.duration_ms", duration_ms
                        )
                        confidence_span.record_exception(exc)
                        confidence_span.set_status(
                            Status(
                                StatusCode.ERROR, "confidence failed"
                            )
                        )
                        assessment = ConfidenceAssessment(
                            overall_confidence=0.0,
                            tier=ConfidenceTier.REVIEW,
                            breakdown={},
                            validation_adjustment=0.0,
                        )

                # Annotate the parent job span with the final score+tier so
                # an operator inspecting a single job span in Phoenix has
                # the headline metrics without drilling into the
                # confidence child span.
                job_span.set_attribute(
                    "confidence.score", float(assessment.overall_confidence)
                )
                job_span.set_attribute(
                    "confidence.tier", assessment.tier.value
                )

                # Attach confidence_score and confidence_tier to a refreshed
                # ExtractionResult (frozen dataclass — use replace()).
                extraction_result = replace(
                    extraction_result,
                    confidence_score=assessment.overall_confidence,
                    confidence_tier=assessment.tier,
                )

        return PipelineResult(
            extraction_result=extraction_result,
            companion_result=companion_result,
            sidecar=sidecar,
            confidence_assessment=assessment,
            errors=errors,
            pipeline_trace=pipeline_trace,
            otel_trace_id=otel_trace_id,
        )

    async def _run_extractor(
        self,
        span_name: str,
        label: str,
        extractor: IExtractor,
        context: PipelineContext,
    ) -> ExtractionResult:
        """Run a single Stage 1 extractor inside its own OTEL span.

        The span carries:

        * ``extractor.label`` — the field this extractor populates
        * ``inference.duration_ms`` — wall-clock duration of the call
        * ``model.name`` — best-effort, taken from the extractor's
          ``model_name`` attribute if present (the canonical extractors
          from task-14 expose this; tests can opt out by passing a fake
          extractor without the attribute)
        * ``confidence.score`` — the per-field confidence on the
          returned :class:`ExtractionResult` (only the field this
          extractor owns, since the others will be 0.0 in a partial
          result)

        On exception the span status is set to ERROR and the exception
        is re-raised so :func:`asyncio.gather(return_exceptions=True)`
        can capture it for the caller's per-extractor error handling.
        """
        with self._tracer.start_as_current_span(span_name) as span:
            span.set_attribute("extractor.label", label)
            model_name = getattr(extractor, "model_name", None)
            if model_name:
                span.set_attribute("model.name", str(model_name))

            start = time.perf_counter()
            try:
                result = await extractor.extract(context)
            except Exception as exc:
                duration_ms = _elapsed_ms(start)
                span.set_attribute("inference.duration_ms", duration_ms)
                span.record_exception(exc)
                span.set_status(
                    Status(StatusCode.ERROR, f"{label} extractor failed")
                )
                raise

            duration_ms = _elapsed_ms(start)
            span.set_attribute("inference.duration_ms", duration_ms)

            field_confidence = _per_field_confidence(label, result)
            if field_confidence is not None:
                span.set_attribute(
                    "confidence.score", float(field_confidence)
                )

            return result


def _per_field_confidence(
    label: str,
    result: ExtractionResult,
) -> float | None:
    """Return the confidence score for the field a given extractor owns."""
    if label == "drawing_number":
        return result.drawing_number_confidence
    if label == "title":
        return result.title_confidence
    if label == "document_type":
        return result.document_type_confidence
    return None


def _elapsed_ms(start: float) -> int:
    """Return the integer milliseconds elapsed since ``start``."""
    return int((time.perf_counter() - start) * 1000)


def _format_error(exc: BaseException) -> str:
    """Render ``exc`` as ``'ExceptionType: message'`` for trace storage."""
    return f"{type(exc).__name__}: {exc}"


def _empty_sidecar(job: Job) -> SidecarDocument:
    """Build an empty fallback :class:`SidecarDocument`."""
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
