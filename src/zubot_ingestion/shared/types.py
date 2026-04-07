"""Cross-layer DTOs (transitional stub).

NOTE (transitional stub): This worker (task-7 / step-7 celery-configuration)
was branched from the initial commit and therefore could NOT see the canonical
shared/types.py produced by task-2 (commit df79faf, bright-pike worktree).

This file exposes ONLY the symbols needed by task-7's task_queue.py:

    - TaskStatus

The TaskStatus dataclass is byte-identical to the canonical task-2 module so
this stub can be safely overwritten by the canonical superset on merge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class TaskStatus:
    """Status of a Celery task, returned by ITaskQueue.get_task_status()."""

    state: Literal["PENDING", "STARTED", "SUCCESS", "FAILURE", "RETRY"]
    result: Any | None = None
    traceback: str | None = None


__all__ = ["TaskStatus"]
