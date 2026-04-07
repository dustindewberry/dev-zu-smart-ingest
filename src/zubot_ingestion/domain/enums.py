"""Transitional minimal stub of zubot_ingestion.domain.enums.

Contains ONLY the enum classes required by this worker's task (step-15
sidecar-builder). The canonical, full-featured enums module is produced by
task-2 (bright-pike worktree) and is a strict superset of this stub.

On merge, the canonical module from task-2 MUST overwrite this stub. Enum
values are byte-identical to the canonical version so behavior is unchanged
either way.
"""

from __future__ import annotations

from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    REVIEW = "review"
    REJECTED = "rejected"
    CACHED = "cached"


class ExtractionMode(str, Enum):
    AUTO = "auto"
    DRAWING = "drawing"
    TITLE = "title"


class ConfidenceTier(str, Enum):
    AUTO = "auto"
    SPOT = "spot"
    REVIEW = "review"


class DocumentType(str, Enum):
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
    DOCUMENT = "document"


class Discipline(str, Enum):
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
