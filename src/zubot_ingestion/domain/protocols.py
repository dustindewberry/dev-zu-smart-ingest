"""Minimal stub of zubot_ingestion.domain.protocols for the OTEL task.

TRANSITIONAL stub created by silver-falcon (task-22 / step-22). The canonical
full protocols module is produced by task-2 (bright-pike worktree). On merge,
the canonical task-2 protocols.py MUST overwrite this file. The Protocol
method signatures here are byte-identical to the canonical version for the
interfaces this task needs.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from zubot_ingestion.domain.entities import (
    CompanionResult,
    ConfidenceAssessment,
    ExtractionResult,
    Job,
    PDFData,
    PipelineContext,
    PipelineResult,
    SidecarDocument,
    ValidationResult,
)


@runtime_checkable
class IPDFProcessor(Protocol):
    def load(self, pdf_bytes: bytes) -> PDFData: ...


@runtime_checkable
class IExtractor(Protocol):
    async def extract(self, context: PipelineContext) -> ExtractionResult: ...


@runtime_checkable
class ISidecarBuilder(Protocol):
    def build(
        self,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult | None,
        job: Job,
    ) -> SidecarDocument: ...


@runtime_checkable
class IConfidenceCalculator(Protocol):
    def calculate(
        self,
        extraction_result: ExtractionResult,
        validation_result: ValidationResult | None,
    ) -> ConfidenceAssessment: ...


@runtime_checkable
class IOrchestrator(Protocol):
    async def run_pipeline(
        self,
        job: Job,
        pdf_bytes: bytes,
    ) -> PipelineResult: ...


__all__ = [
    "IConfidenceCalculator",
    "IExtractor",
    "IOrchestrator",
    "IPDFProcessor",
    "ISidecarBuilder",
]
