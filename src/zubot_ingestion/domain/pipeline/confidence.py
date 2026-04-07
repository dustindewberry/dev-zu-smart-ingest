"""Confidence calculator — implements IConfidenceCalculator (CAP-021).

The :class:`ConfidenceCalculator` collapses an :class:`ExtractionResult`'s
per-field confidence scores into a single overall score and a
:class:`ConfidenceTier` band. The algorithm is the canonical one defined by
section 4.12 of ``boundary-contracts.md``:

    weighted = (
        drawing_number_confidence * CONFIDENCE_WEIGHT_DRAWING_NUMBER
        + title_confidence          * CONFIDENCE_WEIGHT_TITLE
        + document_type_confidence  * CONFIDENCE_WEIGHT_DOCUMENT_TYPE
    )
    if validation_result and not validation_result.passed:
        weighted += CONFIDENCE_VALIDATION_PENALTY
    weighted = clamp(weighted, 0.0, 1.0)
    tier = AUTO   if weighted >= CONFIDENCE_TIER_AUTO_MIN
         else SPOT  if weighted >= CONFIDENCE_TIER_SPOT_MIN
         else REVIEW

All thresholds and weights are sourced from
:mod:`zubot_ingestion.shared.constants` so any future re-tuning lives in
exactly one place.

Per the dependency rules (boundary-contracts.md §3) this module may only
import from stdlib, ``zubot_ingestion.shared``, and ``zubot_ingestion.domain``.
It MUST NOT import from api, services, or infrastructure layers.
"""

from __future__ import annotations

from zubot_ingestion.domain.entities import (
    ConfidenceAssessment,
    ExtractionResult,
    ValidationResult,
)
from zubot_ingestion.domain.enums import ConfidenceTier
from zubot_ingestion.domain.protocols import IConfidenceCalculator
from zubot_ingestion.shared.constants import (
    CONFIDENCE_TIER_AUTO_MIN,
    CONFIDENCE_TIER_SPOT_MIN,
    CONFIDENCE_VALIDATION_PENALTY,
    CONFIDENCE_WEIGHT_DOCUMENT_TYPE,
    CONFIDENCE_WEIGHT_DRAWING_NUMBER,
    CONFIDENCE_WEIGHT_TITLE,
)

__all__ = ["ConfidenceCalculator"]


def _clamp_unit(value: float) -> float:
    """Clamp ``value`` to the closed unit interval ``[0.0, 1.0]``."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


class ConfidenceCalculator(IConfidenceCalculator):
    """Concrete confidence calculator (CAP-021)."""

    def calculate(
        self,
        extraction_result: ExtractionResult,
        validation_result: ValidationResult | None = None,
    ) -> ConfidenceAssessment:
        """Compute the overall confidence and tier for an extraction.

        Args:
            extraction_result: Stage 1 multi-source extraction result.
            validation_result: Companion validation result, if Stage 2 ran.
                When ``None`` no penalty is applied.

        Returns:
            A :class:`ConfidenceAssessment` whose ``overall_confidence``
            is in ``[0.0, 1.0]``, whose ``tier`` reflects the configured
            thresholds, and whose ``breakdown`` exposes the four
            per-component contributions for debugging.
        """
        drawing_component = (
            extraction_result.drawing_number_confidence
            * CONFIDENCE_WEIGHT_DRAWING_NUMBER
        )
        title_component = (
            extraction_result.title_confidence * CONFIDENCE_WEIGHT_TITLE
        )
        document_type_component = (
            extraction_result.document_type_confidence
            * CONFIDENCE_WEIGHT_DOCUMENT_TYPE
        )

        weighted = drawing_component + title_component + document_type_component

        validation_penalty = 0.0
        if validation_result is not None and not validation_result.passed:
            validation_penalty = CONFIDENCE_VALIDATION_PENALTY
            weighted += validation_penalty

        weighted = _clamp_unit(weighted)

        if weighted >= CONFIDENCE_TIER_AUTO_MIN:
            tier = ConfidenceTier.AUTO
        elif weighted >= CONFIDENCE_TIER_SPOT_MIN:
            tier = ConfidenceTier.SPOT
        else:
            tier = ConfidenceTier.REVIEW

        breakdown: dict[str, float] = {
            "drawing_number": drawing_component,
            "title": title_component,
            "document_type": document_type_component,
            "validation_penalty": validation_penalty,
        }

        return ConfidenceAssessment(
            overall_confidence=weighted,
            tier=tier,
            breakdown=breakdown,
            validation_adjustment=validation_penalty,
        )
