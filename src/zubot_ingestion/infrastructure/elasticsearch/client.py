"""Elasticsearch search indexer (CAP-024 / step-21 canonical).

Implements :class:`~zubot_ingestion.domain.protocols.ISearchIndexer` for
full-text companion document indexing. Uses the official ``elasticsearch``
Python async client when available, and falls back to a minimal
``httpx.AsyncClient``-based implementation otherwise so the adapter can
still run in environments where the official client is not installed.

Index name format follows §4.19: ``zubot_companion_{deployment_id}`` when
``deployment_id`` is provided, or ``zubot_companion_default`` otherwise.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from typing import Any, cast

import httpx

from zubot_ingestion.domain.entities import CompanionResult, ExtractionResult

try:
    from elasticsearch import AsyncElasticsearch  # type: ignore[import-not-found]

    _HAS_ELASTICSEARCH_CLIENT = True
except ImportError:  # pragma: no cover - exercised only when dep missing
    AsyncElasticsearch = None  # type: ignore[assignment,misc]
    _HAS_ELASTICSEARCH_CLIENT = False


_LOG = logging.getLogger(__name__)

_DEFAULT_INDEX_PREFIX = "zubot_companion"


def _index_name_for(deployment_id: int | None) -> str:
    """Return the target Elasticsearch index for a given deployment."""
    if deployment_id is None:
        return f"{_DEFAULT_INDEX_PREFIX}_default"
    return f"{_DEFAULT_INDEX_PREFIX}_{int(deployment_id)}"


def _extraction_to_dict(extraction_result: ExtractionResult) -> dict[str, Any]:
    """Convert an :class:`ExtractionResult` dataclass to a JSON-safe dict."""
    if is_dataclass(extraction_result) and not isinstance(extraction_result, type):
        data = asdict(extraction_result)
    else:  # pragma: no cover - defensive
        data = dict(getattr(extraction_result, "__dict__", {}))

    # Normalise enum values to their string form so Elasticsearch accepts them.
    for key, value in list(data.items()):
        if hasattr(value, "value"):
            data[key] = value.value
    return data


def _build_document(
    *,
    document_id: str,
    extraction_result: ExtractionResult,
    companion_result: CompanionResult,
    deployment_id: int | None,
) -> dict[str, Any]:
    """Assemble the Elasticsearch document payload for a companion record."""
    extraction = _extraction_to_dict(extraction_result)
    document: dict[str, Any] = {
        "document_id": document_id,
        "deployment_id": deployment_id,
        "companion_text": companion_result.companion_text,
        "pages_described": companion_result.pages_described,
        "companion_generated": companion_result.companion_generated,
        "validation_passed": companion_result.validation_passed,
        "quality_score": companion_result.quality_score,
        "drawing_number": extraction.get("drawing_number"),
        "drawing_number_confidence": extraction.get("drawing_number_confidence"),
        "title": extraction.get("title"),
        "title_confidence": extraction.get("title_confidence"),
        "document_type": extraction.get("document_type"),
        "document_type_confidence": extraction.get("document_type_confidence"),
        "discipline": extraction.get("discipline"),
        "revision": extraction.get("revision"),
        "building_zone": extraction.get("building_zone"),
        "project": extraction.get("project"),
        "confidence_score": extraction.get("confidence_score"),
        "confidence_tier": extraction.get("confidence_tier"),
    }
    return document


class ElasticsearchSearchIndexer:
    """Elasticsearch-backed :class:`ISearchIndexer` implementation.

    Uses the official ``elasticsearch`` async client when it is importable.
    If that dependency is missing for any reason, the adapter transparently
    falls back to an ``httpx.AsyncClient``-based minimal implementation that
    speaks the Elasticsearch REST API directly (index via
    ``PUT {base_url}/{index}/_doc/{id}`` and health via
    ``GET {base_url}/_cluster/health``).
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
        self._auth: tuple[str, str] | None = (
            (username, password) if username and password else None
        )

        self._client: Any | None = None
        if _HAS_ELASTICSEARCH_CLIENT and AsyncElasticsearch is not None:
            client_kwargs: dict[str, Any] = {
                "hosts": [self._base_url],
                "request_timeout": self._timeout,
                "verify_certs": self._verify_certs,
            }
            if self._auth is not None:
                client_kwargs["basic_auth"] = self._auth
            self._client = AsyncElasticsearch(**client_kwargs)

    async def index_companion(
        self,
        document_id: str,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult,
        deployment_id: int | None,
    ) -> bool:
        """Index a companion document.

        Returns ``True`` on success and ``False`` on any error so callers can
        degrade gracefully — indexing failures must not take down the
        extraction pipeline.
        """
        index_name = _index_name_for(deployment_id)
        document = _build_document(
            document_id=document_id,
            extraction_result=extraction_result,
            companion_result=companion_result,
            deployment_id=deployment_id,
        )

        try:
            if self._client is not None:
                await self._client.index(
                    index=index_name,
                    id=document_id,
                    document=document,
                )
                return True
            return await self._index_via_httpx(
                index_name=index_name,
                document_id=document_id,
                document=document,
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            _LOG.warning(
                "elasticsearch_index_failed",
                extra={
                    "document_id": document_id,
                    "index": index_name,
                    "error": str(exc),
                },
            )
            return False

    async def check_connection(self) -> bool:
        """Return ``True`` if the Elasticsearch cluster is reachable."""
        try:
            if self._client is not None:
                result = await self._client.cluster.health()
                # Cluster health returns a dict-like with a ``status`` key;
                # any response at all means the endpoint is reachable.
                return bool(result)
            return await self._health_via_httpx()
        except Exception as exc:  # pragma: no cover - defensive logging
            _LOG.warning(
                "elasticsearch_health_check_failed", extra={"error": str(exc)}
            )
            return False

    async def close(self) -> None:
        """Release any resources held by the underlying client."""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as exc:  # pragma: no cover - defensive
                _LOG.debug(
                    "elasticsearch_client_close_failed", extra={"error": str(exc)}
                )

    # ------------------------------------------------------------------
    # httpx fallback path
    # ------------------------------------------------------------------

    async def _index_via_httpx(
        self,
        *,
        index_name: str,
        document_id: str,
        document: dict[str, Any],
    ) -> bool:
        url = f"{self._base_url}/{index_name}/_doc/{document_id}"
        async with httpx.AsyncClient(
            timeout=self._timeout,
            auth=cast(Any, self._auth),
            verify=self._verify_certs,
        ) as client:
            response = await client.put(url, json=document)
            response.raise_for_status()
        return True

    async def _health_via_httpx(self) -> bool:
        url = f"{self._base_url}/_cluster/health"
        async with httpx.AsyncClient(
            timeout=self._timeout,
            auth=cast(Any, self._auth),
            verify=self._verify_certs,
        ) as client:
            response = await client.get(url)
            return response.status_code == 200


__all__ = ["ElasticsearchSearchIndexer"]
