"""Transitional minimal stub of zubot_ingestion.shared.constants.

Contains ONLY the symbols required by this worker's task (step-15
sidecar-builder). The canonical, full-featured constants module is produced
by task-26 (witty-maple worktree) and is a strict superset of this stub.

On merge, the canonical module from task-26 MUST overwrite this stub. The
values here are byte-identical to the canonical version so behavior is
unchanged either way.
"""

from __future__ import annotations

# AWS Bedrock Knowledge Base imposes a maximum of 10 metadata attributes per
# document. The sidecar builder enforces this limit before persistence.
MAX_SIDECAR_METADATA_KEYS: int = 10

__all__ = ["MAX_SIDECAR_METADATA_KEYS"]
