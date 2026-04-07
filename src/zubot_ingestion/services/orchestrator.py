"""Orchestrator metric instrumentation hooks (CAP-028 instrumentation slice).

This file is a MINIMAL STUB owned by the witty-atlas worktree (task-23 /
step-23 prometheus-metrics). The canonical full ExtractionOrchestrator
that implements IOrchestrator and runs the three-stage extraction
pipeline is owned by task-16 (agile-falcon worktree). On merge, the
canonical task-16 orchestrator MUST take precedence over this stub. The
ONLY behavioural contribution this file makes is the metric-recording
helpers below — they MUST be ported into the canonical orchestrator
file at the locations indicated by the docstrings.

Three integration points are required in the canonical orchestrator:

1. **Stage timing -> extraction_duration histogram**

   At the very top of ``run_pipeline``, capture ``time.perf_counter()``
   into ``_pipeline_start``. Immediately before ``return PipelineResult(...)``,
   call ``record_extraction_duration(_pipeline_start)`` so the total
   wall-clock for the entire pipeline is observed in seconds.

2. **Per-field confidence -> confidence_score histogram**

   After the merge step that produces ``extraction_result`` from the
   three Stage 1 partials, call
   ``record_field_confidences(extraction_result)`` so each of the three
   per-field confidence values lands in
   ``confidence_score.labels(field=...)``. Skip None values gracefully.

3. **Status counter -> extraction_total**

   At the end of ``run_pipeline`` (after the confidence calculator
   resolves the tier), call
   ``record_extraction_status(assessment.tier, errors)``. The helper
   maps:

       - any unrecoverable error in ``errors`` -> 'failed'
       - tier == ConfidenceTier.REVIEW          -> 'review'
       - otherwise                              -> 'completed'

   The status label MUST be one of {'completed', 'failed', 'review'}
   per the task spec.

The helpers below are pure functions that take primitive inputs and
update the metric singletons in
``zubot_ingestion.infrastructure.metrics.prometheus``. They have no
runtime dependency on the canonical orchestrator's internals beyond
the public ``ExtractionResult`` and ``ConfidenceTier`` types from the
domain layer, which means they can be unit-tested in isolation.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from zubot_ingestion.infrastructure.metrics.prometheus import (
    confidence_score,
    extraction_duration,
    extraction_total,
)

__all__ = [
    "record_extraction_duration",
    "record_field_confidences",
    "record_extraction_status",
]


# The label values are pinned here as module-level constants so the
# canonical orchestrator and the unit tests can share the same source
# of truth for the spelling.
STATUS_COMPLETED: str = "completed"
STATUS_FAILED: str = "failed"
STATUS_REVIEW: str = "review"

# Per-field labels for the confidence_score histogram. These three
# labels MUST match the literal strings used by the Stage 1 extractors.
FIELD_DRAWING_NUMBER: str = "drawing_number"
FIELD_TITLE: str = "title"
FIELD_DOCUMENT_TYPE: str = "document_type"


def record_extraction_duration(start_perf_counter: float) -> None:
    """Observe the elapsed wall-clock since ``start_perf_counter`` (seconds).

    Args:
        start_perf_counter: The value returned by ``time.perf_counter()``
            at the start of the pipeline.
    """
    elapsed_seconds = max(0.0, time.perf_counter() - start_perf_counter)
    extraction_duration.observe(elapsed_seconds)


def record_field_confidences(extraction_result: Any) -> None:
    """Observe one ``confidence_score{field=...}`` sample per populated field.

    Reads ``drawing_number_confidence``, ``title_confidence``, and
    ``document_type_confidence`` off the ``extraction_result``. Any
    attribute that is missing or ``None`` is skipped — this matches the
    "best-effort, never raise" contract that the orchestrator already
    follows for partial Stage 1 failures.

    Args:
        extraction_result: A merged Stage 1 ExtractionResult dataclass.
    """
    pairs: tuple[tuple[str, str], ...] = (
        (FIELD_DRAWING_NUMBER, "drawing_number_confidence"),
        (FIELD_TITLE, "title_confidence"),
        (FIELD_DOCUMENT_TYPE, "document_type_confidence"),
    )
    for field_label, attr in pairs:
        value = getattr(extraction_result, attr, None)
        if value is None:
            continue
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        confidence_score.labels(field=field_label).observe(numeric_value)


def record_extraction_status(
    tier: Any | None,
    errors: Iterable[Any] | None = None,
) -> str:
    """Increment ``extraction_total{status=...}`` exactly once per pipeline run.

    Args:
        tier: A ``ConfidenceTier`` enum value (or None when the pipeline
            failed before confidence calculation could complete).
        errors: The ``errors`` list that the orchestrator accumulates
            for unrecoverable stage failures. If non-empty AND any of
            the contained errors has ``recoverable=False``, the status
            is forced to 'failed' regardless of the tier value.

    Returns:
        The status label that was actually recorded
        ({'completed', 'failed', 'review'}). Returning the label makes
        the function easy to assert against in unit tests.
    """
    has_unrecoverable_error = False
    if errors is not None:
        for err in errors:
            if not getattr(err, "recoverable", True):
                has_unrecoverable_error = True
                break

    if has_unrecoverable_error or tier is None:
        status = STATUS_FAILED
    else:
        # Compare against the enum's .value (a lowercase string) so this
        # function does not need to import the ConfidenceTier enum
        # directly — that keeps the metric layer dependency-free of the
        # domain layer.
        tier_value = getattr(tier, "value", tier)
        if isinstance(tier_value, str) and tier_value.lower() == STATUS_REVIEW:
            status = STATUS_REVIEW
        else:
            status = STATUS_COMPLETED

    extraction_total.labels(status=status).inc()
    return status
