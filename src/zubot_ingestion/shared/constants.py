"""Minimal stub of zubot_ingestion.shared.constants for the OTEL task.

This is a TRANSITIONAL stub created by the silver-falcon worker (task-22 /
step-22 otel-instrumentation) so that the OTEL instrumentation module and the
OTEL-wrapped orchestrator can be implemented and tested in isolation. The
canonical full-featured constants module is produced by task-26 (witty-maple
worktree, commit 50fe47a) and is a strict superset of this stub. On merge,
the canonical task-26 version MUST overwrite this stub.

All values defined here are byte-identical to the canonical witty-maple
constants module so behavior is unchanged either way.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Service identifiers
# ---------------------------------------------------------------------------

SERVICE_NAME: str = "zubot-ingestion"
SERVICE_VERSION: str = "0.1.0"


# ---------------------------------------------------------------------------
# OpenTelemetry span names
# ---------------------------------------------------------------------------

OTEL_SPAN_BATCH: str = "zubot.extraction.batch"
OTEL_SPAN_JOB: str = "zubot.extraction.job"
OTEL_SPAN_STAGE1_DRAWING_NUMBER: str = "zubot.extraction.stage1.drawing_number"
OTEL_SPAN_STAGE1_TITLE: str = "zubot.extraction.stage1.title"
OTEL_SPAN_STAGE1_DOC_TYPE: str = "zubot.extraction.stage1.document_type"
OTEL_SPAN_STAGE2_COMPANION: str = "zubot.extraction.stage2.companion"
OTEL_SPAN_STAGE3_SIDECAR: str = "zubot.extraction.stage3.sidecar"
OTEL_SPAN_CONFIDENCE: str = "zubot.confidence.calculate"


__all__ = [
    "SERVICE_NAME",
    "SERVICE_VERSION",
    "OTEL_SPAN_BATCH",
    "OTEL_SPAN_JOB",
    "OTEL_SPAN_STAGE1_DRAWING_NUMBER",
    "OTEL_SPAN_STAGE1_TITLE",
    "OTEL_SPAN_STAGE1_DOC_TYPE",
    "OTEL_SPAN_STAGE2_COMPANION",
    "OTEL_SPAN_STAGE3_SIDECAR",
    "OTEL_SPAN_CONFIDENCE",
]
