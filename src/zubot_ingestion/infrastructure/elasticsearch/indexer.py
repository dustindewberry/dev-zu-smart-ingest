"""Elasticsearch search indexer — concrete implementation of ``ISearchIndexer``.

This module is the only code in the service that talks to Elasticsearch
over HTTP. Callers depend on the ``ISearchIndexer`` protocol and receive
either an :class:`ElasticsearchSearchIndexer` (production) or a
:class:`NoOpSearchIndexer` (local dev / CI where Elasticsearch is not
configured) via dependency injection from the composition root.

Transport
---------
All requests go to ``{base_url}/companions/_doc/{document_id}`` (PUT,
index one document) or ``{base_url}/_cluster/health`` (GET, health
probe) via ``httpx.AsyncClient``. Network failures are caught and
logged as warnings — the adapter NEVER raises out into the extraction
pipeline, because search indexing is a non-blocking enrichment path.

Settings-gated fallback
-----------------------
If ``settings.ELASTICSEARCH_URL`` is unset (``None`` or empty string),
:func:`build_search_indexer` returns a :class:`NoOpSearchIndexer` whose
methods log a debug message and return success without any network I/O.

Implements CAP-024. Protocol contract: ISearchIndexer (§4.19 of
boundary-contracts.md).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from zubot_ingestion.domain.entities import CompanionResult, ExtractionResult

logger = logging.getLogger(__name__)


__all__ = [
    "ElasticsearchSearchIndexer",
    "NoOpSearchIndexer",
    "build_search_indexer",
]


class ElasticsearchSearchIndexer:
    """Concrete ``ISearchIndexer`` that PUTs companion docs into Elasticsearch.

    Parameters
    ----------
    base_url:
        Root URL of the Elasticsearch cluster (e.g.
        ``http://elasticsearch:9200``). Must NOT end with a trailing slash;
        any trailing slash is stripped on construction.
    username:
        Optional HTTP Basic auth user. Used together with ``password``.
    password:
        Optional HTTP Basic auth password. Ignored unless ``username`` is
        also provided.
    timeout:
        Per-request timeout in seconds passed to ``httpx.AsyncClient``.
    verify_certs:
        Whether to verify TLS certificates. Defaults to ``True``. Set to
        ``False`` only for local development against a self-signed cluster.
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 10.0,
        verify_certs: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._timeout = timeout
        self._verify_certs = verify_certs

    # ------------------------------------------------------------------ #
    # Internal helpers                                                   #
    # ------------------------------------------------------------------ #

    def _build_auth(self) -> tuple[str, str] | None:
        if self._username is not None and self._password is not None:
            return (self._username, self._password)
        return None

    def _build_document(
        self,
        document_id: str,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult,
        deployment_id: int | None,
    ) -> dict[str, Any]:
        """Assemble the JSON body PUT to Elasticsearch.

        Contains companion text (for full-text search), extraction
        metadata (structured fields), and the confidence tier (for
        filtering at query time).
        """
        tier_value: str | None = None
        if extraction_result.confidence_tier is not None:
            tier_value = extraction_result.confidence_tier.value

        document_type_value: str | None = None
        if extraction_result.document_type is not None:
            document_type_value = extraction_result.document_type.value

        discipline_value: str | None = None
        if extraction_result.discipline is not None:
            discipline_value = extraction_result.discipline.value

        return {
            "document_id": document_id,
            "deployment_id": deployment_id,
            "companion_text": companion_result.companion_text,
            "pages_described": companion_result.pages_described,
            "companion_generated": companion_result.companion_generated,
            "extraction": {
                "drawing_number": extraction_result.drawing_number,
                "drawing_number_confidence": (
                    extraction_result.drawing_number_confidence
                ),
                "title": extraction_result.title,
                "title_confidence": extraction_result.title_confidence,
                "document_type": document_type_value,
                "document_type_confidence": (
                    extraction_result.document_type_confidence
                ),
                "discipline": discipline_value,
                "revision": extraction_result.revision,
                "building_zone": extraction_result.building_zone,
                "project": extraction_result.project,
            },
            "tier": tier_value,
            "confidence_score": extraction_result.confidence_score,
        }

    # ------------------------------------------------------------------ #
    # ISearchIndexer protocol implementation                             #
    # ------------------------------------------------------------------ #

    async def index_companion(
        self,
        document_id: str,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult,
        deployment_id: int | None,
    ) -> bool:
        """PUT companion document to Elasticsearch.

        Returns ``True`` on a 2xx response, ``False`` on any failure
        (network error, non-2xx response, or unexpected exception).
        Never raises out of the adapter.
        """
        url = f"{self._base_url}/companions/_doc/{document_id}"
        body = self._build_document(
            document_id=document_id,
            extraction_result=extraction_result,
            companion_result=companion_result,
            deployment_id=deployment_id,
        )
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                verify=self._verify_certs,
                auth=self._build_auth(),
            ) as client:
                response = await client.put(url, json=body)
            if 200 <= response.status_code < 300:
                logger.debug(
                    "Indexed companion document to Elasticsearch",
                    extra={
                        "document_id": document_id,
                        "status_code": response.status_code,
                    },
                )
                return True
            logger.warning(
                "Elasticsearch index_companion returned non-2xx: %s (url=%s)",
                response.status_code,
                url,
            )
            return False
        except httpx.HTTPError as exc:
            logger.warning(
                "Elasticsearch index_companion HTTP error: %s (url=%s)",
                exc,
                url,
            )
            return False
        except Exception as exc:  # noqa: BLE001 — adapter must never raise out
            logger.warning(
                "Elasticsearch index_companion unexpected error: %s (url=%s)",
                exc,
                url,
            )
            return False

    async def check_connection(self) -> bool:
        """GET ``/_cluster/health`` and return True on any 2xx response.

        Any network failure or non-2xx response returns ``False`` after
        logging a warning. Never raises.
        """
        url = f"{self._base_url}/_cluster/health"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                verify=self._verify_certs,
                auth=self._build_auth(),
            ) as client:
                response = await client.get(url)
            if 200 <= response.status_code < 300:
                return True
            logger.warning(
                "Elasticsearch health check returned non-2xx: %s (url=%s)",
                response.status_code,
                url,
            )
            return False
        except httpx.HTTPError as exc:
            logger.warning(
                "Elasticsearch health check HTTP error: %s (url=%s)",
                exc,
                url,
            )
            return False
        except Exception as exc:  # noqa: BLE001 — adapter must never raise out
            logger.warning(
                "Elasticsearch health check unexpected error: %s (url=%s)",
                exc,
                url,
            )
            return False


class NoOpSearchIndexer:
    """No-op ``ISearchIndexer`` used when Elasticsearch is not configured.

    Returned by :func:`build_search_indexer` when
    ``settings.ELASTICSEARCH_URL`` is unset. Every method logs a debug
    message and returns a success value without any network I/O, so the
    extraction pipeline can run unchanged in local dev and CI.
    """

    async def index_companion(
        self,
        document_id: str,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult,
        deployment_id: int | None,
    ) -> bool:
        logger.debug(
            "NoOpSearchIndexer.index_companion called (document_id=%s); "
            "Elasticsearch is not configured, skipping",
            document_id,
        )
        return True

    async def check_connection(self) -> bool:
        logger.debug(
            "NoOpSearchIndexer.check_connection called; "
            "Elasticsearch is not configured"
        )
        return True


def build_search_indexer(settings: Any) -> ElasticsearchSearchIndexer | NoOpSearchIndexer:
    """Composition-root factory for the search indexer.

    Returns a :class:`NoOpSearchIndexer` when ``settings.ELASTICSEARCH_URL``
    is ``None`` (or falsy), otherwise constructs a real
    :class:`ElasticsearchSearchIndexer` pointed at the configured URL.

    Args:
        settings: Application settings object exposing an
            ``ELASTICSEARCH_URL`` attribute.

    Returns:
        An object implementing ``ISearchIndexer``.
    """
    base_url = getattr(settings, "ELASTICSEARCH_URL", None)
    if base_url is None or base_url == "":
        logger.debug(
            "build_search_indexer: ELASTICSEARCH_URL is unset, "
            "returning NoOpSearchIndexer"
        )
        return NoOpSearchIndexer()

    username = getattr(settings, "ELASTICSEARCH_USERNAME", None)
    password = getattr(settings, "ELASTICSEARCH_PASSWORD", None)
    timeout = getattr(settings, "ELASTICSEARCH_TIMEOUT", 10.0)
    verify_certs = getattr(settings, "ELASTICSEARCH_VERIFY_CERTS", True)

    return ElasticsearchSearchIndexer(
        base_url=base_url,
        username=username,
        password=password,
        timeout=timeout,
        verify_certs=verify_certs,
    )
