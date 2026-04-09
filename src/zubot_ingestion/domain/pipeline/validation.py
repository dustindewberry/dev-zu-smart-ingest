"""Companion validator — implements ICompanionValidator (CAP-018).

Cross-check the Stage 2 companion text against the Stage 1 extraction
result and produce a :class:`ValidationResult` that the confidence
calculator can consume.

The validator is intentionally simple and rule-based:

* If the companion text is empty or shorter than a minimum number of
  characters, validation fails outright.
* If key extracted fields (drawing number, title) do not appear in the
  companion text, a warning is attached and a small negative
  ``confidence_adjustment`` is applied.
* Otherwise the validator passes with a zero adjustment.

Layering: this module lives in the domain.pipeline layer. It imports only
from stdlib, :mod:`zubot_ingestion.domain.entities`, and
:mod:`zubot_ingestion.domain.protocols`. It MUST NOT import from
:mod:`zubot_ingestion.infrastructure` or :mod:`zubot_ingestion.api`.
"""

from __future__ import annotations

import logging

from zubot_ingestion.domain.entities import ExtractionResult, ValidationResult
from zubot_ingestion.domain.protocols import ICompanionValidator

__all__ = ["CompanionValidator", "build_companion_validator"]

_LOG = logging.getLogger(__name__)


# Rule thresholds — module-level so unit tests can reference them by name.
MIN_COMPANION_LENGTH: int = 40
FIELD_MISSING_PENALTY: float = -0.05


class CompanionValidator(ICompanionValidator):
    """Rule-based companion validator (CAP-018)."""

    def __init__(
        self,
        *,
        min_length: int = MIN_COMPANION_LENGTH,
        field_missing_penalty: float = FIELD_MISSING_PENALTY,
        logger: logging.Logger | None = None,
    ) -> None:
        self._min_length = min_length
        self._field_missing_penalty = field_missing_penalty
        self._log = logger or _LOG

    def validate(
        self,
        companion_text: str,
        extraction_result: ExtractionResult,
    ) -> ValidationResult:
        """Validate the companion text against the extraction result.

        Returns a :class:`ValidationResult` with ``passed``, ``warnings``,
        and ``confidence_adjustment`` populated. The validator is
        deliberately tolerant — it never raises — so callers can always
        use the returned value without wrapping in a try/except.
        """
        warnings: list[str] = []
        adjustment: float = 0.0

        # Rule 1: empty / too-short companion text is a hard fail.
        text = (companion_text or "").strip()
        if not text:
            return ValidationResult(
                passed=False,
                warnings=["companion_text_empty"],
                confidence_adjustment=self._field_missing_penalty * 2,
            )
        if len(text) < self._min_length:
            return ValidationResult(
                passed=False,
                warnings=[
                    f"companion_text_too_short:{len(text)}<{self._min_length}"
                ],
                confidence_adjustment=self._field_missing_penalty,
            )

        lowered = text.lower()

        # Rule 2: key fields referenced in the companion text.
        drawing = extraction_result.drawing_number
        if drawing and drawing.strip() and drawing.lower() not in lowered:
            warnings.append(f"drawing_number_not_referenced:{drawing}")
            adjustment += self._field_missing_penalty

        title = extraction_result.title
        if title and title.strip() and title.lower() not in lowered:
            warnings.append(f"title_not_referenced:{title}")
            adjustment += self._field_missing_penalty

        passed = not warnings
        return ValidationResult(
            passed=passed,
            warnings=warnings,
            confidence_adjustment=adjustment,
        )


def build_companion_validator() -> CompanionValidator:
    """Return a default-configured :class:`CompanionValidator`."""
    return CompanionValidator()
