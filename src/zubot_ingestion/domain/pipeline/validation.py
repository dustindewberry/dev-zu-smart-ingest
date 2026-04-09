"""Companion validator — implements ICompanionValidator (CAP-018 / Bug 5).

The :class:`CompanionValidator` performs rule-based cross-checking of a
generated companion description against the Stage 1 extraction result.
It emits a :class:`ValidationResult` with:

* ``validation_passed``: ``True`` iff no issues were raised.
* ``quality_score``: coverage ratio in ``[0.0, 1.0]`` — the fraction of
  extracted fields (with confidence > 0) whose value appears in the
  companion text, case-insensitively.
* ``issues``: list of issue codes. Codes are stable strings so downstream
  consumers can branch on them without parsing free-form text.

Rule set
--------
1. ``empty_companion`` — ``companion_text`` is empty / whitespace-only.
2. ``companion_too_short`` — ``len(companion_text) < 50``.
3. ``field_not_referenced_{field_name}`` — for each of
   ``drawing_number``, ``title``, ``document_type``, if the extraction
   result has confidence > 0 AND a non-empty value, that value must
   appear in ``companion_text`` at least once, case-insensitively.

Quality score
-------------
``quality_score = fields_referenced / total_fields_with_confidence``
clamped to ``[0.0, 1.0]``. When no fields have confidence > 0 (i.e. the
extractor produced nothing), the score defaults to ``1.0`` because
there is nothing the companion could have failed to reference.

Per the dependency rules (boundary-contracts.md §3) this module may only
import from stdlib, ``zubot_ingestion.shared``, and
``zubot_ingestion.domain``. It MUST NOT import from api, services, or
infrastructure layers.
"""

from __future__ import annotations

from zubot_ingestion.domain.entities import ExtractionResult, ValidationResult
from zubot_ingestion.domain.enums import DocumentType
from zubot_ingestion.domain.protocols import ICompanionValidator

__all__ = ["CompanionValidator", "build_companion_validator"]


_MIN_COMPANION_LENGTH = 50


def _clamp_unit(value: float) -> float:
    """Clamp ``value`` to the closed unit interval ``[0.0, 1.0]``."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _field_value_str(value: object) -> str | None:
    """Coerce an extracted field value to a non-empty string or ``None``.

    ``DocumentType`` and other ``str``-Enum values are normalized to their
    ``.value`` form. Anything else that is falsy or whitespace-only is
    treated as "no value".
    """
    if value is None:
        return None
    if isinstance(value, DocumentType):
        text = value.value
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    text = text.strip()
    return text or None


class CompanionValidator(ICompanionValidator):
    """Rule-based companion validator (CAP-018).

    This implementation is stateless and thread-safe; a single instance
    may be shared across the application.
    """

    def __init__(self, min_length: int = _MIN_COMPANION_LENGTH) -> None:
        """Create a validator with a configurable minimum-length threshold.

        Args:
            min_length: Minimum acceptable ``len(companion_text)``. Any
                shorter (but non-empty) text raises
                ``companion_too_short``.
        """
        self._min_length = min_length

    def validate(
        self,
        companion_text: str,
        extraction_result: ExtractionResult,
    ) -> ValidationResult:
        """Run all validation rules and return a :class:`ValidationResult`.

        Args:
            companion_text: Stage 2 companion description (may be empty).
            extraction_result: Stage 1 extraction result whose fields
                should be referenced in the companion.

        Returns:
            A frozen :class:`ValidationResult` whose ``validation_passed``
            flag is ``True`` iff ``issues`` is empty.
        """
        issues: list[str] = []

        # Rule 1: empty companion.
        stripped = companion_text.strip() if companion_text else ""
        if not stripped:
            issues.append("empty_companion")

        # Rule 2: minimum length. Only apply when the companion is not
        # completely empty — ``empty_companion`` already covers that case
        # and emitting both codes would be redundant noise.
        if stripped and len(companion_text) < self._min_length:
            issues.append("companion_too_short")

        # Rule 3: each extracted field with confidence > 0 must appear in
        # the companion text case-insensitively. Track which fields we
        # checked and which ones were successfully referenced so we can
        # compute the coverage ratio for the quality score.
        lowered_companion = companion_text.lower() if companion_text else ""

        field_specs: tuple[tuple[str, object, float], ...] = (
            (
                "drawing_number",
                extraction_result.drawing_number,
                extraction_result.drawing_number_confidence,
            ),
            (
                "title",
                extraction_result.title,
                extraction_result.title_confidence,
            ),
            (
                "document_type",
                extraction_result.document_type,
                extraction_result.document_type_confidence,
            ),
        )

        total_fields_with_confidence = 0
        fields_referenced = 0

        for field_name, raw_value, confidence in field_specs:
            if confidence <= 0:
                continue
            value_str = _field_value_str(raw_value)
            if value_str is None:
                # Extractor claimed confidence but produced no value —
                # count it as a field that could not be referenced so
                # the coverage ratio correctly penalizes the companion.
                total_fields_with_confidence += 1
                issues.append(f"field_not_referenced_{field_name}")
                continue

            total_fields_with_confidence += 1
            if value_str.lower() in lowered_companion:
                fields_referenced += 1
            else:
                issues.append(f"field_not_referenced_{field_name}")

        # Rule 4: quality score = coverage ratio. When no fields had
        # confidence > 0 there is nothing to cover, so the default is 1.0.
        if total_fields_with_confidence == 0:
            quality_score = 1.0
        else:
            quality_score = _clamp_unit(
                fields_referenced / total_fields_with_confidence
            )

        validation_passed = len(issues) == 0

        return ValidationResult(
            passed=validation_passed,
            warnings=list(issues),
            confidence_adjustment=0.0,
            validation_passed=validation_passed,
            quality_score=quality_score,
            issues=list(issues),
        )


def build_companion_validator() -> CompanionValidator:
    """Return a default-configured :class:`CompanionValidator` instance."""
    return CompanionValidator()
