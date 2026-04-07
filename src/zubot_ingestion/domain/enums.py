"""Domain enums — controlled vocabularies for the Zubot Smart Ingestion Service.

This module defines the canonical enum values used across all layers. Enum values
are authoritative: they are sourced from the boundary contracts document
(phase2/boundary-contracts.md section 5) and the reference architecture
Controlled Vocabularies appendix (phase1/reference-architecture.md).

Per the dependency rules, this module may only import from stdlib.
"""

from __future__ import annotations

from enum import Enum


class JobStatus(str, Enum):
    """Lifecycle status for an extraction job.

    Sourced from boundary-contracts.md §5 (Shared Types Catalog).
    """

    QUEUED = "queued"  # In Celery queue, not yet picked up
    PROCESSING = "processing"  # Worker executing pipeline
    COMPLETED = "completed"  # Successfully extracted, confidence >= review threshold
    FAILED = "failed"  # Unrecoverable error
    REVIEW = "review"  # Confidence < review threshold, needs human
    REJECTED = "rejected"  # Human rejected the result
    CACHED = "cached"  # Returned from dedup cache (duplicate file_hash)


class ExtractionMode(str, Enum):
    """Extraction mode selection for batch submission.

    Sourced from boundary-contracts.md §5 (Shared Types Catalog).
    """

    AUTO = "auto"  # Auto-select best strategy based on PDF content
    DRAWING = "drawing"  # Force drawing-centric extraction (vision-first)
    TITLE = "title"  # Force title-centric extraction (text-first)


class ConfidenceTier(str, Enum):
    """Confidence tier assigned after weighted scoring.

    Thresholds are defined in boundary-contracts.md §4.12 (Confidence Calculator).
    """

    AUTO = "auto"  # overall >= 0.8: proceed automatically
    SPOT = "spot"  # 0.5 <= overall < 0.8: spot check recommended
    REVIEW = "review"  # overall < 0.5: human review required


class DocumentType(str, Enum):
    """Controlled vocabulary for document type classification.

    21 values sourced from phase1/reference-architecture.md "Appendix: Controlled
    Vocabularies → Document Type". `DOCUMENT` is the fallback used when the text
    model cannot confidently classify a document.
    """

    TECHNICAL_DRAWING = "technical_drawing"
    FLOOR_PLAN = "floor_plan"
    ELECTRICAL_SCHEMATIC = "electrical_schematic"
    MECHANICAL_DRAWING = "mechanical_drawing"
    PLUMBING_DRAWING = "plumbing_drawing"
    FIRE_SAFETY_PLAN = "fire_safety_plan"
    INSPECTION_REPORT = "inspection_report"
    MAINTENANCE_LOG = "maintenance_log"
    COMPLIANCE_CERTIFICATE = "compliance_certificate"
    EQUIPMENT_MANUAL = "equipment_manual"
    SPECIFICATION = "specification"
    RISK_ASSESSMENT = "risk_assessment"
    SCHEDULE = "schedule"
    COMMISSIONING_REPORT = "commissioning_report"
    WARRANTY_CERTIFICATE = "warranty_certificate"
    PRODUCT_DATA_SHEET = "product_data_sheet"
    SAFETY_DATA_SHEET = "safety_data_sheet"
    TEST_CERTIFICATE = "test_certificate"
    METHOD_STATEMENT = "method_statement"
    AS_BUILT_DRAWING = "as_built_drawing"
    DOCUMENT = "document"  # fallback when classification is uncertain


class Discipline(str, Enum):
    """Engineering discipline classification.

    Sourced from phase1/reference-architecture.md "Appendix: Controlled
    Vocabularies → Discipline". Used by the extraction pipeline to tag
    drawings with their discipline based on prefix patterns and content.
    """

    ARCHITECTURAL = "architectural"
    ELECTRICAL = "electrical"
    MECHANICAL = "mechanical"
    PLUMBING = "plumbing"
    FIRE = "fire"
    STRUCTURAL = "structural"


__all__ = [
    "ConfidenceTier",
    "Discipline",
    "DocumentType",
    "ExtractionMode",
    "JobStatus",
]
