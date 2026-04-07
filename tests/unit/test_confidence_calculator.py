"""Unit tests for :class:`ConfidenceCalculator` (CAP-021).

Coverage:
    - Protocol conformance
    - All three tiers (AUTO, SPOT, REVIEW) at and around their thresholds
    - Validation penalty (passed=True, passed=False, validation_result=None)
    - Clamping at the unit interval bounds (negative weights, > 1.0)
    - Breakdown contents and shape
    - validation_adjustment field on the assessment
    - Threshold sensitivity (just-above-AUTO, just-below-AUTO, etc.)
"""

from __future__ import annotations

import math

import pytest

from zubot_ingestion.domain.entities import (
    ConfidenceAssessment,
    ExtractionResult,
    ValidationResult,
)
from zubot_ingestion.domain.enums import ConfidenceTier, DocumentType
from zubot_ingestion.domain.pipeline.confidence import ConfidenceCalculator
from zubot_ingestion.domain.protocols import IConfidenceCalculator
from zubot_ingestion.shared.constants import (
    CONFIDENCE_TIER_AUTO_MIN,
    CONFIDENCE_TIER_SPOT_MIN,
    CONFIDENCE_VALIDATION_PENALTY,
    CONFIDENCE_WEIGHT_DOCUMENT_TYPE,
    CONFIDENCE_WEIGHT_DRAWING_NUMBER,
    CONFIDENCE_WEIGHT_TITLE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_extraction(
    drawing: float,
    title: float,
    document_type: float,
) -> ExtractionResult:
    """Build a minimal :class:`ExtractionResult` with given confidences."""
    return ExtractionResult(
        drawing_number="X-001" if drawing > 0 else None,
        drawing_number_confidence=drawing,
        title="Test Title" if title > 0 else None,
        title_confidence=title,
        document_type=DocumentType.TECHNICAL_DRAWING if document_type > 0 else None,
        document_type_confidence=document_type,
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_confidence_calculator_satisfies_protocol() -> None:
    calc = ConfidenceCalculator()
    assert isinstance(calc, IConfidenceCalculator)


# ---------------------------------------------------------------------------
# Constants are byte-pinned
# ---------------------------------------------------------------------------


def test_constants_are_pinned_to_blueprint_values() -> None:
    assert CONFIDENCE_WEIGHT_DRAWING_NUMBER == pytest.approx(0.40)
    assert CONFIDENCE_WEIGHT_TITLE == pytest.approx(0.30)
    assert CONFIDENCE_WEIGHT_DOCUMENT_TYPE == pytest.approx(0.30)
    assert CONFIDENCE_VALIDATION_PENALTY == pytest.approx(-0.10)
    assert CONFIDENCE_TIER_AUTO_MIN == pytest.approx(0.8)
    assert CONFIDENCE_TIER_SPOT_MIN == pytest.approx(0.5)
    # Sanity check: weights sum to 1.0
    total = (
        CONFIDENCE_WEIGHT_DRAWING_NUMBER
        + CONFIDENCE_WEIGHT_TITLE
        + CONFIDENCE_WEIGHT_DOCUMENT_TYPE
    )
    assert total == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Weighted average — happy path
# ---------------------------------------------------------------------------


def test_weighted_average_with_perfect_confidences_yields_1_0() -> None:
    calc = ConfidenceCalculator()
    assessment = calc.calculate(_make_extraction(1.0, 1.0, 1.0))
    assert assessment.overall_confidence == pytest.approx(1.0)
    assert assessment.tier == ConfidenceTier.AUTO


def test_weighted_average_with_zero_confidences_yields_0_0() -> None:
    calc = ConfidenceCalculator()
    assessment = calc.calculate(_make_extraction(0.0, 0.0, 0.0))
    assert assessment.overall_confidence == pytest.approx(0.0)
    assert assessment.tier == ConfidenceTier.REVIEW


def test_weighted_average_uses_correct_weights() -> None:
    calc = ConfidenceCalculator()
    assessment = calc.calculate(_make_extraction(0.5, 0.6, 0.7))
    expected = 0.5 * 0.40 + 0.6 * 0.30 + 0.7 * 0.30
    assert assessment.overall_confidence == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Tier assignment — boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("d", "t", "dt", "expected_score", "expected_tier"),
    [
        # Exactly at AUTO threshold (0.80)
        (0.80, 0.80, 0.80, 0.80, ConfidenceTier.AUTO),
        # Just above AUTO threshold
        (0.85, 0.85, 0.85, 0.85, ConfidenceTier.AUTO),
        # Just below AUTO threshold (still SPOT)
        (0.79, 0.79, 0.79, 0.79, ConfidenceTier.SPOT),
        # Exactly at SPOT threshold (0.50)
        (0.50, 0.50, 0.50, 0.50, ConfidenceTier.SPOT),
        # Just above SPOT threshold
        (0.55, 0.55, 0.55, 0.55, ConfidenceTier.SPOT),
        # Just below SPOT threshold (REVIEW)
        (0.49, 0.49, 0.49, 0.49, ConfidenceTier.REVIEW),
        # Mid REVIEW
        (0.20, 0.20, 0.20, 0.20, ConfidenceTier.REVIEW),
    ],
)
def test_tier_boundaries(
    d: float,
    t: float,
    dt: float,
    expected_score: float,
    expected_tier: ConfidenceTier,
) -> None:
    calc = ConfidenceCalculator()
    assessment = calc.calculate(_make_extraction(d, t, dt))
    assert assessment.overall_confidence == pytest.approx(expected_score)
    assert assessment.tier == expected_tier


# ---------------------------------------------------------------------------
# Validation penalty
# ---------------------------------------------------------------------------


def test_validation_passed_does_not_apply_penalty() -> None:
    calc = ConfidenceCalculator()
    extraction = _make_extraction(0.85, 0.85, 0.85)
    validation = ValidationResult(passed=True)
    assessment = calc.calculate(extraction, validation)
    assert assessment.overall_confidence == pytest.approx(0.85)
    assert assessment.tier == ConfidenceTier.AUTO
    assert assessment.validation_adjustment == pytest.approx(0.0)
    assert assessment.breakdown["validation_penalty"] == pytest.approx(0.0)


def test_validation_failed_applies_negative_penalty() -> None:
    calc = ConfidenceCalculator()
    extraction = _make_extraction(0.85, 0.85, 0.85)
    validation = ValidationResult(passed=False)
    assessment = calc.calculate(extraction, validation)
    # 0.85 - 0.10 = 0.75
    assert assessment.overall_confidence == pytest.approx(0.75)
    assert assessment.tier == ConfidenceTier.SPOT
    assert assessment.validation_adjustment == pytest.approx(-0.10)
    assert assessment.breakdown["validation_penalty"] == pytest.approx(-0.10)


def test_validation_none_does_not_apply_penalty() -> None:
    calc = ConfidenceCalculator()
    extraction = _make_extraction(0.85, 0.85, 0.85)
    assessment = calc.calculate(extraction, None)
    assert assessment.overall_confidence == pytest.approx(0.85)
    assert assessment.validation_adjustment == pytest.approx(0.0)
    assert assessment.breakdown["validation_penalty"] == pytest.approx(0.0)


def test_validation_failure_can_demote_auto_to_spot() -> None:
    calc = ConfidenceCalculator()
    # Right at AUTO with penalty pushed to SPOT
    extraction = _make_extraction(0.82, 0.82, 0.82)
    validation = ValidationResult(passed=False)
    assessment = calc.calculate(extraction, validation)
    # 0.82 - 0.10 = 0.72 -> SPOT
    assert assessment.overall_confidence == pytest.approx(0.72)
    assert assessment.tier == ConfidenceTier.SPOT


def test_validation_failure_can_demote_spot_to_review() -> None:
    calc = ConfidenceCalculator()
    # 0.55 -> SPOT, then penalty -> 0.45 -> REVIEW
    extraction = _make_extraction(0.55, 0.55, 0.55)
    validation = ValidationResult(passed=False)
    assessment = calc.calculate(extraction, validation)
    assert assessment.overall_confidence == pytest.approx(0.45)
    assert assessment.tier == ConfidenceTier.REVIEW


# ---------------------------------------------------------------------------
# Clamping
# ---------------------------------------------------------------------------


def test_clamping_lower_bound_with_negative_input() -> None:
    """Even with bizarre negative confidences the result must be >= 0.0."""
    calc = ConfidenceCalculator()
    extraction = _make_extraction(-1.0, -1.0, -1.0)
    assessment = calc.calculate(extraction)
    assert assessment.overall_confidence == pytest.approx(0.0)
    assert assessment.tier == ConfidenceTier.REVIEW


def test_clamping_lower_bound_with_validation_penalty_overshoot() -> None:
    """0.05 raw + (-0.10) penalty would be -0.05 — clamped to 0.0."""
    calc = ConfidenceCalculator()
    extraction = _make_extraction(0.05, 0.05, 0.05)
    validation = ValidationResult(passed=False)
    assessment = calc.calculate(extraction, validation)
    assert assessment.overall_confidence == pytest.approx(0.0)
    assert assessment.tier == ConfidenceTier.REVIEW


def test_clamping_upper_bound_cannot_exceed_1_0() -> None:
    """Out-of-spec confidences > 1.0 must clamp at the unit interval top."""
    calc = ConfidenceCalculator()
    extraction = _make_extraction(2.0, 2.0, 2.0)
    assessment = calc.calculate(extraction)
    assert assessment.overall_confidence == pytest.approx(1.0)
    assert assessment.tier == ConfidenceTier.AUTO


# ---------------------------------------------------------------------------
# Breakdown shape
# ---------------------------------------------------------------------------


def test_breakdown_contains_required_keys() -> None:
    calc = ConfidenceCalculator()
    assessment = calc.calculate(_make_extraction(0.5, 0.6, 0.7))
    assert set(assessment.breakdown.keys()) == {
        "drawing_number",
        "title",
        "document_type",
        "validation_penalty",
    }


def test_breakdown_components_match_weights() -> None:
    calc = ConfidenceCalculator()
    assessment = calc.calculate(_make_extraction(0.5, 0.6, 0.7))
    assert assessment.breakdown["drawing_number"] == pytest.approx(0.5 * 0.40)
    assert assessment.breakdown["title"] == pytest.approx(0.6 * 0.30)
    assert assessment.breakdown["document_type"] == pytest.approx(0.7 * 0.30)
    assert assessment.breakdown["validation_penalty"] == pytest.approx(0.0)


def test_breakdown_sum_minus_penalty_matches_overall() -> None:
    """The non-penalty breakdown components should sum to the overall score
    when no clamping or penalty is applied."""
    calc = ConfidenceCalculator()
    extraction = _make_extraction(0.4, 0.5, 0.6)
    assessment = calc.calculate(extraction)
    component_sum = (
        assessment.breakdown["drawing_number"]
        + assessment.breakdown["title"]
        + assessment.breakdown["document_type"]
    )
    assert assessment.overall_confidence == pytest.approx(component_sum)


def test_breakdown_with_validation_penalty_records_penalty_separately() -> None:
    calc = ConfidenceCalculator()
    extraction = _make_extraction(0.85, 0.85, 0.85)
    assessment = calc.calculate(extraction, ValidationResult(passed=False))
    component_sum = (
        assessment.breakdown["drawing_number"]
        + assessment.breakdown["title"]
        + assessment.breakdown["document_type"]
        + assessment.breakdown["validation_penalty"]
    )
    assert assessment.overall_confidence == pytest.approx(component_sum)


# ---------------------------------------------------------------------------
# Return value shape
# ---------------------------------------------------------------------------


def test_returns_confidence_assessment_dataclass_instance() -> None:
    calc = ConfidenceCalculator()
    assessment = calc.calculate(_make_extraction(0.5, 0.5, 0.5))
    assert isinstance(assessment, ConfidenceAssessment)
    assert isinstance(assessment.overall_confidence, float)
    assert isinstance(assessment.tier, ConfidenceTier)
    assert isinstance(assessment.breakdown, dict)
    assert math.isfinite(assessment.overall_confidence)
