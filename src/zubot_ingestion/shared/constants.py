"""Single source of truth for cross-layer string and numeric constants.

This is a partial subset of the canonical constants module created by the
shared-constants worker. It contains only the symbols required by the
infrastructure/ollama/* code in this worktree. When this branch is merged
with the canonical constants worker output, the canonical version is
authoritative for any overlapping symbols (the values here MUST match it).

This module has ZERO runtime dependencies on other zubot_ingestion modules
so it can be safely imported anywhere in the dependency graph.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Service identifiers
# ---------------------------------------------------------------------------

SERVICE_NAME: str = "zubot-ingestion"
SERVICE_VERSION: str = "0.1.0"


# ---------------------------------------------------------------------------
# Ollama models and inference defaults
# ---------------------------------------------------------------------------

OLLAMA_MODEL_VISION: str = "qwen2.5vl:7b"
OLLAMA_MODEL_TEXT: str = "qwen2.5:7b"
OLLAMA_TEMPERATURE_DETERMINISTIC: float = 0.0


__all__ = [
    "SERVICE_NAME",
    "SERVICE_VERSION",
    "OLLAMA_MODEL_VISION",
    "OLLAMA_MODEL_TEXT",
    "OLLAMA_TEMPERATURE_DETERMINISTIC",
]
