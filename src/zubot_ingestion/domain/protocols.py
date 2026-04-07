"""Domain protocols (interfaces) for the Zubot Smart Ingestion Service.

This module is a minimal subset containing only the ``IOllamaClient``
protocol required by the infrastructure/ollama/* code in this worktree.
The canonical definitions live in the contracts-and-types worker output
(task-2) and are authoritative for any overlapping symbols.

Per the dependency rules (boundary-contracts.md §3), this module may only
import from stdlib, `zubot_ingestion.shared.types`, and
`zubot_ingestion.domain.entities`. It MUST NOT import from api, services,
or infrastructure.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from zubot_ingestion.domain.entities import OllamaResponse


@runtime_checkable
class IOllamaClient(Protocol):
    """Ollama API client for vision and text inference (§4.16)."""

    async def generate_vision(
        self,
        image_base64: str,
        prompt: str,
        model: str = "qwen2.5vl:7b",
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
    ) -> OllamaResponse:
        """Generate response from vision model with image input."""
        ...

    async def generate_text(
        self,
        text: str,
        prompt: str,
        model: str = "llama3.1:8b",
        temperature: float = 0.0,
        timeout_seconds: float = 30.0,
    ) -> OllamaResponse:
        """Generate response from text model."""
        ...

    async def check_model_available(self, model: str) -> bool:
        """Check if a model is loaded and available."""
        ...


__all__ = ["IOllamaClient"]
