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

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:  # pragma: no cover - type-checking only
    from zubot_ingestion.domain.protocols import IOrchestrator
    from zubot_ingestion.infrastructure.database.repository import JobRepository


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
    from zubot_ingestion.domain.pipeline.companion import CompanionGenerator
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
    from zubot_ingestion.domain.pipeline.validation import (
        build_companion_validator,
    )
    from zubot_ingestion.infrastructure.callback import build_callback_client
    from zubot_ingestion.infrastructure.chromadb.writer import (
        ChromaDBMetadataWriter,
    )
    from zubot_ingestion.infrastructure.elasticsearch import (
        build_search_indexer,
    )
    from zubot_ingestion.infrastructure.ollama.client import OllamaClient
    from zubot_ingestion.infrastructure.pdf.processor import PyMuPDFProcessor
    from zubot_ingestion.services.orchestrator import ExtractionOrchestrator

    settings = get_settings()

    pdf_processor = PyMuPDFProcessor()
    # Pool limits + retry budget flow through the shared Settings
    # instance constructed above — OllamaClient reads the pool /
    # timeout / retry fields directly from it so operators can scale
    # the client via environment variables alone.
    ollama_client = OllamaClient(
        base_url=settings.OLLAMA_HOST,
        settings=settings,
    )
    response_parser = JsonResponseParser()
    filename_parser = FilenameParser()

    # Source the Ollama model names from Settings so operators can swap
    # the vision + text models at deploy time via OLLAMA_VISION_MODEL /
    # OLLAMA_TEXT_MODEL environment variables. The extractors store the
    # kwargs on self and forward them on every generate_vision /
    # generate_text call.
    vision_model = settings.OLLAMA_VISION_MODEL
    text_model = settings.OLLAMA_TEXT_MODEL

    drawing_number_extractor = DrawingNumberExtractor(
        pdf_processor=pdf_processor,
        ollama_client=ollama_client,
        response_parser=response_parser,
        filename_parser=filename_parser,
        vision_model=vision_model,
        text_model=text_model,
    )
    title_extractor = TitleExtractor(
        pdf_processor=pdf_processor,
        ollama_client=ollama_client,
        response_parser=response_parser,
        filename_parser=filename_parser,
        vision_model=vision_model,
        text_model=text_model,
    )
    document_type_extractor = DocumentTypeExtractor(
        pdf_processor=pdf_processor,
        ollama_client=ollama_client,
        response_parser=response_parser,
        filename_parser=filename_parser,
        text_model=text_model,
    )

    sidecar_builder = SidecarBuilder()
    confidence_calculator = ConfidenceCalculator()

    companion_generator = CompanionGenerator(
        pdf_processor=pdf_processor,
        ollama_client=ollama_client,
        response_parser=response_parser,
        settings=settings,
    )

    metadata_writer = ChromaDBMetadataWriter(
        host=settings.CHROMADB_HOST,
        port=settings.CHROMADB_PORT,
    )

    companion_validator = build_companion_validator()
    search_indexer = build_search_indexer(settings)
    callback_client = build_callback_client(settings)

    return ExtractionOrchestrator(
        drawing_number_extractor=drawing_number_extractor,
        title_extractor=title_extractor,
        document_type_extractor=document_type_extractor,
        sidecar_builder=sidecar_builder,
        confidence_calculator=confidence_calculator,
        pdf_processor=pdf_processor,
        companion_generator=companion_generator,
        metadata_writer=metadata_writer,
        companion_validator=companion_validator,
        search_indexer=search_indexer,
        callback_client=callback_client,
    )


@asynccontextmanager
async def get_job_repository() -> "AsyncIterator[JobRepository]":
    """Yield a :class:`JobRepository` bound to a fresh async session.

    This is the canonical composition-root factory for the job repository.
    The concrete :class:`JobRepository` takes an :class:`AsyncSession` in
    its constructor, so the factory opens a session via ``get_session()``
    and yields the repository to the caller. On exit the session is
    committed (or rolled back on exception) and closed by ``get_session``.

    Callers must use ``async with``::

        async with get_job_repository() as repo:
            await repo.get_job(job_id)

    Lazily imports the concrete repository so the services package can
    still be imported in environments without ``asyncpg`` / SQLAlchemy.
    """
    from zubot_ingestion.infrastructure.database.repository import (
        JobRepository,
    )
    from zubot_ingestion.infrastructure.database.session import get_session

    async with get_session() as session:
        yield JobRepository(session)


__all__ = ["build_orchestrator", "get_job_repository"]
