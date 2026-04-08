"""ChromaDB metadata writer — implements :class:`IMetadataWriter` (CAP-023).

The :class:`ChromaDBMetadataWriter` is the infrastructure-layer adapter that
persists Stage 3 sidecar metadata to a ChromaDB collection. It is the final
write path for AUTO and SPOT-tier extractions; REVIEW-tier results are NOT
written here — they go to the human review queue first and are only flushed
to ChromaDB after a reviewer approves them.

Collection naming follows :func:`zubot_ingestion.shared.constants.chroma_collection_name`:

    zubot_metadata_{deployment_id}_{node_id}

with ``None`` deployment_id / node_id substituted by the literal ``'default'``.

The writer also stamps every persisted record with a provenance marker
(``ingestion_service: zubot-ingestion``) so downstream consumers of the
ChromaDB collection can distinguish records originating from this service
from records ingested by other pipelines.

Layering: this module lives in the infrastructure layer and is the ONLY
place that imports the ``chromadb`` client library. The chromadb import is
deliberately lazy (performed inside :meth:`__init__`) so that:

1. Importing :mod:`zubot_ingestion.infrastructure.chromadb.writer` does not
   require ``chromadb`` to be installed (useful for unit tests that inject a
   stub client).
2. ChromaDB connection failures surface at construction time, not at module
   import time, which keeps the import graph robust against network outages.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from zubot_ingestion.domain.entities import SidecarDocument
from zubot_ingestion.domain.protocols import IMetadataWriter
from zubot_ingestion.shared.constants import (
    PROVENANCE_INGESTION_SERVICE,
    chroma_collection_name,
)

__all__ = ["ChromaDBMetadataWriter"]

_LOG = logging.getLogger(__name__)


class ChromaDBMetadataWriter(IMetadataWriter):
    """Persist Stage 3 sidecar metadata to a ChromaDB collection (CAP-023)."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        client: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Construct the writer with a ChromaDB host/port.

        Args:
            host: ChromaDB server hostname (e.g. ``"chromadb"`` or ``"localhost"``).
            port: ChromaDB server port (typically 8000).
            client: Optional pre-built ChromaDB client. If provided, ``host``
                and ``port`` are ignored and the supplied client is used as-is.
                This hook exists primarily for unit tests; production code
                should pass ``host``/``port`` and let the writer build its
                own ``HttpClient``.
            logger: Optional logger override. Defaults to the module logger.
        """
        self._host = host
        self._port = port
        self._log = logger or _LOG

        if client is not None:
            self._client = client
        else:
            # Lazy import keeps the chromadb dependency off the import graph
            # of any module that does not actually need to write metadata.
            import chromadb  # noqa: PLC0415 - intentional lazy import

            self._client = chromadb.HttpClient(host=host, port=port)

    async def write_metadata(
        self,
        document_id: str,
        sidecar: SidecarDocument,
        deployment_id: int | None,
        node_id: int | None,
    ) -> bool:
        """Upsert a sidecar document into the appropriate ChromaDB collection.

        The collection name is derived from the deployment + node tuple via
        :func:`chroma_collection_name`. ``int`` IDs are coerced to ``str``
        (the helper expects strings) before lookup.

        Metadata payload structure:
            * Every key from ``sidecar.metadata_attributes`` is copied
              verbatim.
            * The provenance key ``ingestion_service`` is added (or overwritten
              if a colliding upstream key was supplied) so the persisted record
              is unambiguously attributable to this service.

        The ``companion_text`` field on the sidecar (when present) is stored
        as the ChromaDB *document* body. When ``companion_text`` is ``None``
        an empty string is stored — ChromaDB requires a string value.

        Failures are logged and swallowed; the function never raises. The
        return value indicates whether the upsert succeeded.

        Args:
            document_id: Unique document identifier — almost always the
                file hash from :class:`Job.file_hash`.
            sidecar: Stage 3 sidecar document with ``metadata_attributes``
                and optional ``companion_text``.
            deployment_id: Deployment scope for collection routing.
                ``None`` falls back to the ``'default'`` deployment.
            node_id: Node scope for collection routing. ``None`` falls back
                to the ``'default'`` node.

        Returns:
            ``True`` if the upsert succeeded, ``False`` if any exception was
            raised during collection lookup or upsert.
        """
        collection_name = chroma_collection_name(
            _coerce_id_to_str(deployment_id),
            _coerce_id_to_str(node_id),
        )

        metadata: dict[str, Any] = {
            **sidecar.metadata_attributes,
            "ingestion_service": PROVENANCE_INGESTION_SERVICE,
        }

        document_body = sidecar.companion_text or ""

        try:
            await asyncio.to_thread(
                self._upsert_sync,
                collection_name=collection_name,
                document_id=document_id,
                document_body=document_body,
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001 - surface as False per contract
            self._log.exception(
                "chromadb_write_failed",
                extra={
                    "collection": collection_name,
                    "document_id": document_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return False

        return True

    def _upsert_sync(
        self,
        *,
        collection_name: str,
        document_id: str,
        document_body: str,
        metadata: dict[str, Any],
    ) -> None:
        """Synchronous helper that wraps the blocking ChromaDB calls.

        Split out so :meth:`write_metadata` can dispatch it via
        :func:`asyncio.to_thread`, keeping the event loop free during the
        network round-trip to ChromaDB.
        """
        collection = self._client.get_or_create_collection(name=collection_name)
        collection.upsert(
            ids=[document_id],
            documents=[document_body],
            metadatas=[metadata],
        )

    async def check_connection(self) -> bool:
        """Verify the ChromaDB server is reachable.

        Calls :meth:`chromadb.HttpClient.list_collections` once. Returns
        ``True`` on any successful response, ``False`` if the call raises.
        Never propagates exceptions — callers (e.g. ``/health``) can rely
        on this method to be safe.
        """
        try:
            await asyncio.to_thread(self._client.list_collections)
        except Exception as exc:  # noqa: BLE001 - surface as False per contract
            self._log.warning(
                "chromadb_check_connection_failed",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
            return False
        return True


def _coerce_id_to_str(value: int | str | None) -> str | None:
    """Coerce an int|str|None ID to str|None for collection naming.

    The canonical :class:`Batch` entity stores ``deployment_id`` and
    ``node_id`` as ``int | None`` (matching the database schema), but
    :func:`chroma_collection_name` accepts ``str | None`` so the writer
    has to bridge the two representations. ``None`` passes through
    unchanged so the helper's default-substitution behavior is preserved.
    """
    if value is None:
        return None
    return str(value)
