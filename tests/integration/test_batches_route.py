"""Integration tests for GET /batches/{batch_id} (CAP-010).

Uses FastAPI's :class:`TestClient` with ``dependency_overrides`` to swap
the real :class:`JobService` and the auth context for in-memory stubs,
exactly the same way ``tests/unit/test_extract_route.py`` does for the
extract route. The route is mounted on a minimal :class:`FastAPI` app
(not :func:`create_app`) so AuthMiddleware is bypassed and we can test
the routing/serialisation surface in isolation.

Coverage:
    * Happy path: 200 response with correctly shaped JSON body
    * Aggregated batch status derived from child job statuses
    * Progress object includes the ``in_progress`` counter
    * 404 when the service raises :class:`NotFoundError`
    * 404 when the service returns ``None`` (defensive fallback)
    * 400 for malformed UUID in the path
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zubot_ingestion.api.middleware.auth import get_auth_context
from zubot_ingestion.api.routes.batches import router as batches_router
from zubot_ingestion.api.routes.extract import get_job_service
from zubot_ingestion.domain.enums import JobStatus
from zubot_ingestion.services.job_service import NotFoundError
from zubot_ingestion.shared.types import (
    AuthContext,
    BatchId,
    BatchProgress,
    BatchWithJobs,
    FileHash,
    JobId,
    JobSummary,
)


# ---------------------------------------------------------------------------
# Stub job service
# ---------------------------------------------------------------------------


class StubBatchService:
    """In-memory stand-in for IJobService.

    Holds a single configurable result and tracks the calls made to
    ``get_batch`` so tests can verify the route forwarded the parsed
    UUID and the auth context unmodified.
    """

    def __init__(self) -> None:
        self.get_batch_calls: list[tuple[UUID, AuthContext]] = []
        self._result: BatchWithJobs | None = None
        self._raise: Exception | None = None

    def set_result(self, result: BatchWithJobs | None) -> None:
        self._result = result
        self._raise = None

    def set_raise(self, exc: Exception) -> None:
        self._raise = exc
        self._result = None

    async def get_batch(self, batch_id: UUID, auth_context: AuthContext) -> BatchWithJobs | None:
        self.get_batch_calls.append((batch_id, auth_context))
        if self._raise is not None:
            raise self._raise
        return self._result

    # Unused stubs to satisfy the IJobService protocol shape if anyone
    # ever calls them through this fixture.
    async def submit_batch(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def get_job(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def get_job_preview(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_test_app(stub: StubBatchService) -> FastAPI:
    """Build a minimal FastAPI app wrapping just the batches router.

    Auth is bypassed by overriding ``get_auth_context`` to return a
    fixed :class:`AuthContext`. The real :func:`get_job_service` factory
    is overridden to yield the provided stub.
    """
    app = FastAPI()
    app.include_router(batches_router)

    def _fake_auth() -> AuthContext:
        return AuthContext(
            user_id="test-user",
            auth_method="api_key",
            deployment_id=None,
            node_id=None,
        )

    async def _fake_job_service():
        yield stub

    app.dependency_overrides[get_auth_context] = _fake_auth
    app.dependency_overrides[get_job_service] = _fake_job_service
    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_summary(
    job_id: UUID | None = None,
    filename: str = "doc.pdf",
    status: JobStatus = JobStatus.QUEUED,
    file_hash: str = "0" * 64,
    result: dict[str, Any] | None = None,
) -> JobSummary:
    return JobSummary(
        job_id=JobId(job_id or uuid4()),
        filename=filename,
        status=status,
        file_hash=FileHash(file_hash),
        result=result,
    )


def _make_batch(
    batch_id: UUID | None = None,
    status: JobStatus = JobStatus.QUEUED,
    jobs: list[JobSummary] | None = None,
    progress: BatchProgress | None = None,
    callback_url: str | None = None,
    deployment_id: int | None = None,
    node_id: int | None = None,
) -> BatchWithJobs:
    job_list = jobs if jobs is not None else [_make_summary()]
    if progress is None:
        completed = sum(1 for j in job_list if j.status == JobStatus.COMPLETED)
        queued = sum(1 for j in job_list if j.status == JobStatus.QUEUED)
        failed = sum(1 for j in job_list if j.status == JobStatus.FAILED)
        in_progress = sum(1 for j in job_list if j.status == JobStatus.PROCESSING)
        progress = BatchProgress(
            completed=completed,
            queued=queued,
            failed=failed,
            total=len(job_list),
            in_progress=in_progress,
        )
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return BatchWithJobs(
        batch_id=BatchId(batch_id or uuid4()),
        status=status,
        progress=progress,
        jobs=job_list,
        callback_url=callback_url,
        deployment_id=deployment_id,
        node_id=node_id,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_get_batch_happy_path_returns_200_with_full_payload() -> None:
    stub = StubBatchService()
    batch_id = uuid4()
    job_id = uuid4()
    summary = _make_summary(
        job_id=job_id,
        filename="dwg-001.pdf",
        status=JobStatus.QUEUED,
        file_hash="a" * 64,
    )
    stub.set_result(
        _make_batch(
            batch_id=batch_id,
            status=JobStatus.QUEUED,
            jobs=[summary],
            callback_url="https://example.test/cb",
            deployment_id=42,
            node_id=7,
        )
    )

    client = TestClient(_make_test_app(stub))
    response = client.get(f"/batches/{batch_id}")
    assert response.status_code == 200

    body = response.json()
    assert body["batch_id"] == str(batch_id)
    assert body["status"] == "queued"
    assert body["callback_url"] == "https://example.test/cb"
    assert body["deployment_id"] == 42
    assert body["node_id"] == 7
    assert body["progress"] == {
        "completed": 0,
        "queued": 1,
        "failed": 0,
        "in_progress": 0,
        "total": 1,
    }
    assert body["jobs"] == [
        {
            "job_id": str(job_id),
            "filename": "dwg-001.pdf",
            "status": "queued",
            "file_hash": "a" * 64,
            "result": None,
        }
    ]
    assert "created_at" in body and "updated_at" in body


def test_get_batch_progress_includes_in_progress_counter() -> None:
    stub = StubBatchService()
    summaries = [
        _make_summary(status=JobStatus.PROCESSING),
        _make_summary(status=JobStatus.PROCESSING),
        _make_summary(status=JobStatus.COMPLETED),
        _make_summary(status=JobStatus.FAILED),
    ]
    stub.set_result(_make_batch(jobs=summaries, status=JobStatus.PROCESSING))

    client = TestClient(_make_test_app(stub))
    response = client.get(f"/batches/{uuid4()}")
    assert response.status_code == 200
    progress = response.json()["progress"]
    assert progress["in_progress"] == 2
    assert progress["completed"] == 1
    assert progress["failed"] == 1
    assert progress["queued"] == 0
    assert progress["total"] == 4


def test_get_batch_forwards_parsed_uuid_and_auth_context() -> None:
    stub = StubBatchService()
    stub.set_result(_make_batch())
    client = TestClient(_make_test_app(stub))
    target = uuid4()
    response = client.get(f"/batches/{target}")
    assert response.status_code == 200
    assert len(stub.get_batch_calls) == 1
    forwarded_id, forwarded_auth = stub.get_batch_calls[0]
    assert forwarded_id == target
    assert isinstance(forwarded_id, UUID)
    assert forwarded_auth.user_id == "test-user"
    assert forwarded_auth.auth_method == "api_key"


# ---------------------------------------------------------------------------
# 404 tests
# ---------------------------------------------------------------------------


def test_get_batch_returns_404_when_service_raises_not_found() -> None:
    stub = StubBatchService()
    stub.set_raise(NotFoundError("Batch not in repo"))
    client = TestClient(_make_test_app(stub))
    response = client.get(f"/batches/{uuid4()}")
    assert response.status_code == 404
    assert "Batch not in repo" in response.json()["detail"]


def test_get_batch_returns_404_when_service_returns_none() -> None:
    """Defensive fallback for stale adapters that return None."""
    stub = StubBatchService()
    stub.set_result(None)
    client = TestClient(_make_test_app(stub))
    response = client.get(f"/batches/{uuid4()}")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 400 test
# ---------------------------------------------------------------------------


def test_get_batch_rejects_malformed_uuid_with_400() -> None:
    stub = StubBatchService()
    stub.set_result(_make_batch())
    client = TestClient(_make_test_app(stub))
    response = client.get("/batches/not-a-uuid")
    assert response.status_code == 400
    assert "Invalid batch_id" in response.json()["detail"]
    # Service must NOT have been invoked.
    assert stub.get_batch_calls == []
