"""Domain enums (transitional stub).

Only enums required by the database repository module are defined here.
Canonical enums are produced by task-2 (contracts-and-types-definition);
on merge, the canonical versions must overwrite this file.
"""

from __future__ import annotations

from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class BatchStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class ExtractionMode(str, Enum):
    FAST = "fast"
    ACCURATE = "accurate"


class ConfidenceTier(str, Enum):
    AUTO = "auto"
    SPOT = "spot"
    REVIEW = "review"


class ReviewActionType(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
