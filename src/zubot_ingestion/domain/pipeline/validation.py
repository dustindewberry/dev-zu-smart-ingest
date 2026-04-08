"""Companion-text validator (CAP-018 / step-19 canonical implementation).

Cross-checks a generated companion description against the Stage-1
extraction result and reports any inconsistencies as failures (hard
validation errors) or warnings (soft signals that feed a confidence
penalty).

This module implements the ICompanionValidator protocol defined in
zubot_ingestion.domain.protocols and is the single source of truth for
companion-vs-extraction consistency checks.
"""

from __future__ import annotations

import re

from zubot_ingestion.domain.protocols import (
    ExtractionResult,
    ICompanionValidator,
    ValidationResult,
)

# Minimum companion length before we treat it as "suspiciously short"
_MIN_REASONABLE_LENGTH = 50

# Minimum token length for a title word to count as a meaningful search term
_MIN_TITLE_TOKEN_LENGTH = 4

# Penalties applied per-rule (additive, then clamped to [0.0, 1.0])
_PENALTY_EMPTY = 1.0
_PENALTY_SHORT = 0.1
_PENALTY_MISSING_DRAWING_NUMBER = 0.3
_PENALTY_MISSING_TITLE_TOKENS = 0.1


class CompanionValidator:
    """Cross-checks the generated companion text against the extraction result.

    Implements ICompanionValidator (domain/protocols.py).

    Rules (see CAP-018):
      1. Empty / whitespace-only companion text is a hard failure
         (confidence_penalty = 1.0).
      2. Companion text shorter than 50 characters is a warning
         (confidence_penalty += 0.1).
      3. If an extracted drawing number exists and does not appear
         (case-insensitive) in the companion text, it is a hard failure
         (confidence_penalty += 0.3).
      4. If an extracted title exists and none of its >=4-character tokens
         appear (case-insensitive) in the companion text, it is a warning
         (confidence_penalty += 0.1).
      5. document_type presence is informational only — no penalty.

    is_valid is True iff no failures were recorded. Warnings alone do not
    invalidate. confidence_penalty is clamped to [0.0, 1.0].
    """

    async def validate(
        self,
        companion_text: str,
        extraction_result: ExtractionResult,
    ) -> ValidationResult:
        """Validate companion_text against extraction_result.

        Args:
            companion_text: Generated visual description text.
            extraction_result: Stage 1 extraction results.

        Returns:
            ValidationResult with is_valid, confidence_penalty, failures,
            warnings populated per the rules described in the class
            docstring.
        """
        failures: list[str] = []
        warnings: list[str] = []
        penalty: float = 0.0

        # Rule 1: non-empty check. Short-circuit — an empty companion
        # cannot be meaningfully checked against extraction metadata.
        if not companion_text or not companion_text.strip():
            return ValidationResult(
                is_valid=False,
                confidence_penalty=_PENALTY_EMPTY,
                failures=["companion text is empty"],
                warnings=[],
            )

        # Rule 2: length sanity check.
        if len(companion_text) < _MIN_REASONABLE_LENGTH:
            warnings.append("companion text suspiciously short")
            penalty += _PENALTY_SHORT

        companion_lower = companion_text.lower()

        # Rule 3: drawing number presence (case-insensitive substring match).
        drawing_number = extraction_result.drawing_number
        if drawing_number:
            if drawing_number.lower() not in companion_lower:
                failures.append(
                    f"drawing number {drawing_number} not found in companion"
                )
                penalty += _PENALTY_MISSING_DRAWING_NUMBER

        # Rule 4: title token presence. We consider any word of >=4 chars
        # from the title a candidate search term. If NONE of them appear in
        # the companion we emit a warning.
        title = extraction_result.title
        if title:
            title_tokens = [
                tok
                for tok in re.findall(r"\w+", title)
                if len(tok) >= _MIN_TITLE_TOKEN_LENGTH
            ]
            if title_tokens:
                any_found = any(
                    tok.lower() in companion_lower for tok in title_tokens
                )
                if not any_found:
                    warnings.append("title tokens not found in companion")
                    penalty += _PENALTY_MISSING_TITLE_TOKENS

        # Rule 5: document_type is informational only — no automatic
        # failure, no penalty. We deliberately do not inspect it here.
        _ = extraction_result.document_type

        return ValidationResult(
            is_valid=len(failures) == 0,
            confidence_penalty=min(max(penalty, 0.0), 1.0),
            failures=failures,
            warnings=warnings,
        )


__all__ = ["CompanionValidator"]
