"""Stage 3 sidecar builder — implements ISidecarBuilder (CAP-019).

The SidecarBuilder collects the outputs of Stage 1 (ExtractionResult) and
Stage 2 (CompanionResult) and assembles them into the AWS Bedrock Knowledge
Base sidecar document format. The output is a frozen ``SidecarDocument``
entity whose ``metadata_attributes`` dict is constrained by:

    1. A maximum of ``MAX_SIDECAR_METADATA_KEYS`` (10) keys, enforced by
       AWS Bedrock KB.
    2. A strict JSON Schema (``SIDECAR_JSON_SCHEMA``) that pins per-key
       types, requires the three core fields, and forbids unknown keys.

Per the dependency rules (boundary-contracts.md §3), this module may only
import from stdlib, ``zubot_ingestion.shared``, ``zubot_ingestion.domain``,
and third-party validation libraries. It MUST NOT import from api,
services, or infrastructure layers.

Design notes (for reviewers):

* The ``SidecarDocument`` entity in ``domain.entities`` exposes the assembled
  attributes via the ``metadata_attributes`` field (snake_case). Some
  documentation refers to ``metadataAttributes`` (camelCase) — that is the
  on-the-wire JSON key name used by Bedrock KB and is preserved in the JSON
  Schema title. The Python entity field is snake_case to match canonical
  task-2 conventions; the wire format is unchanged.

* The task description references ``extraction_result.confidence_score``.
  The canonical ``ExtractionResult`` entity does not (yet) carry such a
  field — only per-field confidences. To produce the required
  ``extraction_confidence`` value the builder uses the canonical weighted
  average from ``IConfidenceCalculator`` (drawing_number 40%, title 30%,
  document_type 30%). If a future ``ExtractionResult`` version adds an
  authoritative ``confidence_score`` attribute the builder will use it
  directly via ``getattr`` and skip the recomputation.

* The DocumentType enum's value is normalized to ``'unknown'`` when the
  Stage 1 classifier produces ``None``. This is the only place that
  injects the literal string ``'unknown'`` into the metadata, so callers
  can rely on the contract that document_type is always present and
  non-empty in the assembled sidecar.
"""

from __future__ import annotations

from typing import Any

import jsonschema

from zubot_ingestion.domain.entities import (
    CompanionResult,
    ExtractionResult,
    Job,
    SidecarDocument,
)
from zubot_ingestion.domain.protocols import ISidecarBuilder
from zubot_ingestion.shared.constants import MAX_SIDECAR_METADATA_KEYS

from zubot_ingestion.domain.pipeline.sidecar_schema import (
    SIDECAR_JSON_SCHEMA,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Sentinel value used as the document_type when Stage 1 produced no
#: classification. Kept as a module constant so tests can refer to it
#: without hardcoding the literal.
UNKNOWN_DOCUMENT_TYPE: str = "unknown"

#: Required attribute keys (always present in any valid sidecar).
REQUIRED_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "source_filename",
    "document_type",
    "extraction_confidence",
)

#: Optional attribute keys, in the order they will be considered for
#: inclusion when the optional fields would otherwise overflow the
#: MAX_SIDECAR_METADATA_KEYS budget. Earlier entries are higher priority.
OPTIONAL_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "drawing_number",
    "title",
    "discipline",
    "revision",
    "building_zone",
    "project",
)

# Confidence weights — kept in sync with IConfidenceCalculator.
_WEIGHT_DRAWING_NUMBER = 0.40
_WEIGHT_TITLE = 0.30
_WEIGHT_DOCUMENT_TYPE = 0.30


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SidecarValidationError(ValueError):
    """Raised when a sidecar cannot be assembled or fails schema validation.

    Attributes:
        message: Human-readable description of the validation failure.
        dropped_fields: Optional fields that were dropped to satisfy the
            ``MAX_SIDECAR_METADATA_KEYS`` budget. Empty when the failure is
            schema-related rather than count-related.
        schema_error: The underlying ``jsonschema.ValidationError`` if the
            failure originated from schema validation, else ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        dropped_fields: list[str] | None = None,
        schema_error: jsonschema.ValidationError | None = None,
    ) -> None:
        self.message = message
        self.dropped_fields = list(dropped_fields or [])
        self.schema_error = schema_error
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        if self.dropped_fields:
            return (
                f"{self.message} "
                f"(dropped optional fields: {', '.join(self.dropped_fields)})"
            )
        return self.message


# ---------------------------------------------------------------------------
# SidecarBuilder
# ---------------------------------------------------------------------------


class SidecarBuilder(ISidecarBuilder):
    """Stage 3: Assemble the final Bedrock KB sidecar document."""

    def build(
        self,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult | None,
        job: Job,
    ) -> SidecarDocument:
        """Build the SidecarDocument for a single completed extraction.

        Args:
            extraction_result: Stage 1 multi-source extraction output.
            companion_result: Stage 2 companion output, or ``None`` if the
                companion stage was skipped (e.g., text-only PDF).
            job: The persisted Job entity for this extraction.

        Returns:
            A frozen :class:`SidecarDocument` whose ``metadata_attributes``
            satisfies both the key-count budget and the JSON Schema.

        Raises:
            SidecarValidationError: If the optional fields cannot fit
                within the key-count budget, or if the assembled metadata
                fails JSON Schema validation.
        """
        confidence_score = self._derive_confidence(extraction_result)

        required = self._build_required(
            job=job,
            extraction_result=extraction_result,
            confidence_score=confidence_score,
        )

        optional_candidates = self._build_optional_candidates(extraction_result)

        metadata_attributes, dropped = self._merge_with_budget(
            required=required,
            optional_candidates=optional_candidates,
        )

        if dropped:
            # Hard-fail rather than silently dropping fields. Stage 1 should
            # never emit more than 6 optional fields + 3 required = 9 keys,
            # so reaching this branch indicates a contract violation
            # somewhere upstream.
            raise SidecarValidationError(
                (
                    f"Sidecar metadata exceeds {MAX_SIDECAR_METADATA_KEYS} key "
                    f"limit ({len(required) + len(optional_candidates)} > "
                    f"{MAX_SIDECAR_METADATA_KEYS})"
                ),
                dropped_fields=dropped,
            )

        self._validate_schema(metadata_attributes)

        companion_text = (
            companion_result.companion_text
            if companion_result is not None and companion_result.companion_generated
            else None
        )

        return SidecarDocument(
            metadata_attributes=metadata_attributes,
            companion_text=companion_text,
            source_filename=job.filename,
            file_hash=job.file_hash,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_confidence(extraction_result: ExtractionResult) -> float:
        """Return an aggregate confidence in [0.0, 1.0].

        Prefers an authoritative ``confidence_score`` attribute on the
        extraction result if one exists; otherwise falls back to the
        weighted average used by ``IConfidenceCalculator``.
        """
        explicit = getattr(extraction_result, "confidence_score", None)
        if explicit is not None:
            return SidecarBuilder._clamp(float(explicit))

        weighted = (
            extraction_result.drawing_number_confidence * _WEIGHT_DRAWING_NUMBER
            + extraction_result.title_confidence * _WEIGHT_TITLE
            + extraction_result.document_type_confidence * _WEIGHT_DOCUMENT_TYPE
        )
        return SidecarBuilder._clamp(weighted)

    @staticmethod
    def _clamp(value: float) -> float:
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return value

    @staticmethod
    def _build_required(
        *,
        job: Job,
        extraction_result: ExtractionResult,
        confidence_score: float,
    ) -> dict[str, Any]:
        """Build the always-present required attribute trio."""
        document_type_value = SidecarBuilder._stringify_enum(
            extraction_result.document_type,
            fallback=UNKNOWN_DOCUMENT_TYPE,
        )
        return {
            "source_filename": job.filename,
            "document_type": document_type_value,
            "extraction_confidence": confidence_score,
        }

    @staticmethod
    def _build_optional_candidates(
        extraction_result: ExtractionResult,
    ) -> dict[str, str]:
        """Collect non-null optional fields, normalizing enums to strings.

        The returned dict preserves insertion order in
        ``OPTIONAL_ATTRIBUTE_KEYS`` order so the budget logic in
        ``_merge_with_budget`` deterministically prefers higher-priority
        fields.
        """
        raw_values: dict[str, Any] = {
            "drawing_number": extraction_result.drawing_number,
            "title": extraction_result.title,
            "discipline": extraction_result.discipline,
            "revision": extraction_result.revision,
            "building_zone": extraction_result.building_zone,
            "project": extraction_result.project,
        }

        included: dict[str, str] = {}
        for key in OPTIONAL_ATTRIBUTE_KEYS:
            raw = raw_values[key]
            if raw is None:
                continue
            normalized = SidecarBuilder._stringify_enum(raw, fallback=None)
            if normalized is None:
                continue
            normalized = normalized.strip()
            if not normalized:
                continue
            included[key] = normalized
        return included

    @staticmethod
    def _merge_with_budget(
        *,
        required: dict[str, Any],
        optional_candidates: dict[str, str],
    ) -> tuple[dict[str, Any], list[str]]:
        """Merge required + optional under the MAX_SIDECAR_METADATA_KEYS cap.

        Returns the merged dict plus the list of optional keys that had to
        be dropped to fit the budget. The dropped list is empty when
        everything fits.
        """
        merged: dict[str, Any] = dict(required)
        budget = MAX_SIDECAR_METADATA_KEYS - len(merged)
        dropped: list[str] = []

        for key, value in optional_candidates.items():
            if budget > 0:
                merged[key] = value
                budget -= 1
            else:
                dropped.append(key)

        return merged, dropped

    @staticmethod
    def _stringify_enum(value: Any, *, fallback: str | None) -> str | None:
        """Normalize Enum-like values to their underlying string representation."""
        if value is None:
            return fallback
        if hasattr(value, "value"):
            inner = value.value  # Enum -> primitive
            return str(inner) if inner is not None else fallback
        return str(value)

    @staticmethod
    def _validate_schema(metadata_attributes: dict[str, Any]) -> None:
        """Validate the assembled dict against ``SIDECAR_JSON_SCHEMA``.

        Translates ``jsonschema.ValidationError`` into a layer-local
        ``SidecarValidationError`` so callers in the orchestrator only
        need to know about one exception type.
        """
        try:
            jsonschema.validate(
                instance=metadata_attributes,
                schema=SIDECAR_JSON_SCHEMA,
            )
        except jsonschema.ValidationError as exc:
            raise SidecarValidationError(
                f"Sidecar metadata failed JSON Schema validation: {exc.message}",
                schema_error=exc,
            ) from exc


__all__ = [
    "OPTIONAL_ATTRIBUTE_KEYS",
    "REQUIRED_ATTRIBUTE_KEYS",
    "SidecarBuilder",
    "SidecarValidationError",
    "UNKNOWN_DOCUMENT_TYPE",
]
