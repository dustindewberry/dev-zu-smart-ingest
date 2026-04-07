"""Unit tests for the ChromaDB metadata writer (CAP-023).

These tests inject a stub ChromaDB client (via the ``client=`` constructor
hook) so the test suite never touches the real chromadb HTTP client. Each
test verifies the *state after mutation* on the stub collection — per the
worker-agent guidance:

    "for any function that mutates persistent state, write at least one test
    that verifies the state AFTER mutation matches expectations."

The collection-name routing is verified against the canonical
:func:`chroma_collection_name` helper from
:mod:`zubot_ingestion.shared.constants` so the writer cannot drift from
the rest of the codebase.
"""

from __future__ import annotations

from typing import Any

import pytest

from zubot_ingestion.domain.entities import SidecarDocument
from zubot_ingestion.domain.protocols import IMetadataWriter
from zubot_ingestion.infrastructure.chromadb.writer import (
    ChromaDBMetadataWriter,
    _coerce_id_to_str,
)
from zubot_ingestion.shared.constants import (
    PROVENANCE_INGESTION_SERVICE,
    chroma_collection_name,
)
from zubot_ingestion.shared.types import FileHash


# ---------------------------------------------------------------------------
# Stub chromadb client + collection
# ---------------------------------------------------------------------------


class StubCollection:
    """In-memory stand-in for a chromadb Collection."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.upsert_calls: list[dict[str, Any]] = []
        self._raise_on_upsert: BaseException | None = None

    def upsert(
        self,
        *,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        if self._raise_on_upsert is not None:
            raise self._raise_on_upsert
        self.upsert_calls.append(
            {"ids": list(ids), "documents": list(documents), "metadatas": list(metadatas)}
        )

    def fail_next_upsert_with(self, exc: BaseException) -> None:
        self._raise_on_upsert = exc


class StubChromaClient:
    """In-memory stand-in for a chromadb HttpClient."""

    def __init__(self) -> None:
        self.collections: dict[str, StubCollection] = {}
        self.get_or_create_calls: list[str] = []
        self.list_collections_calls: int = 0
        self._raise_on_get_or_create: BaseException | None = None
        self._raise_on_list: BaseException | None = None

    def get_or_create_collection(self, *, name: str) -> StubCollection:
        self.get_or_create_calls.append(name)
        if self._raise_on_get_or_create is not None:
            raise self._raise_on_get_or_create
        if name not in self.collections:
            self.collections[name] = StubCollection(name)
        return self.collections[name]

    def list_collections(self) -> list[StubCollection]:
        self.list_collections_calls += 1
        if self._raise_on_list is not None:
            raise self._raise_on_list
        return list(self.collections.values())

    def fail_next_get_or_create_with(self, exc: BaseException) -> None:
        self._raise_on_get_or_create = exc

    def fail_list_with(self, exc: BaseException) -> None:
        self._raise_on_list = exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_sidecar(
    *,
    metadata_attributes: dict[str, Any] | None = None,
    companion_text: str | None = None,
) -> SidecarDocument:
    return SidecarDocument(
        metadata_attributes=metadata_attributes
        or {
            "source_filename": "test.pdf",
            "document_type": "technical_drawing",
            "extraction_confidence": 0.85,
        },
        companion_text=companion_text,
        source_filename="test.pdf",
        file_hash=FileHash("a" * 64),
    )


def _build_writer(client: StubChromaClient | None = None) -> tuple[ChromaDBMetadataWriter, StubChromaClient]:
    stub = client or StubChromaClient()
    writer = ChromaDBMetadataWriter(host="chromadb", port=8000, client=stub)
    return writer, stub


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_chromadb_writer_satisfies_imetadata_writer_protocol() -> None:
    """ChromaDBMetadataWriter must structurally satisfy IMetadataWriter."""
    writer, _ = _build_writer()
    assert isinstance(writer, IMetadataWriter)


def test_lazy_chromadb_import_skipped_when_client_injected() -> None:
    """Constructing with a stub client must not require chromadb to be installed.

    The fact that this test runs at all (in a venv that does not have
    chromadb installed) is the proof. Constructing the writer should not
    raise ImportError.
    """
    stub = StubChromaClient()
    writer = ChromaDBMetadataWriter(host="chromadb", port=8000, client=stub)
    assert writer is not None
    # And the client really is the stub, not a real HttpClient
    assert writer._client is stub


# ---------------------------------------------------------------------------
# write_metadata — happy path / state after mutation
# ---------------------------------------------------------------------------


async def test_write_metadata_upserts_into_default_collection_when_ids_none() -> None:
    """Both deployment_id and node_id None → 'zubot_metadata_default_default'."""
    writer, stub = _build_writer()
    sidecar = _build_sidecar()

    ok = await writer.write_metadata(
        document_id="abc123",
        sidecar=sidecar,
        deployment_id=None,
        node_id=None,
    )

    assert ok is True
    expected_collection = chroma_collection_name(None, None)
    assert expected_collection == "zubot_metadata_default_default"
    assert stub.get_or_create_calls == [expected_collection]
    collection = stub.collections[expected_collection]
    assert len(collection.upsert_calls) == 1
    call = collection.upsert_calls[0]
    assert call["ids"] == ["abc123"]


async def test_write_metadata_upserts_into_scoped_collection_when_ids_present() -> None:
    """deployment_id=42, node_id=7 → 'zubot_metadata_42_7'."""
    writer, stub = _build_writer()
    sidecar = _build_sidecar()

    ok = await writer.write_metadata(
        document_id="abc123",
        sidecar=sidecar,
        deployment_id=42,
        node_id=7,
    )

    assert ok is True
    expected_collection = chroma_collection_name("42", "7")
    assert expected_collection == "zubot_metadata_42_7"
    assert stub.get_or_create_calls == [expected_collection]
    assert expected_collection in stub.collections


async def test_write_metadata_upserts_into_partial_scoped_collection() -> None:
    """deployment_id present but node_id None → 'zubot_metadata_42_default'."""
    writer, stub = _build_writer()
    sidecar = _build_sidecar()

    ok = await writer.write_metadata(
        document_id="abc123",
        sidecar=sidecar,
        deployment_id=42,
        node_id=None,
    )

    assert ok is True
    expected = chroma_collection_name("42", None)
    assert expected == "zubot_metadata_42_default"
    assert stub.get_or_create_calls == [expected]


async def test_write_metadata_payload_includes_sidecar_attributes() -> None:
    """Every key from sidecar.metadata_attributes must be in the upsert metadata."""
    writer, stub = _build_writer()
    sidecar = _build_sidecar(
        metadata_attributes={
            "source_filename": "drawing.pdf",
            "document_type": "technical_drawing",
            "extraction_confidence": 0.92,
            "drawing_number": "170154-L-001",
            "title": "Foundation Plan",
            "discipline": "structural",
        }
    )

    ok = await writer.write_metadata(
        document_id="hash-1",
        sidecar=sidecar,
        deployment_id=None,
        node_id=None,
    )

    assert ok is True
    collection = stub.collections["zubot_metadata_default_default"]
    [call] = collection.upsert_calls
    [metadata] = call["metadatas"]
    assert metadata["source_filename"] == "drawing.pdf"
    assert metadata["document_type"] == "technical_drawing"
    assert metadata["extraction_confidence"] == 0.92
    assert metadata["drawing_number"] == "170154-L-001"
    assert metadata["title"] == "Foundation Plan"
    assert metadata["discipline"] == "structural"


async def test_write_metadata_payload_stamps_provenance_marker() -> None:
    """The persisted metadata must include ingestion_service=zubot-ingestion."""
    writer, stub = _build_writer()
    sidecar = _build_sidecar()

    await writer.write_metadata(
        document_id="hash-1",
        sidecar=sidecar,
        deployment_id=None,
        node_id=None,
    )

    [call] = stub.collections["zubot_metadata_default_default"].upsert_calls
    [metadata] = call["metadatas"]
    assert "ingestion_service" in metadata
    assert metadata["ingestion_service"] == PROVENANCE_INGESTION_SERVICE
    assert metadata["ingestion_service"] == "zubot-ingestion"


async def test_write_metadata_provenance_overrides_colliding_upstream_key() -> None:
    """A colliding 'ingestion_service' from upstream must be overwritten."""
    writer, stub = _build_writer()
    sidecar = _build_sidecar(
        metadata_attributes={
            "source_filename": "x.pdf",
            "document_type": "rfi",
            "extraction_confidence": 0.7,
            "ingestion_service": "some-other-service",
        }
    )

    await writer.write_metadata(
        document_id="hash-1",
        sidecar=sidecar,
        deployment_id=None,
        node_id=None,
    )

    [call] = stub.collections["zubot_metadata_default_default"].upsert_calls
    [metadata] = call["metadatas"]
    assert metadata["ingestion_service"] == PROVENANCE_INGESTION_SERVICE


async def test_write_metadata_uses_companion_text_as_document_body() -> None:
    """When companion_text is set it must become the upsert document body."""
    writer, stub = _build_writer()
    companion = "VISUAL DESCRIPTION:\nFoundation plan with rebar layout."
    sidecar = _build_sidecar(companion_text=companion)

    await writer.write_metadata(
        document_id="hash-1",
        sidecar=sidecar,
        deployment_id=None,
        node_id=None,
    )

    [call] = stub.collections["zubot_metadata_default_default"].upsert_calls
    assert call["documents"] == [companion]


async def test_write_metadata_uses_empty_string_when_companion_text_is_none() -> None:
    """When companion_text is None the document body must be an empty string."""
    writer, stub = _build_writer()
    sidecar = _build_sidecar(companion_text=None)

    await writer.write_metadata(
        document_id="hash-1",
        sidecar=sidecar,
        deployment_id=None,
        node_id=None,
    )

    [call] = stub.collections["zubot_metadata_default_default"].upsert_calls
    assert call["documents"] == [""]


async def test_write_metadata_passes_document_id_as_upsert_id() -> None:
    """The document_id argument must become the single upsert id."""
    writer, stub = _build_writer()
    sidecar = _build_sidecar()

    await writer.write_metadata(
        document_id="custom-id-xyz",
        sidecar=sidecar,
        deployment_id=None,
        node_id=None,
    )

    [call] = stub.collections["zubot_metadata_default_default"].upsert_calls
    assert call["ids"] == ["custom-id-xyz"]


# ---------------------------------------------------------------------------
# write_metadata — failure paths
# ---------------------------------------------------------------------------


async def test_write_metadata_returns_false_when_upsert_raises() -> None:
    """An exception during upsert must be swallowed and surfaced as False."""
    writer, stub = _build_writer()
    sidecar = _build_sidecar()
    # Pre-create the collection so we can rig its upsert to fail
    pre_collection = stub.get_or_create_collection(name="zubot_metadata_default_default")
    pre_collection.fail_next_upsert_with(RuntimeError("chromadb is unhappy"))
    # Reset the call log so the test only sees the writer's call
    stub.get_or_create_calls.clear()

    ok = await writer.write_metadata(
        document_id="hash-1",
        sidecar=sidecar,
        deployment_id=None,
        node_id=None,
    )

    assert ok is False


async def test_write_metadata_returns_false_when_get_or_create_raises() -> None:
    """An exception during collection lookup must be swallowed → False."""
    writer, stub = _build_writer()
    stub.fail_next_get_or_create_with(ConnectionError("server unreachable"))
    sidecar = _build_sidecar()

    ok = await writer.write_metadata(
        document_id="hash-1",
        sidecar=sidecar,
        deployment_id=None,
        node_id=None,
    )

    assert ok is False


async def test_write_metadata_does_not_propagate_exceptions() -> None:
    """The function must NEVER raise — even on the wildest failure."""
    writer, stub = _build_writer()
    stub.fail_next_get_or_create_with(SystemError("boom"))
    sidecar = _build_sidecar()

    # No pytest.raises — must complete without raising.
    ok = await writer.write_metadata(
        document_id="hash-1",
        sidecar=sidecar,
        deployment_id=None,
        node_id=None,
    )
    assert ok is False


# ---------------------------------------------------------------------------
# check_connection
# ---------------------------------------------------------------------------


async def test_check_connection_returns_true_on_success() -> None:
    writer, stub = _build_writer()

    ok = await writer.check_connection()

    assert ok is True
    assert stub.list_collections_calls == 1


async def test_check_connection_returns_false_when_list_raises() -> None:
    writer, stub = _build_writer()
    stub.fail_list_with(ConnectionError("server is down"))

    ok = await writer.check_connection()

    assert ok is False
    assert stub.list_collections_calls == 1


async def test_check_connection_does_not_propagate_exceptions() -> None:
    writer, stub = _build_writer()
    stub.fail_list_with(RuntimeError("oh no"))

    # No pytest.raises — must complete without raising.
    ok = await writer.check_connection()
    assert ok is False


# ---------------------------------------------------------------------------
# _coerce_id_to_str helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        (0, "0"),
        (42, "42"),
        ("already-str", "already-str"),
    ],
)
def test_coerce_id_to_str(value: int | str | None, expected: str | None) -> None:
    assert _coerce_id_to_str(value) == expected
