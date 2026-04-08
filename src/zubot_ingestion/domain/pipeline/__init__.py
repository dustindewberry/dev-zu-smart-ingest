"""Extraction pipeline.

Three-stage pipeline for metadata extraction from construction PDFs:
    Stage 1 - Extract (drawing number, title, document type)
    Stage 2 - Companion (visual description generation)
    Stage 3 - Sidecar (metadata assembly and validation)
"""

from zubot_ingestion.domain.pipeline.validation import CompanionValidator

__all__ = ["CompanionValidator"]
