"""Domain entities for the Zubot Smart Ingestion Service (partial subset).

This module is a minimal subset containing only the ``OllamaResponse``
entity required by the infrastructure/ollama/* code in this worktree.
The canonical definition lives in the contracts-and-types worker output
(task-2) and is authoritative for any overlapping symbols.

Per the dependency rules (boundary-contracts.md §3), this module may only
import from stdlib, `zubot_ingestion.shared.types`, and
`zubot_ingestion.domain.enums`. It MUST NOT import from api, services, or
infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class OllamaResponse:
    """Response returned by IOllamaClient.generate_vision/generate_text().

    Fields mirror the Ollama ``/api/generate`` JSON response envelope:
        response_text: Model-produced text (``response`` field of the
            upstream JSON payload).
        model: Name of the model that produced this response.
        prompt_eval_count: Number of prompt tokens evaluated, if reported.
        eval_count: Number of tokens generated, if reported.
        total_duration_ns: End-to-end duration of the request in
            nanoseconds, as reported by Ollama.
        raw: The full decoded JSON body for debugging / trace capture.
    """

    response_text: str
    model: str
    prompt_eval_count: int | None
    eval_count: int | None
    total_duration_ns: int | None
    raw: dict[str, Any] = field(default_factory=dict)


__all__ = ["OllamaResponse"]
