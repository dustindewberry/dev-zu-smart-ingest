"""Minimal stub of zubot_ingestion.shared.constants for the witty-atlas
worktree (task-23 / step-23 prometheus-metrics).

This worktree was branched from the initial commit and does not see the
canonical full constants module produced by task-26 (witty-maple). The
canonical module is a strict superset of this stub. On merge, the
canonical version from task-26 MUST overwrite this file.

The values defined here are byte-identical to the canonical task-26
values for the symbols used by the metrics layer:

    SERVICE_NAME, SERVICE_VERSION
    METRIC_EXTRACTION_TOTAL, METRIC_EXTRACTION_DURATION,
    METRIC_CONFIDENCE_SCORE, METRIC_QUEUE_DEPTH,
    METRIC_OLLAMA_REQUESTS, METRIC_OLLAMA_DURATION
    AUTH_EXEMPT_PATHS
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Service identifiers
# ---------------------------------------------------------------------------

SERVICE_NAME: str = "zubot-ingestion"
SERVICE_VERSION: str = "0.1.0"


# ---------------------------------------------------------------------------
# Prometheus metric names
# ---------------------------------------------------------------------------

METRIC_EXTRACTION_TOTAL: str = "zubot_extraction_total"
METRIC_EXTRACTION_DURATION: str = "zubot_extraction_duration_seconds"
METRIC_CONFIDENCE_SCORE: str = "zubot_confidence_score"
METRIC_QUEUE_DEPTH: str = "zubot_queue_depth"
METRIC_OLLAMA_REQUESTS: str = "zubot_ollama_requests_total"
METRIC_OLLAMA_DURATION: str = "zubot_ollama_duration_seconds"


# ---------------------------------------------------------------------------
# Auth-exempt routes
# ---------------------------------------------------------------------------

AUTH_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/redoc",
    }
)


__all__ = [
    "SERVICE_NAME",
    "SERVICE_VERSION",
    "METRIC_EXTRACTION_TOTAL",
    "METRIC_EXTRACTION_DURATION",
    "METRIC_CONFIDENCE_SCORE",
    "METRIC_QUEUE_DEPTH",
    "METRIC_OLLAMA_REQUESTS",
    "METRIC_OLLAMA_DURATION",
    "AUTH_EXEMPT_PATHS",
]
