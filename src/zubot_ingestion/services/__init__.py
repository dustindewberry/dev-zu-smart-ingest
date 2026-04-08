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

The following factory functions are exported:

* :func:`build_orchestrator` — wire up the full :class:`ExtractionOrchestrator`
  with its Stage 1 extractors, sidecar builder, confidence calculator, and
  PDF processor. Called by the Celery worker at task-start time.
* :func:`get_job_repository` — async context manager that yields an
  :class:`IJobRepository` bound to a fresh async session. Use
  ``async with get_job_repository() as repo:`` at every call site.
* :func:`build_companion_validator` — construct a
  :class:`CompanionValidator` (CAP-018 / step-19).
* :func:`build_search_indexer` — construct an
  :class:`ElasticsearchSearchIndexer` (CAP-024 / step-21).
* :func:`build_callback_client` — construct a :class:`CallbackHttpClient`
  (CAP-025 / step-21).

The factories perform their infrastructure imports lazily so that merely
importing ``zubot_ingestion.services`` does not drag in every adapter at
module-load time — useful for test runs that only want the domain layer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type-checking only
    from zubot_ingestion.domain.pipeline.validation import CompanionValidator
    from zubot_ingestion.domain.protocols import IOrchestrator
    from zubot_ingestion.infrastructure.callback import CallbackHttpClient
    from zubot_ingestion.infrastructure.database.repository import JobRepository
    from zubot_ingestion.infrastructure.elasticsearch import (
        ElasticsearchSearchIndexer,
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

    companion_generator = CompanionGenerator(
        pdf_processor=pdf_processor,
        ollama_client=ollama_client,
        response_parser=response_parser,
    )

    metadata_writer = ChromaDBMetadataWriter(
        host=settings.CHROMADB_HOST,
        port=settings.CHROMADB_PORT,
    )

    # CAP-018 / CAP-024 / CAP-025: construct and inject the three
    # previously-unwired downstream adapters. Each factory is guarded by
    # a try/except so a misconfigured ElasticsearchSearchIndexer URL (for
    # example) does not prevent the orchestrator from building — the
    # orchestrator treats each adapter as optional and degrades
    # gracefully if it is not present.
    try:
        companion_validator = build_companion_validator()
    except Exception:  # noqa: BLE001 - degrade gracefully
        companion_validator = None  # type: ignore[assignment]

    try:
        search_indexer = build_search_indexer()
    except Exception:  # noqa: BLE001 - degrade gracefully
        search_indexer = None  # type: ignore[assignment]

    try:
        callback_client = build_callback_client()
    except Exception:  # noqa: BLE001 - degrade gracefully
        callback_client = None  # type: ignore[assignment]

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
async def get_job_repository() -> AsyncIterator["JobRepository"]:
    """Yield a :class:`JobRepository` bound to a fresh async session.

    This is the composition root for the job repository. The previous
    synchronous factory constructed ``JobRepository(database_url=...)``
    which does NOT match the repository's actual
    ``__init__(self, session: AsyncSession)`` signature and raised
    ``TypeError`` on the first call. Converting to an
    ``@asynccontextmanager`` fixes the drift and also guarantees the
    underlying session is committed / rolled back / closed correctly.

    Usage::

        async with get_job_repository() as repo:
            job = await repo.get_job(job_id)

    TODO(task-6): there are still synchronous callers of this function
    outside ``services/__init__.py`` that will TypeError at runtime until
    they are converted to the ``async with`` form. Known call sites:

    * ``services/celery_app.py:231`` — ``repo = get_job_repository()``
      inside ``_mark_job_failed``
    * ``services/celery_app.py:250`` — ``repo = get_job_repository()``
      inside ``_run_extract_document_task``
    * ``tests/unit/test_celery_extract_task.py`` — several
      ``unittest.mock.patch("zubot_ingestion.services.get_job_repository",
      return_value=repo)`` call sites whose ``return_value`` must become
      an async-context-manager mock once task 6 converts the callers.

    Task 6 owns the ``celery_app.py`` call-site conversions, and task 9's
    regression tests will catch any remaining synchronous callers.
    """
    # Lazy imports: keep infrastructure modules off the import graph of
    # plain ``import zubot_ingestion.services``.
    from zubot_ingestion.infrastructure.database.repository import JobRepository
    from zubot_ingestion.infrastructure.database.session import get_session

    async with get_session() as session:
        yield JobRepository(session)


def build_companion_validator() -> "CompanionValidator":
    """Construct a :class:`CompanionValidator` (CAP-018 / step-19).

    The validator has no external dependencies — it consumes an
    extraction result and the companion text and returns a
    :class:`ValidationResult`. The orchestrator will be wired to consume
    it in a follow-up task; for now this factory exists so callers
    (currently the Celery task layer and tests) can construct it from a
    single composition-root entry point.
    """
    from zubot_ingestion.domain.pipeline.validation import CompanionValidator

    return CompanionValidator()


def build_search_indexer() -> "ElasticsearchSearchIndexer":
    """Construct an :class:`ElasticsearchSearchIndexer` (CAP-024 / step-21).

    The indexer is configured from ``settings.ELASTICSEARCH_URL`` if
    present on the :class:`Settings` class; otherwise a sensible
    localhost default is used. Optional HTTP basic auth can be supplied
    via ``settings.ELASTICSEARCH_USERNAME`` / ``ELASTICSEARCH_PASSWORD``.

    Index name routing is handled inside the adapter via
    ``_index_name_for(deployment_id)`` — there is NO ``index``
    constructor kwarg on :class:`ElasticsearchSearchIndexer`. Callers
    (orchestrator, Celery task layer) should treat this as a per-request
    client — the underlying HTTP session is cheap to create.
    """
    from zubot_ingestion.config import get_settings
    from zubot_ingestion.infrastructure.elasticsearch import (
        ElasticsearchSearchIndexer,
    )

    settings = get_settings()
    return ElasticsearchSearchIndexer(
        base_url=getattr(settings, "ELASTICSEARCH_URL", "http://localhost:9200"),
        username=getattr(settings, "ELASTICSEARCH_USERNAME", None),
        password=getattr(settings, "ELASTICSEARCH_PASSWORD", None),
    )


def build_callback_client() -> "CallbackHttpClient":
    """Construct a :class:`CallbackHttpClient` (CAP-025 / step-21).

    The callback client has no required constructor arguments — it
    delivers webhook completion notifications to per-job callback URLs
    using an async httpx session with retry/backoff configured inside
    the adapter. Callers should construct it once per Celery task and
    dispose of it when the task ends.
    """
    from zubot_ingestion.infrastructure.callback import CallbackHttpClient

    return CallbackHttpClient()


__all__ = [
    "build_callback_client",
    "build_companion_validator",
    "build_orchestrator",
    "build_search_indexer",
    "get_job_repository",
]
