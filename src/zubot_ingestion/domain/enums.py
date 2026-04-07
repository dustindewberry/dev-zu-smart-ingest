"""Minimal stub of zubot_ingestion.domain.enums for the OTEL task.

TRANSITIONAL stub created by silver-falcon (task-22 / step-22). The canonical
full enums module is produced by task-2 (bright-pike worktree). On merge,
the canonical task-2 enums.py MUST overwrite this file. The enum values
defined here are byte-identical to the canonical version.
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
