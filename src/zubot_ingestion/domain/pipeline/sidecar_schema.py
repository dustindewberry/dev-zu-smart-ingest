"""JSON Schema for the Bedrock KB sidecar metadataAttributes object.

Implements the Stage 3 sidecar contract from boundary-contracts.md §4.11
(ISidecarBuilder). The schema is intentionally strict so that any drift
between the SidecarBuilder's output and the AWS Bedrock Knowledge Base
ingest format is caught locally before it reaches the vector store.

Conventions:
    * Required fields: source_filename, document_type, extraction_confidence
    * All string fields are typed string (no nulls in optional positions —
      optional fields are simply omitted from the dict).
    * extraction_confidence is typed number with explicit [0, 1] bounds.
    * additionalProperties is false to guarantee no unknown keys leak in.
    * Total key count is independently bounded by the SidecarBuilder against
      MAX_SIDECAR_METADATA_KEYS — the schema enforces the per-key contract,
      while the builder enforces the count contract.

Per the dependency rules (boundary-contracts.md §3), this module may only
import from stdlib and third-party validation libraries. It MUST NOT import
from api, services, or infrastructure layers.
"""

from __future__ import annotations

from typing import Any

import jsonschema
from jsonschema import ValidationError as JsonSchemaValidationError

# ---------------------------------------------------------------------------
# Schema definition
# ---------------------------------------------------------------------------

#: JSON Schema (Draft 2020-12) for the metadataAttributes object that the
#: SidecarBuilder produces and the AWS Bedrock Knowledge Base ingests.
SIDECAR_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ZubotSidecarMetadataAttributes",
    "description": (
        "Bedrock KB sidecar metadataAttributes object produced by Stage 3 "
        "(ISidecarBuilder.build). All optional fields are simply omitted "
        "when their source value is null/missing."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": [
        "source_filename",
        "document_type",
        "extraction_confidence",
    ],
    "properties": {
        "source_filename": {
            "type": "string",
            "minLength": 1,
            "description": "Original filename of the uploaded PDF.",
        },
        "document_type": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Document classification from the controlled vocabulary, "
                "or 'unknown' if Stage 1 produced no classification."
            ),
        },
        "extraction_confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": (
                "Aggregate per-field extraction confidence (drawing number "
                "weighted highest). Range [0.0, 1.0]."
            ),
        },
        "drawing_number": {
            "type": "string",
            "minLength": 1,
            "description": "Drawing number extracted from title block or filename.",
        },
        "title": {
            "type": "string",
            "minLength": 1,
            "description": "Document title extracted from title block.",
        },
        "discipline": {
            "type": "string",
            "minLength": 1,
            "description": "Engineering discipline classification.",
        },
        "revision": {
            "type": "string",
            "minLength": 1,
            "description": "Drawing revision identifier (e.g., 'P02', 'Rev A').",
        },
        "building_zone": {
            "type": "string",
            "minLength": 1,
            "description": "Building zone or area identifier.",
        },
        "project": {
            "type": "string",
            "minLength": 1,
            "description": "Project identifier or name.",
        },
    },
}


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------


def validate_sidecar_metadata(metadata_attributes: dict[str, Any]) -> None:
    """Validate a metadataAttributes dict against ``SIDECAR_JSON_SCHEMA``.

    This is a thin wrapper around ``jsonschema.validate`` that exists so the
    SidecarBuilder can centralize validation in one call site rather than
    inlining the schema reference. It does not transform the input.

    Args:
        metadata_attributes: The fully assembled metadataAttributes dict
            (required keys + optional keys filtered to non-null only).

    Raises:
        jsonschema.ValidationError: If the dict violates the schema. The
            caller (SidecarBuilder.build) is responsible for translating
            this into the layer-local SidecarValidationError.
    """
    jsonschema.validate(instance=metadata_attributes, schema=SIDECAR_JSON_SCHEMA)


__all__ = [
    "SIDECAR_JSON_SCHEMA",
    "JsonSchemaValidationError",
    "validate_sidecar_metadata",
]
