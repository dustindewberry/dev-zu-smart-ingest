"""Transitional minimal stub of zubot_ingestion.shared.types.

Contains ONLY the type aliases required by this worker's task (step-15
sidecar-builder). The canonical, full-featured types module is produced by
task-2 (bright-pike worktree) and is a strict superset of this stub.

On merge, the canonical module from task-2 MUST overwrite this stub. The
type aliases here are byte-identical to the canonical version so behavior
is unchanged either way.
"""

from __future__ import annotations

from typing import NewType
from uuid import UUID

JobId = NewType("JobId", UUID)
BatchId = NewType("BatchId", UUID)
FileHash = NewType("FileHash", str)  # SHA-256 hex string

__all__ = ["BatchId", "FileHash", "JobId"]
