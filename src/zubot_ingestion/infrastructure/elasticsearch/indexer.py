"""Elasticsearch adapter — implements ISearchIndexer (CAP-024).

:class:`ElasticsearchSearchIndexer` indexes Stage 2 companion documents
into Elasticsearch so the review queue UI and downstream search services
can perform full-text lookups by drawing number, title, or prose content.

Layering: infrastructure adapter. May import from stdlib, httpx, the
:mod:`zubot_ingestion.shared`, :mod:`zubot_ingestion.config`, and
:mod:`zubot_ingestion.domain.protocols` modules. MUST NOT import from
:mod:`zubot_ingestion.services` or :mod:`zubot_ingestion.api`.

The adapter never raises out of :meth:`index_companion` —  it returns
``False`` on error so the orchestrator can record the failure in its
pipeline_trace and continue. This matches the "degrade gracefully" rule
used by every other cross-cutting adapter in the service.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from zubot_ingestion.domain.entities import CompanionResult, ExtractionResult
from zubot_ingestion.domain.protocols import ISearchIndexer

__all__ = [
    "ElasticsearchSearchIndexer",
    "NoOpSearchIndexer",
    "build_search_indexer",
]

_LOG = logging.getLogger(__name__)

#: Default index name used when no deployment_id is supplied.
DEFAULT_INDEX_NAME: str = "zubot_companion_default"

#: Index name prefix — combined with the deployment_id to form the
#: per-deployment index (e.g. ``zubot_companion_42``).
INDEX_NAME_PREFIX: str = "zubot_companion_"


def _index_name_for(deployment_id: int | None) -> str:
    """Return the Elasticsearch index name for a given deployment."""
    if deployment_id is None:
        return DEFAULT_INDEX_NAME
    return f"{INDEX_NAME_PREFIX}{int(deployment_id)}"


class ElasticsearchSearchIndexer(ISearchIndexer):
    """Concrete ISearchIndexer backed by Elasticsearch over HTTP."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._log = logger or _LOG

    async def index_companion(
        self,
        document_id: str,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult,
        deployment_id: int | None,
    ) -> bool:
        """PUT the companion document to Elasticsearch.

        Target URL: ``{base_url}/{index_name}/_doc/{document_id}``

        Returns True on 2xx, False otherwise. Never raises.
        """
        index_name = _index_name_for(deployment_id)
        url = f"{self._base_url}/{index_name}/_doc/{document_id}"

        body: dict[str, Any] = {
            "document_id": document_id,
            "drawing_number": extraction_result.drawing_number,
            "title": extraction_result.title,
            "document_type": (
                extraction_result.document_type.value
                if extraction_result.document_type is not None
                else None
            ),
            "confidence_score": extraction_result.confidence_score,
            "confidence_tier": (
                extraction_result.confidence_tier.value
                if extraction_result.confidence_tier is not None
                else None
            ),
            "companion_text": companion_result.companion_text,
            "pages_described": companion_result.pages_described,
            "deployment_id": deployment_id,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.put(url, json=body)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            self._log.warning(
                "elasticsearch_index_failed",
                extra={
                    "document_id": document_id,
                    "url": url,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return False

        if 200 <= response.status_code < 300:
            return True

        self._log.warning(
            "elasticsearch_index_non_2xx",
            extra={
                "document_id": document_id,
                "url": url,
                "status_code": response.status_code,
            },
        )
        return False

    async def check_connection(self) -> bool:
        """GET ``/_cluster/health``; return True on 2xx."""
        url = f"{self._base_url}/_cluster/health"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            self._log.warning(
                "elasticsearch_health_check_failed",
                extra={"url": url, "error": f"{type(exc).__name__}: {exc}"},
            )
            return False
        return 200 <= response.status_code < 300


class NoOpSearchIndexer(ISearchIndexer):
    """No-op fallback ISearchIndexer used when Elasticsearch is unavailable.

    Every :meth:`index_companion` call logs a debug line and returns
    ``True``. This is the safe default in local/dev environments where
    no Elasticsearch cluster is configured — the pipeline keeps running
    and nothing gets indexed.
    """

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._log = logger or _LOG

    async def index_companion(
        self,
        document_id: str,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult,
        deployment_id: int | None,
    ) -> bool:
        self._log.debug(
            "noop_search_indexer_index_companion",
            extra={"document_id": document_id, "deployment_id": deployment_id},
        )
        return True

    async def check_connection(self) -> bool:
        return True


def build_search_indexer(settings: Any) -> ISearchIndexer:
    """Factory: return a configured ISearchIndexer.

    If ``settings.ELASTICSEARCH_URL`` is set to a truthy value, returns
    a real :class:`ElasticsearchSearchIndexer`. Otherwise returns a
    :class:`NoOpSearchIndexer` so the composition root is always fully
    wired.
    """
    url = getattr(settings, "ELASTICSEARCH_URL", None)
    if not url:
        return NoOpSearchIndexer()
    return ElasticsearchSearchIndexer(base_url=str(url))
