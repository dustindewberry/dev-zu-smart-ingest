"""Protocol-completeness tests for IJobRepository.

These tests guard against the recurring PolyForge drift pattern where the
``IJobRepository`` protocol in ``domain/protocols.py`` and the concrete
``JobRepository`` adapter in ``infrastructure/database/repository.py`` fall
out of sync. Specifically they pin:

1. ``IJobRepository.update_job_result`` is declared on the protocol (not
   merely on the concrete class), so typed callers can invoke it.
2. ``IJobRepository.get_job_by_file_hash`` uses the branded ``FileHash``
   NewType from ``shared.types`` — not bare ``str`` — so the type-level
   contract matches the rest of the codebase.
3. The concrete :class:`JobRepository` satisfies :class:`IJobRepository`
   structurally. When the protocol is ``@runtime_checkable`` we use a real
   ``isinstance()`` check; otherwise we fall back to an attribute-level
   structural check.
"""

from __future__ import annotations

import inspect
import typing
from typing import Any, get_type_hints

import pytest

from zubot_ingestion.domain.protocols import IJobRepository
from zubot_ingestion.infrastructure.database.repository import JobRepository
from zubot_ingestion.shared.types import FileHash


# ---------------------------------------------------------------------------
# (1) IJobRepository declares update_job_result
# ---------------------------------------------------------------------------


def test_protocol_declares_update_job_result() -> None:
    """IJobRepository must expose update_job_result as a protocol method."""
    assert hasattr(IJobRepository, "update_job_result"), (
        "IJobRepository is missing update_job_result — protocol drift "
        "against JobRepository.update_job_result (see repository.py:300)."
    )
    # Also confirm it's callable-shaped (a function descriptor) rather than a
    # stray attribute.
    member = inspect.getattr_static(IJobRepository, "update_job_result")
    assert callable(member), (
        "IJobRepository.update_job_result must be a method, not a value."
    )


def test_protocol_update_job_result_has_keyword_only_signature() -> None:
    """The protocol signature must match the concrete repository shape.

    The canonical signature is::

        async def update_job_result(
            self,
            job_id: JobId,
            *,
            result: dict[str, Any],
            confidence_tier: ConfidenceTier,
            confidence_score: float,
            processing_time_ms: int,
            otel_trace_id: str | None,
            pipeline_trace: dict[str, Any] | None,
        ) -> None: ...
    """
    sig = inspect.signature(IJobRepository.update_job_result)
    params = sig.parameters
    # job_id is positional-or-keyword, the rest must be keyword-only.
    expected_kw_only = {
        "result",
        "confidence_tier",
        "confidence_score",
        "processing_time_ms",
        "otel_trace_id",
        "pipeline_trace",
    }
    kw_only_params = {
        name
        for name, p in params.items()
        if p.kind is inspect.Parameter.KEYWORD_ONLY
    }
    missing = expected_kw_only - kw_only_params
    assert not missing, (
        f"IJobRepository.update_job_result is missing keyword-only "
        f"parameters: {sorted(missing)}"
    )
    assert "job_id" in params, (
        "IJobRepository.update_job_result must accept a job_id parameter."
    )


# ---------------------------------------------------------------------------
# (2) get_job_by_file_hash annotation uses FileHash
# ---------------------------------------------------------------------------


def test_get_job_by_file_hash_uses_filehash_on_protocol() -> None:
    """The protocol annotation must use the branded FileHash NewType."""
    hints = get_type_hints(IJobRepository.get_job_by_file_hash)
    assert "file_hash" in hints, (
        "IJobRepository.get_job_by_file_hash must declare a file_hash param."
    )
    annotation = hints["file_hash"]
    assert annotation is FileHash, (
        "IJobRepository.get_job_by_file_hash.file_hash must be typed as "
        f"shared.types.FileHash; got {annotation!r}."
    )


def test_get_job_by_file_hash_uses_filehash_on_concrete() -> None:
    """The concrete JobRepository must also use FileHash on the annotation."""
    hints = get_type_hints(JobRepository.get_job_by_file_hash)
    assert "file_hash" in hints, (
        "JobRepository.get_job_by_file_hash must declare a file_hash param."
    )
    assert hints["file_hash"] is FileHash, (
        "JobRepository.get_job_by_file_hash.file_hash must be typed as "
        f"shared.types.FileHash; got {hints['file_hash']!r}."
    )


# ---------------------------------------------------------------------------
# (3) JobRepository structurally satisfies IJobRepository
# ---------------------------------------------------------------------------


def _protocol_methods(protocol_cls: type) -> set[str]:
    """Return the set of protocol-defined method names (excluding dunders)."""
    members: set[str] = set()
    for name in dir(protocol_cls):
        if name.startswith("_"):
            continue
        attr = inspect.getattr_static(protocol_cls, name, None)
        if callable(attr):
            members.add(name)
    return members


def test_jobrepository_has_every_protocol_method() -> None:
    """Every IJobRepository method must exist on the concrete JobRepository.

    This is the structural equivalent of ``isinstance(repo, IJobRepository)``
    and does not require constructing a real AsyncSession.
    """
    protocol_methods = _protocol_methods(IJobRepository)
    missing = [m for m in sorted(protocol_methods) if not hasattr(JobRepository, m)]
    assert not missing, (
        f"JobRepository does not implement IJobRepository methods: {missing}"
    )


def test_jobrepository_isinstance_of_runtime_checkable_protocol() -> None:
    """If IJobRepository is @runtime_checkable, JobRepository must pass isinstance.

    ``@runtime_checkable`` only verifies method *names*, not signatures, so
    this is a lightweight guard. When the protocol is not runtime_checkable,
    the test degrades to the structural check above (already covered) and is
    skipped here.
    """
    is_runtime_checkable = getattr(IJobRepository, "_is_runtime_protocol", False)
    if not is_runtime_checkable:
        pytest.skip("IJobRepository is not @runtime_checkable")

    # Build a bare instance without calling __init__ so we avoid needing a
    # real AsyncSession. Protocol isinstance checks inspect method presence
    # on the instance's class, not on any instance state.
    repo = JobRepository.__new__(JobRepository)
    assert isinstance(repo, IJobRepository), (
        "JobRepository instance fails isinstance(IJobRepository) structural "
        "check — a protocol method is missing on the concrete class."
    )


# ---------------------------------------------------------------------------
# Regression guard: update_job_result docstring no longer disclaims the
# protocol.
# ---------------------------------------------------------------------------


def test_update_job_result_docstring_does_not_disclaim_protocol_membership() -> None:
    """The 'not part of the canonical protocol' note must be gone.

    update_job_result is now part of the canonical IJobRepository protocol,
    so the old docstring disclaimer on the concrete implementation must no
    longer appear.
    """
    doc = JobRepository.update_job_result.__doc__ or ""
    assert "not part of the canonical" not in doc.lower(), (
        "JobRepository.update_job_result docstring still claims the method "
        "is not part of the canonical protocol. Update the docstring to "
        "reflect that it is now declared on IJobRepository."
    )


# ---------------------------------------------------------------------------
# Sanity: ensure typing.get_type_hints works across __future__ annotations.
# ---------------------------------------------------------------------------


def test_typing_imports_are_available() -> None:
    """Smoke test that the imports this module depends on all resolve."""
    assert typing is not None
    assert Any is not None
