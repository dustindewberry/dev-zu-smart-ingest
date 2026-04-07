"""Transitional minimal stub of zubot_ingestion.domain.protocols.

Contains ONLY the protocol required by this worker's task (step-15
sidecar-builder). The canonical, full-featured protocols module is produced
by task-2 (bright-pike worktree) and is a strict superset of this stub.

On merge, the canonical module from task-2 MUST overwrite this stub. The
ISidecarBuilder signature here is byte-identical to the canonical version
so behavior is unchanged either way.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from zubot_ingestion.domain.entities import (
    CompanionResult,
    ExtractionResult,
    Job,
    SidecarDocument,
)


@runtime_checkable
class ISidecarBuilder(Protocol):
    """Stage 3: Sidecar document assembly (boundary-contracts §4.11)."""

    def build(
        self,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult | None,
        job: Job,
    ) -> SidecarDocument:
        """Build Bedrock KB sidecar document from pipeline stage results."""
        ...


__all__ = ["ISidecarBuilder"]
