"""Loop-breaker build-gate regression tests.

These tests are the durable guardrails that prevent the zutec-smart-ingest
deterministic 8-bug fingerprint from silently reappearing on future merges.
Unlike the per-bug regression tests, these tests treat the codebase as a
whole and assert structural invariants:

1. Protected files must NOT contain any placeholder sentinel strings and
   must not look like one-line docstring stubs.

2. The services composition root must produce a runnable JobRepository
   — calling ``get_job_repository()`` must not raise TypeError.

3. The concrete ``JobRepository`` must implement every public method
   declared on the ``IJobRepository`` protocol (guards against protocol
   drift).

All three tests are import-time safe and can be collected even when the
canonical fixes have not landed yet.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Shared constants — tolerant import so the test module still collects
# even when task-1 has not yet added the constants.
# ---------------------------------------------------------------------------


try:
    from zubot_ingestion.shared.constants import (
        BUILD_GATE_PROTECTED_FILES,
        PLACEHOLDER_SENTINEL_OVERWRITE,
        PLACEHOLDER_SENTINEL_OWNED_BY,
    )
except ImportError:  # pragma: no cover — task-1 not yet merged
    # Fallback defaults matching the canonical task-1 contract so this
    # module can still be imported. The actual test below will still fail
    # loudly when the constants are missing, because the first assert
    # checks that the constants are importable.
    BUILD_GATE_PROTECTED_FILES = (
        "src/zubot_ingestion/api/routes/review.py",
        "src/zubot_ingestion/domain/pipeline/validation.py",
        "src/zubot_ingestion/infrastructure/elasticsearch/__init__.py",
        "src/zubot_ingestion/infrastructure/callback/__init__.py",
    )
    PLACEHOLDER_SENTINEL_OVERWRITE = "MUST overwrite this file on merge"
    PLACEHOLDER_SENTINEL_OWNED_BY = "placeholder owned by sibling task"


# ---------------------------------------------------------------------------
# Build gate #1 — No placeholder sentinels in protected files
# ---------------------------------------------------------------------------


def test_protected_files_contain_no_placeholder_sentinels() -> None:
    """Guards against the eight-bug fingerprint recurring.

    Protected files (owned by sibling tasks that historically fail to
    land) must never ship with their placeholder sentinel strings
    (``MUST overwrite``, ``placeholder owned by sibling task``) present,
    and they must not look like 1-line docstring stubs.

    Also verifies that the build-gate constants themselves are importable
    from ``shared.constants`` — the very first assertion catches the
    state where task-1 has not landed the constants.
    """
    # Hard fail if the shared constants are not importable. This is the
    # loop-breaker: if task-1 has not merged, the tests *must* fail so
    # the orchestrator cannot mark the build complete.
    from zubot_ingestion.shared.constants import (  # noqa: F401
        BUILD_GATE_PROTECTED_FILES as _BGP,
        PLACEHOLDER_SENTINEL_OVERWRITE as _PSO,
        PLACEHOLDER_SENTINEL_OWNED_BY as _PSB,
    )

    repo_root = Path(__file__).resolve().parents[2]

    for rel_path in _BGP:
        f = repo_root / rel_path
        assert f.exists(), (
            f"Protected file is missing: {rel_path}. One of the canonical "
            f"sibling-task implementations (step-19/20/21) has been lost."
        )

        text = f.read_text()
        assert _PSO not in text, (
            f"Protected file {rel_path} contains the placeholder sentinel "
            f"string '{_PSO}'. The canonical sibling-task implementation "
            f"has been silently replaced with a placeholder again."
        )
        assert _PSB not in text, (
            f"Protected file {rel_path} contains the placeholder sentinel "
            f"string '{_PSB}'. The canonical sibling-task implementation "
            f"has been silently replaced with a placeholder again."
        )

        # Stub detection: a file with fewer than 4 non-blank lines is
        # almost certainly a 1-line-docstring placeholder.
        non_blank = [line for line in text.splitlines() if line.strip()]
        assert len(non_blank) > 3, (
            f"Protected file {rel_path} has only {len(non_blank)} "
            f"non-blank lines — this looks like a placeholder stub. "
            f"The canonical implementation is missing."
        )


# ---------------------------------------------------------------------------
# Build gate #2 — Composition root produces a runnable JobRepository
# ---------------------------------------------------------------------------


def test_get_job_repository_is_callable_without_typeerror() -> None:
    """Catches composition-root drift before it reaches runtime.

    Calling ``get_job_repository()`` must not raise ``TypeError`` from
    wrong keyword arguments passed to ``JobRepository.__init__``. This
    is the single-check version of the per-bug regression test for
    Critical Issue #1 — it catches the broken factory shape even if the
    test suite has no DB fixture.
    """
    from zubot_ingestion.services import get_job_repository

    try:
        cm = get_job_repository()
    except TypeError as exc:
        pytest.fail(
            f"get_job_repository() raised TypeError — this is the "
            f"composition-root drift bug where the factory passes a "
            f"kwarg that the concrete JobRepository adapter does not "
            f"accept. Error: {exc!r}"
        )

    assert cm is not None, "get_job_repository() must not return None"


# ---------------------------------------------------------------------------
# Build gate #3 — Concrete repository implements every protocol method
# ---------------------------------------------------------------------------


def test_concrete_repository_implements_protocol_methods() -> None:
    """Catches protocol drift between IJobRepository and JobRepository.

    Every public method declared on the ``IJobRepository`` protocol must
    also be present on the concrete ``JobRepository`` class. If they
    diverge, callers that depend on the protocol type will silently lose
    static type safety and runtime ``AttributeError``-s will leak into
    production.
    """
    from zubot_ingestion.domain.protocols import IJobRepository
    from zubot_ingestion.infrastructure.database.repository import (
        JobRepository,
    )

    protocol_methods = {
        name
        for name, val in inspect.getmembers(IJobRepository, inspect.isfunction)
        if not name.startswith("_")
    }
    concrete_methods = {
        name
        for name, val in inspect.getmembers(JobRepository, inspect.isfunction)
        if not name.startswith("_")
    }

    missing = protocol_methods - concrete_methods
    assert not missing, (
        f"JobRepository is missing protocol methods declared on "
        f"IJobRepository: {sorted(missing)}. This is the protocol-drift "
        f"bug — callers using the protocol type will fail at runtime "
        f"with AttributeError."
    )
