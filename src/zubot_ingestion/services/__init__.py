"""Application services for the Zubot Smart Ingestion Service.

Application services that orchestrate domain logic and coordinate
with infrastructure via protocol interfaces. This layer depends on
domain protocols/entities and must not import concrete infrastructure
implementations (except in dependency-injection composition roots).

This module is the single composition root for the application-layer
services. It is the ONLY place that imports concrete infrastructure
adapters (PDF processor, Ollama client, repository) — the rest of the
domain and service code depends on the abstract protocols declared in
:mod:`zubot_ingestion.domain.protocols`.

Two factory functions are exported:

* :func:`build_orchestrator` — wire up the full :class:`ExtractionOrchestrator`
  with its Stage 1 extractors, sidecar builder, confidence calculator, and
  PDF processor. Called by the Celery worker at task-start time.
* :func:`get_job_repository` — return the configured :class:`IJobRepository`
  implementation. Called by the Celery worker to load and persist jobs.

The factories perform their infrastructure imports lazily so that merely
importing ``zubot_ingestion.services`` does not drag in every adapter at
module-load time — useful for test runs that only want the domain layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type-checking only
    from zubot_ingestion.domain.protocols import (
        IJobRepository,
        IOrchestrator,
    )


def build_orchestrator() -> "IOrchestrator":
    """Construct a fully-wired :class:`ExtractionOrchestrator`.

    The orchestrator is stateless apart from its injected dependencies, so
    callers may either cache the instance or rebuild it per task. This
    factory rebuilds it per call to keep dependency lifetimes simple.
    """
    # Lazy imports: keep infrastructure modules off the import graph of
    # plain ``import zubot_ingestion.services``. This matters because the
    # Celery worker's eager mode imports the services package very early.
    from zubot_ingestion.config import get_settings
    from zubot_ingestion.domain.pipeline.confidence import ConfidenceCalculator
    from zubot_ingestion.domain.pipeline.extractors.document_type import (
        DocumentTypeExtractor,
    )
    from zubot_ingestion.domain.pipeline.extractors.drawing_number import (
        DrawingNumberExtractor,
    )
    from zubot_ingestion.domain.pipeline.extractors.filename_parser import (
        FilenameParser,
    )
    from zubot_ingestion.domain.pipeline.extractors.title import TitleExtractor
    from zubot_ingestion.domain.pipeline.json_parser import JsonResponseParser
    from zubot_ingestion.domain.pipeline.sidecar import SidecarBuilder
    from zubot_ingestion.infrastructure.chromadb.writer import (
        ChromaDBMetadataWriter,
    )
    from zubot_ingestion.infrastructure.ollama.client import OllamaClient
    from zubot_ingestion.infrastructure.pdf.processor import PyMuPDFProcessor
    from zubot_ingestion.services.orchestrator import ExtractionOrchestrator

    settings = get_settings()

    pdf_processor = PyMuPDFProcessor()
    ollama_client = OllamaClient()
    response_parser = JsonResponseParser()
    filename_parser = FilenameParser()

    drawing_number_extractor = DrawingNumberExtractor(
        pdf_processor=pdf_processor,
        ollama_client=ollama_client,
        response_parser=response_parser,
        filename_parser=filename_parser,
    )
    title_extractor = TitleExtractor(
        pdf_processor=pdf_processor,
        ollama_client=ollama_client,
        response_parser=response_parser,
    )
    document_type_extractor = DocumentTypeExtractor(
        pdf_processor=pdf_processor,
        ollama_client=ollama_client,
        response_parser=response_parser,
    )

    sidecar_builder = SidecarBuilder()
    confidence_calculator = ConfidenceCalculator()

    metadata_writer = ChromaDBMetadataWriter(
        host=settings.CHROMADB_HOST,
        port=settings.CHROMADB_PORT,
    )

    return ExtractionOrchestrator(
        drawing_number_extractor=drawing_number_extractor,
        title_extractor=title_extractor,
        document_type_extractor=document_type_extractor,
        sidecar_builder=sidecar_builder,
        confidence_calculator=confidence_calculator,
        pdf_processor=pdf_processor,
        metadata_writer=metadata_writer,
    )


def get_job_repository() -> "IJobRepository":
    """Return the configured :class:`IJobRepository` implementation.

    Lazily imports the concrete repository so the services package can
    still be imported in environments without ``asyncpg`` / SQLAlchemy.
    """
    from zubot_ingestion.config import get_settings
    from zubot_ingestion.infrastructure.database.repository import (
        JobRepository,
    )

    settings = get_settings()
    return JobRepository(database_url=settings.DATABASE_URL)


__all__ = ["build_orchestrator", "get_job_repository"]
