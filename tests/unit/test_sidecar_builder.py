"""Unit tests for the Stage 3 SidecarBuilder (CAP-019)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import jsonschema
import pytest

from zubot_ingestion.domain.entities import (
    CompanionResult,
    ExtractionResult,
    Job,
    SidecarDocument,
)
from zubot_ingestion.domain.enums import (
    ConfidenceTier,
    Discipline,
    DocumentType,
    JobStatus,
)
from zubot_ingestion.domain.pipeline.sidecar import (
    OPTIONAL_ATTRIBUTE_KEYS,
    REQUIRED_ATTRIBUTE_KEYS,
    SidecarBuilder,
    SidecarValidationError,
    UNKNOWN_DOCUMENT_TYPE,
)
from zubot_ingestion.domain.pipeline.sidecar_schema import (
    SIDECAR_JSON_SCHEMA,
    validate_sidecar_metadata,
)
from zubot_ingestion.domain.protocols import ISidecarBuilder
from zubot_ingestion.shared.constants import MAX_SIDECAR_METADATA_KEYS
from zubot_ingestion.shared.types import BatchId, FileHash, JobId


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_job(filename: str = "test_drawing.pdf") -> Job:
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return Job(
        job_id=JobId(uuid4()),
        batch_id=BatchId(uuid4()),
        filename=filename,
        file_hash=FileHash("a" * 64),
        file_path=f"/tmp/zubot/{filename}",
        status=JobStatus.PROCESSING,
        result=None,
        error_message=None,
        pipeline_trace=None,
        otel_trace_id=None,
        processing_time_ms=None,
        created_at=now,
        updated_at=now,
        confidence_tier=ConfidenceTier.AUTO,
        overall_confidence=0.85,
    )


def _make_full_extraction_result() -> ExtractionResult:
    """An ExtractionResult with EVERY optional field populated."""
    return ExtractionResult(
        drawing_number="170154-L-001",
        drawing_number_confidence=0.95,
        title="Ground Floor Lighting Layout",
        title_confidence=0.85,
        document_type=DocumentType.ELECTRICAL_SCHEMATIC,
        document_type_confidence=0.90,
        discipline=Discipline.ELECTRICAL,
        revision="P02",
        building_zone="Zone A",
        project="Heathrow T3",
        sources_used=["vision", "text", "filename"],
    )


def _make_minimal_extraction_result() -> ExtractionResult:
    """An ExtractionResult with no optional fields populated."""
    return ExtractionResult(
        drawing_number=None,
        drawing_number_confidence=0.40,
        title=None,
        title_confidence=0.30,
        document_type=None,
        document_type_confidence=0.20,
    )


def _make_companion_result(text: str = "Sample companion description.") -> CompanionResult:
    return CompanionResult(
        companion_text=text,
        pages_described=4,
        companion_generated=True,
        validation_passed=True,
        quality_score=0.8,
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_sidecar_builder_satisfies_isidecarbuilder_protocol() -> None:
    """SidecarBuilder must satisfy the ISidecarBuilder runtime protocol."""
    builder = SidecarBuilder()
    assert isinstance(builder, ISidecarBuilder)


# ---------------------------------------------------------------------------
# Full sidecar (every optional field populated)
# ---------------------------------------------------------------------------


def test_full_sidecar_includes_all_attributes() -> None:
    builder = SidecarBuilder()
    job = _make_job(filename="170154-L-001.pdf")
    extraction = _make_full_extraction_result()
    companion = _make_companion_result("Companion text for the lighting drawing.")

    sidecar = builder.build(extraction, companion, job)

    assert isinstance(sidecar, SidecarDocument)

    attrs = sidecar.metadata_attributes
    # Three required + six optional = nine total (under the 10 budget).
    assert len(attrs) == 9
    assert set(attrs.keys()) == {
        "source_filename",
        "document_type",
        "extraction_confidence",
        "drawing_number",
        "title",
        "discipline",
        "revision",
        "building_zone",
        "project",
    }

    assert attrs["source_filename"] == "170154-L-001.pdf"
    assert attrs["document_type"] == "electrical_schematic"
    assert attrs["drawing_number"] == "170154-L-001"
    assert attrs["title"] == "Ground Floor Lighting Layout"
    assert attrs["discipline"] == "electrical"
    assert attrs["revision"] == "P02"
    assert attrs["building_zone"] == "Zone A"
    assert attrs["project"] == "Heathrow T3"

    # Confidence is the weighted aggregate: 0.95*0.4 + 0.85*0.3 + 0.90*0.3
    expected_confidence = 0.95 * 0.40 + 0.85 * 0.30 + 0.90 * 0.30
    assert attrs["extraction_confidence"] == pytest.approx(expected_confidence)


def test_full_sidecar_carries_companion_text_through() -> None:
    builder = SidecarBuilder()
    job = _make_job()
    extraction = _make_full_extraction_result()
    companion = _make_companion_result("Detailed companion description.")

    sidecar = builder.build(extraction, companion, job)

    assert sidecar.companion_text == "Detailed companion description."
    assert sidecar.source_filename == job.filename
    assert sidecar.file_hash == job.file_hash


def test_full_sidecar_validates_against_schema() -> None:
    builder = SidecarBuilder()
    sidecar = builder.build(
        _make_full_extraction_result(),
        _make_companion_result(),
        _make_job(),
    )
    # Should not raise — every attribute satisfies its per-key schema rule.
    validate_sidecar_metadata(sidecar.metadata_attributes)


# ---------------------------------------------------------------------------
# Minimal sidecar (only the required trio)
# ---------------------------------------------------------------------------


def test_minimal_sidecar_only_required_keys() -> None:
    builder = SidecarBuilder()
    job = _make_job(filename="unknown.pdf")
    extraction = _make_minimal_extraction_result()

    sidecar = builder.build(extraction, None, job)

    attrs = sidecar.metadata_attributes
    assert len(attrs) == len(REQUIRED_ATTRIBUTE_KEYS)
    assert set(attrs.keys()) == set(REQUIRED_ATTRIBUTE_KEYS)
    assert attrs["source_filename"] == "unknown.pdf"
    assert attrs["document_type"] == UNKNOWN_DOCUMENT_TYPE
    expected_confidence = 0.40 * 0.40 + 0.30 * 0.30 + 0.20 * 0.30
    assert attrs["extraction_confidence"] == pytest.approx(expected_confidence)


def test_minimal_sidecar_companion_text_is_none() -> None:
    builder = SidecarBuilder()
    sidecar = builder.build(
        _make_minimal_extraction_result(),
        None,
        _make_job(),
    )
    assert sidecar.companion_text is None


def test_minimal_sidecar_validates_against_schema() -> None:
    builder = SidecarBuilder()
    sidecar = builder.build(
        _make_minimal_extraction_result(),
        None,
        _make_job(),
    )
    validate_sidecar_metadata(sidecar.metadata_attributes)


def test_minimal_sidecar_with_skipped_companion_result() -> None:
    """A CompanionResult with companion_generated=False yields no companion_text."""
    builder = SidecarBuilder()
    skipped_companion = CompanionResult(
        companion_text="",
        pages_described=0,
        companion_generated=False,
        validation_passed=True,
    )
    sidecar = builder.build(
        _make_minimal_extraction_result(),
        skipped_companion,
        _make_job(),
    )
    assert sidecar.companion_text is None


# ---------------------------------------------------------------------------
# Optional field handling
# ---------------------------------------------------------------------------


def test_partial_optional_fields_only_includes_present_keys() -> None:
    builder = SidecarBuilder()
    extraction = ExtractionResult(
        drawing_number="L-001",
        drawing_number_confidence=0.8,
        title=None,  # absent
        title_confidence=0.0,
        document_type=DocumentType.FLOOR_PLAN,
        document_type_confidence=0.7,
        discipline=None,  # absent
        revision="A",
        building_zone=None,  # absent
        project=None,  # absent
    )
    sidecar = builder.build(extraction, None, _make_job())
    attrs = sidecar.metadata_attributes

    # Three required + drawing_number + revision = 5 keys
    assert len(attrs) == 5
    assert "drawing_number" in attrs
    assert "revision" in attrs
    assert "title" not in attrs
    assert "discipline" not in attrs
    assert "building_zone" not in attrs
    assert "project" not in attrs


def test_empty_string_optional_field_is_dropped() -> None:
    """Whitespace-only or empty optional fields should not be persisted."""
    builder = SidecarBuilder()
    extraction = ExtractionResult(
        drawing_number="   ",  # whitespace-only
        drawing_number_confidence=0.5,
        title="",  # empty string
        title_confidence=0.5,
        document_type=DocumentType.SPECIFICATION,
        document_type_confidence=0.7,
    )
    sidecar = builder.build(extraction, None, _make_job())
    attrs = sidecar.metadata_attributes
    assert "drawing_number" not in attrs
    assert "title" not in attrs
    assert len(attrs) == 3


def test_document_type_enum_normalized_to_string_value() -> None:
    builder = SidecarBuilder()
    extraction = ExtractionResult(
        drawing_number=None,
        drawing_number_confidence=0.0,
        title=None,
        title_confidence=0.0,
        document_type=DocumentType.AS_BUILT_DRAWING,
        document_type_confidence=0.9,
    )
    sidecar = builder.build(extraction, None, _make_job())
    assert sidecar.metadata_attributes["document_type"] == "as_built_drawing"
    assert isinstance(sidecar.metadata_attributes["document_type"], str)


def test_discipline_enum_normalized_to_string_value() -> None:
    builder = SidecarBuilder()
    extraction = ExtractionResult(
        drawing_number=None,
        drawing_number_confidence=0.0,
        title=None,
        title_confidence=0.0,
        document_type=DocumentType.MECHANICAL_DRAWING,
        document_type_confidence=0.7,
        discipline=Discipline.MECHANICAL,
    )
    sidecar = builder.build(extraction, None, _make_job())
    assert sidecar.metadata_attributes["discipline"] == "mechanical"


# ---------------------------------------------------------------------------
# Confidence derivation
# ---------------------------------------------------------------------------


def test_confidence_clamped_to_unit_interval_when_explicit_attribute_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a future ExtractionResult adds confidence_score, the builder uses it."""
    builder = SidecarBuilder()

    class _ExtendedExtraction(ExtractionResult):
        confidence_score: float

    extraction = _make_full_extraction_result()
    object.__setattr__(extraction, "confidence_score", 1.5)  # out of range

    sidecar = builder.build(extraction, None, _make_job())
    # Clamped to 1.0
    assert sidecar.metadata_attributes["extraction_confidence"] == 1.0


def test_confidence_clamped_lower_bound_when_explicit_attribute_negative() -> None:
    builder = SidecarBuilder()
    extraction = _make_full_extraction_result()
    object.__setattr__(extraction, "confidence_score", -0.2)
    sidecar = builder.build(extraction, None, _make_job())
    assert sidecar.metadata_attributes["extraction_confidence"] == 0.0


# ---------------------------------------------------------------------------
# Key-count enforcement
# ---------------------------------------------------------------------------


def test_max_key_constant_matches_aws_bedrock_kb_limit() -> None:
    """Defensive check that the constant has not drifted."""
    assert MAX_SIDECAR_METADATA_KEYS == 10


def test_full_sidecar_under_max_keys() -> None:
    builder = SidecarBuilder()
    sidecar = builder.build(
        _make_full_extraction_result(),
        _make_companion_result(),
        _make_job(),
    )
    assert len(sidecar.metadata_attributes) <= MAX_SIDECAR_METADATA_KEYS


def test_total_optional_field_count_does_not_overflow_budget() -> None:
    """Sanity guard: required + all optionals = 9, which fits in 10."""
    assert len(REQUIRED_ATTRIBUTE_KEYS) + len(OPTIONAL_ATTRIBUTE_KEYS) == 9
    assert (
        len(REQUIRED_ATTRIBUTE_KEYS) + len(OPTIONAL_ATTRIBUTE_KEYS)
        <= MAX_SIDECAR_METADATA_KEYS
    )


def test_key_count_overflow_raises_with_dropped_field_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forcing a tighter budget surfaces SidecarValidationError listing dropped fields."""
    # Tighten the budget to 5 so that 3 required + 6 optional overflows by 4.
    import zubot_ingestion.domain.pipeline.sidecar as sidecar_module

    monkeypatch.setattr(sidecar_module, "MAX_SIDECAR_METADATA_KEYS", 5)

    builder = SidecarBuilder()
    with pytest.raises(SidecarValidationError) as exc_info:
        builder.build(
            _make_full_extraction_result(),
            _make_companion_result(),
            _make_job(),
        )

    err = exc_info.value
    # 3 required + 2 optional fit, 4 must drop. The lower-priority optionals
    # are dropped first (revision, building_zone, project, ...).
    assert len(err.dropped_fields) == 4
    assert "project" in err.dropped_fields
    assert "building_zone" in err.dropped_fields
    assert "revision" in err.dropped_fields
    # The exception message names the dropped fields.
    msg = str(err)
    assert "project" in msg
    assert "building_zone" in msg
    assert "drawing_number" not in err.dropped_fields  # higher priority kept
    assert "title" not in err.dropped_fields


def test_key_count_overflow_drops_lower_priority_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Field priority order is: drawing_number > title > discipline > revision > building_zone > project."""
    import zubot_ingestion.domain.pipeline.sidecar as sidecar_module

    monkeypatch.setattr(sidecar_module, "MAX_SIDECAR_METADATA_KEYS", 4)

    builder = SidecarBuilder()
    with pytest.raises(SidecarValidationError) as exc_info:
        builder.build(
            _make_full_extraction_result(),
            None,
            _make_job(),
        )

    dropped = exc_info.value.dropped_fields
    # Budget = 4, required = 3, so 1 optional fits. drawing_number is highest
    # priority and should be the one kept; the other 5 should be dropped.
    assert len(dropped) == 5
    assert "drawing_number" not in dropped
    assert set(dropped) == {"title", "discipline", "revision", "building_zone", "project"}


# ---------------------------------------------------------------------------
# JSON Schema validation failure
# ---------------------------------------------------------------------------


def test_schema_validation_failure_raises_sidecar_validation_error() -> None:
    """A directly malformed metadata dict surfaces as SidecarValidationError."""
    bad_metadata = {
        "source_filename": "test.pdf",
        "document_type": "electrical_schematic",
        "extraction_confidence": "not-a-number",  # WRONG TYPE
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_sidecar_metadata(bad_metadata)


def test_schema_rejects_additional_properties() -> None:
    bad_metadata = {
        "source_filename": "test.pdf",
        "document_type": "specification",
        "extraction_confidence": 0.5,
        "rogue_field": "should not be allowed",
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_sidecar_metadata(bad_metadata)


def test_schema_rejects_missing_required_field() -> None:
    bad_metadata = {
        # source_filename is missing
        "document_type": "specification",
        "extraction_confidence": 0.5,
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_sidecar_metadata(bad_metadata)


def test_schema_rejects_out_of_range_confidence() -> None:
    bad_metadata = {
        "source_filename": "test.pdf",
        "document_type": "specification",
        "extraction_confidence": 1.5,  # > 1.0
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_sidecar_metadata(bad_metadata)


def test_schema_rejects_empty_source_filename() -> None:
    bad_metadata = {
        "source_filename": "",
        "document_type": "specification",
        "extraction_confidence": 0.5,
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_sidecar_metadata(bad_metadata)


def test_builder_translates_schema_failure_to_sidecar_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the assembled metadata fails schema validation, the builder
    surfaces a SidecarValidationError that wraps the underlying jsonschema
    error rather than letting the raw jsonschema exception leak out."""
    import zubot_ingestion.domain.pipeline.sidecar as sidecar_module

    # Inject a bogus schema that requires a key the builder will never set,
    # so any assembled sidecar fails validation.
    bogus_schema: dict[str, Any] = {
        "type": "object",
        "required": ["NEVER_PRESENT"],
        "properties": {"NEVER_PRESENT": {"type": "string"}},
        "additionalProperties": True,
    }
    monkeypatch.setattr(sidecar_module, "SIDECAR_JSON_SCHEMA", bogus_schema)

    builder = SidecarBuilder()
    with pytest.raises(SidecarValidationError) as exc_info:
        builder.build(
            _make_full_extraction_result(),
            _make_companion_result(),
            _make_job(),
        )

    err = exc_info.value
    assert err.schema_error is not None
    assert isinstance(err.schema_error, jsonschema.ValidationError)
    assert "NEVER_PRESENT" in str(err)


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


def test_schema_marks_three_required_fields() -> None:
    assert set(SIDECAR_JSON_SCHEMA["required"]) == {
        "source_filename",
        "document_type",
        "extraction_confidence",
    }


def test_schema_disallows_additional_properties() -> None:
    assert SIDECAR_JSON_SCHEMA.get("additionalProperties") is False


def test_schema_property_set_matches_required_plus_optional() -> None:
    expected = set(REQUIRED_ATTRIBUTE_KEYS) | set(OPTIONAL_ATTRIBUTE_KEYS)
    assert set(SIDECAR_JSON_SCHEMA["properties"].keys()) == expected


def test_schema_extraction_confidence_is_number_with_unit_interval() -> None:
    prop = SIDECAR_JSON_SCHEMA["properties"]["extraction_confidence"]
    assert prop["type"] == "number"
    assert prop["minimum"] == 0.0
    assert prop["maximum"] == 1.0
