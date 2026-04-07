"""Minimal stub of zubot_ingestion.shared.types for the OTEL task.

TRANSITIONAL stub created by silver-falcon (task-22 / step-22). The canonical
full module is produced by task-2 (bright-pike worktree). On merge, the
canonical task-2 shared/types.py MUST overwrite this file. The definitions
here are strict subsets of the canonical version.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType
from uuid import UUID

# Branded type aliases (byte-identical to canonical bright-pike).
JobId = NewType("JobId", UUID)
BatchId = NewType("BatchId", UUID)
FileHash = NewType("FileHash", str)


@dataclass(frozen=True)
class PipelineError:
    """An error that occurred during a pipeline stage."""

    stage: str
    error_type: str
    message: str
    recoverable: bool


__all__ = [
    "BatchId",
    "FileHash",
    "JobId",
    "PipelineError",
]
