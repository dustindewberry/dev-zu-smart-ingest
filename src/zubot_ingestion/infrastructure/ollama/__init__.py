"""Ollama HTTP client adapter (implements IOllamaClient).

Exports:
    OllamaClient — concrete httpx-based implementation.
    OllamaError — exception raised on persistent transport failure.
"""

from zubot_ingestion.infrastructure.ollama.client import OllamaClient, OllamaError

__all__ = ["OllamaClient", "OllamaError"]
