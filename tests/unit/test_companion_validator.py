"""Unit tests for :class:`CompanionValidator` (CAP-018 / Bug 5)."""

from __future__ import annotations

import pytest

from zubot_ingestion.domain.entities import ExtractionResult, ValidationResult
from zubot_ingestion.domain.enums import DocumentType
from zubot_ingestion.domain.pipeline.validation import (
    CompanionValidator,
    build_companion_validator,
)
from zubot_ingestion.domain.protocols import ICompanionValidator


def _extraction(
    *,
    drawing_number: str | None = "KXC-B6-001",
    drawing_number_confidence: float = 0.9,
    title: str | None = "Level 3 Electrical Layout",
    title_confidence: float = 0.85,
    document_type: DocumentType | None = DocumentType.ELECTRICAL_SCHEMATIC,
    document_type_confidence: float = 0.8,
) -> ExtractionResult:
    return ExtractionResult(
        drawing_number=drawing_number,
        drawing_number_confidence=drawing_number_confidence,
        title=title,
        title_confidence=title_confidence,
        document_type=document_type,
        document_type_confidence=document_type_confidence,
    )


def _companion_with_all_fields() -> str:
    return (
        "This electrical_schematic titled 'Level 3 Electrical Layout' "
        "for drawing KXC-B6-001 depicts the full distribution board layout, "
        "cable runs, and panel schedules across the third floor."
    )


# ---------------------------------------------------------------------------
# Empty / too-short text
# ---------------------------------------------------------------------------


def test_empty_text_raises_empty_companion_issue() -> None:
    validator = CompanionValidator()
    result = validator.validate("", _extraction())

    assert isinstance(result, ValidationResult)
    assert result.validation_passed is False
    assert result.passed is False
    assert "empty_companion" in result.issues
    # An empty companion must not *also* emit companion_too_short; that
    # rule is only meaningful for non-empty text.
    assert "companion_too_short" not in result.issues


def test_whitespace_only_text_treated_as_empty() -> None:
    validator = CompanionValidator()
    result = validator.validate("   \n\t  ", _extraction())

    assert result.validation_passed is False
    assert "empty_companion" in result.issues


def test_too_short_text_raises_companion_too_short_issue() -> None:
    validator = CompanionValidator()
    # 20 characters — below the 50-char threshold.
    short_text = "Electrical KXC-B6-001"
    result = validator.validate(short_text, _extraction())

    assert result.validation_passed is False
    assert "companion_too_short" in result.issues
    assert "empty_companion" not in result.issues


def test_text_just_under_threshold_is_rejected() -> None:
    validator = CompanionValidator(min_length=50)
    text = "a" * 49
    # Build an extraction result with zero confidence everywhere so the
    # only remaining failure mode is the length check.
    extraction = _extraction(
        drawing_number_confidence=0.0,
        title_confidence=0.0,
        document_type_confidence=0.0,
    )
    result = validator.validate(text, extraction)

    assert "companion_too_short" in result.issues
    assert result.validation_passed is False


def test_text_at_threshold_passes_length_check() -> None:
    validator = CompanionValidator(min_length=50)
    text = "a" * 50
    extraction = _extraction(
        drawing_number_confidence=0.0,
        title_confidence=0.0,
        document_type_confidence=0.0,
    )
    result = validator.validate(text, extraction)

    assert "companion_too_short" not in result.issues
    assert result.validation_passed is True
    # No fields with confidence > 0 → default score 1.0
    assert result.quality_score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Field reference checks
# ---------------------------------------------------------------------------


def test_all_fields_referenced_case_insensitively_passes() -> None:
    validator = CompanionValidator()
    companion = _companion_with_all_fields()
    result = validator.validate(companion, _extraction())

    assert result.validation_passed is True
    assert result.issues == []
    assert result.quality_score == pytest.approx(1.0)


def test_case_insensitive_match_for_field_values() -> None:
    validator = CompanionValidator()
    extraction = _extraction(
        drawing_number="ABC-123",
        title="Foundation Plan",
        document_type=DocumentType.FLOOR_PLAN,
    )
    # All three field values rewritten in wildly different case.
    companion = (
        "This document, abc-123, presents the FOUNDATION PLAN for the north "
        "wing and should be classified as a Floor_Plan by the system."
    )
    result = validator.validate(companion, extraction)

    assert result.validation_passed is True
    assert result.issues == []


def test_missing_drawing_number_reports_field_not_referenced() -> None:
    validator = CompanionValidator()
    # Title and document_type are present, drawing_number ("KXC-B6-001") is not.
    companion = (
        "This electrical_schematic titled 'Level 3 Electrical Layout' "
        "depicts the full distribution board layout across the floor."
    )
    result = validator.validate(companion, _extraction())

    assert result.validation_passed is False
    assert "field_not_referenced_drawing_number" in result.issues
    assert "field_not_referenced_title" not in result.issues
    assert "field_not_referenced_document_type" not in result.issues


def test_missing_title_and_doctype_reports_both_issues() -> None:
    validator = CompanionValidator()
    # Only drawing_number appears in the companion.
    companion = (
        "The single identifying marker on this sheet is the drawing "
        "number KXC-B6-001, shown in the lower right corner of the page."
    )
    result = validator.validate(companion, _extraction())

    assert result.validation_passed is False
    assert "field_not_referenced_title" in result.issues
    assert "field_not_referenced_document_type" in result.issues
    assert "field_not_referenced_drawing_number" not in result.issues


def test_field_with_zero_confidence_is_not_required_in_companion() -> None:
    validator = CompanionValidator()
    extraction = _extraction(
        drawing_number="KXC-B6-001",
        drawing_number_confidence=0.9,
        title="Some Title That Is Absent",
        title_confidence=0.0,  # <-- zero confidence, must be skipped
        document_type=DocumentType.FLOOR_PLAN,
        document_type_confidence=0.8,
    )
    companion = (
        "The floor_plan for this project references drawing KXC-B6-001, "
        "which is the primary identifier on the sheet."
    )
    result = validator.validate(companion, extraction)

    assert result.validation_passed is True
    assert "field_not_referenced_title" not in result.issues


def test_field_with_confidence_but_no_value_is_counted_as_missing() -> None:
    validator = CompanionValidator()
    # Confidence > 0 but the actual value is None. The extractor claimed
    # confidence so we must count it against coverage.
    extraction = _extraction(
        drawing_number=None,
        drawing_number_confidence=0.7,
        title="Level 3 Electrical Layout",
        title_confidence=0.85,
        document_type=DocumentType.ELECTRICAL_SCHEMATIC,
        document_type_confidence=0.8,
    )
    companion = _companion_with_all_fields()
    result = validator.validate(companion, extraction)

    assert result.validation_passed is False
    assert "field_not_referenced_drawing_number" in result.issues
    # Title and doc_type should still be found.
    assert "field_not_referenced_title" not in result.issues
    assert "field_not_referenced_document_type" not in result.issues
    # 2 of 3 fields referenced → quality_score ~ 0.666
    assert result.quality_score == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# Quality score computation
# ---------------------------------------------------------------------------


def test_quality_score_all_fields_covered() -> None:
    validator = CompanionValidator()
    result = validator.validate(_companion_with_all_fields(), _extraction())
    assert result.quality_score == pytest.approx(1.0)


def test_quality_score_partial_coverage() -> None:
    validator = CompanionValidator()
    extraction = _extraction()
    # Only drawing number appears.
    companion = (
        "The only identifier on this sheet is KXC-B6-001 — the rest of "
        "the metadata must be inferred from context in later stages."
    )
    result = validator.validate(companion, extraction)
    assert result.quality_score == pytest.approx(1 / 3)


def test_quality_score_zero_when_no_fields_match() -> None:
    validator = CompanionValidator()
    extraction = _extraction()
    companion = (
        "This document contains no useful identifying information that "
        "can be matched against the extraction result at all."
    )
    result = validator.validate(companion, extraction)
    assert result.quality_score == pytest.approx(0.0)
    assert result.validation_passed is False


def test_quality_score_defaults_to_one_when_no_fields_have_confidence() -> None:
    validator = CompanionValidator()
    extraction = _extraction(
        drawing_number_confidence=0.0,
        title_confidence=0.0,
        document_type_confidence=0.0,
    )
    companion = "A generic description with no extraction fields to cover " + ("x" * 20)
    result = validator.validate(companion, extraction)

    assert result.quality_score == pytest.approx(1.0)
    assert result.validation_passed is True
    assert result.issues == []


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_build_companion_validator_returns_companion_validator() -> None:
    validator = build_companion_validator()
    assert isinstance(validator, CompanionValidator)
    assert isinstance(validator, ICompanionValidator)


def test_build_companion_validator_returns_independent_instances() -> None:
    a = build_companion_validator()
    b = build_companion_validator()
    assert a is not b


def test_build_companion_validator_default_instance_runs_validate() -> None:
    validator = build_companion_validator()
    result = validator.validate(_companion_with_all_fields(), _extraction())
    assert isinstance(result, ValidationResult)
    assert result.validation_passed is True
