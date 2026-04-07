"""Integration tests for GET /jobs/{job_id} (CAP-011).

Uses FastAPI's :class:`TestClient` with ``dependency_overrides`` to swap
the real :class:`JobService` and the auth context for in-memory stubs,
exactly the same way ``tests/unit/test_extract_route.py`` does for the
extract route. The route is mounted on a minimal :class:`FastAPI` app
(not :func:`create_app`) so AuthMiddleware is bypassed and we can test
the routing/serialisation surface in isolation.

Coverage:
    * Happy path: 200 with full JobDetail JSON including pipeline_trace,
      otel_trace_id, processing_time_ms
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
from zubot_ingestion.api.routes.extract import get_job_service
from zubot_ingestion.api.routes.jobs import router as jobs_router
from zubot_ingestion.domain.enums import JobStatus
from zubot_ingestion.services.job_service import NotFoundError
from zubot_ingestion.shared.types import (
    AuthContext,
    BatchId,
    FileHash,
    JobDetail,
    JobId,
)


# ---------------------------------------------------------------------------
# Stub job service
# ---------------------------------------------------------------------------


class StubJobService:
    """In-memory stand-in for IJobService."""

    def __init__(self) -> None:
        self.get_job_calls: list[tuple[UUID, AuthContext]] = []
        self._result: JobDetail | None = None
        self._raise: Exception | None = None

    def set_result(self, result: JobDetail | None) -> None:
        self._result = result
        self._raise = None

    def set_raise(self, exc: Exception) -> None:
        self._raise = exc
        self._result = None

    async def get_job(self, job_id: UUID, auth_context: AuthContext) -> JobDetail | None:
        self.get_job_calls.append((job_id, auth_context))
        if self._raise is not None:
            raise self._raise
        return self._result

    async def submit_batch(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def get_batch(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def get_job_preview(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_test_app(stub: StubJobService) -> FastAPI:
    """Build a minimal FastAPI app wrapping just the jobs router."""
    app = FastAPI()
    app.include_router(jobs_router)

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


def _make_job_detail(
    job_id: UUID | None = None,
    batch_id: UUID | None = None,
    filename: str = "doc.pdf",
    status: JobStatus = JobStatus.COMPLETED,
    result: dict[str, Any] | None = None,
    pipeline_trace: dict[str, Any] | None = None,
    otel_trace_id: str | None = None,
    processing_time_ms: int | None = None,
    error_message: str | None = None,
) -> JobDetail:
    now = datetime(2025, 1, 2, 9, 30, 0, tzinfo=timezone.utc)
    return JobDetail(
        job_id=JobId(job_id or uuid4()),
        batch_id=BatchId(batch_id or uuid4()),
        filename=filename,
        file_hash=FileHash("b" * 64),
        status=status,
        result=result,
        error_message=error_message,
        pipeline_trace=pipeline_trace,
        otel_trace_id=otel_trace_id,
        processing_time_ms=processing_time_ms,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_get_job_happy_path_returns_200_with_full_detail() -> None:
    stub = StubJobService()
    job_id = uuid4()
    batch_id = uuid4()
    stub.set_result(
        _make_job_detail(
            job_id=job_id,
            batch_id=batch_id,
            filename="170154-L-001.pdf",
            status=JobStatus.COMPLETED,
            result={
                "drawing_number": "170154-L-001",
                "title": "GENERAL ARRANGEMENT",
                "document_type": "technical_drawing",
            },
            pipeline_trace={
                "stage1": {"duration_ms": 432, "model": "qwen2.5vl:7b"},
                "stage3": {"duration_ms": 12},
            },
            otel_trace_id="abc123def456",
            processing_time_ms=1842,
        )
    )

    client = TestClient(_make_test_app(stub))
    response = client.get(f"/jobs/{job_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == str(job_id)
    assert body["batch_id"] == str(batch_id)
    assert body["filename"] == "170154-L-001.pdf"
    assert body["file_hash"] == "b" * 64
    assert body["status"] == "completed"
    assert body["result"]["drawing_number"] == "170154-L-001"
    assert body["result"]["document_type"] == "technical_drawing"
    assert body["pipeline_trace"]["stage1"]["model"] == "qwen2.5vl:7b"
    assert body["otel_trace_id"] == "abc123def456"
    assert body["processing_time_ms"] == 1842
    assert body["error_message"] is None
    assert "created_at" in body and "updated_at" in body


def test_get_job_forwards_parsed_uuid_and_auth_context() -> None:
    stub = StubJobService()
    stub.set_result(_make_job_detail())
    target = uuid4()
    client = TestClient(_make_test_app(stub))
    response = client.get(f"/jobs/{target}")
    assert response.status_code == 200
    assert len(stub.get_job_calls) == 1
    forwarded_id, forwarded_auth = stub.get_job_calls[0]
    assert forwarded_id == target
    assert isinstance(forwarded_id, UUID)
    assert forwarded_auth.user_id == "test-user"


def test_get_job_failed_status_includes_error_message() -> None:
    stub = StubJobService()
    stub.set_result(
        _make_job_detail(
            status=JobStatus.FAILED,
            error_message="OllamaTimeoutError after 60s",
            result=None,
        )
    )
    client = TestClient(_make_test_app(stub))
    response = client.get(f"/jobs/{uuid4()}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_message"] == "OllamaTimeoutError after 60s"
    assert body["result"] is None


# ---------------------------------------------------------------------------
# 404 tests
# ---------------------------------------------------------------------------


def test_get_job_returns_404_when_service_raises_not_found() -> None:
    stub = StubJobService()
    stub.set_raise(NotFoundError("Job not in repo"))
    client = TestClient(_make_test_app(stub))
    response = client.get(f"/jobs/{uuid4()}")
    assert response.status_code == 404
    assert "Job not in repo" in response.json()["detail"]


def test_get_job_returns_404_when_service_returns_none() -> None:
    """Defensive fallback for stale adapters that return None."""
    stub = StubJobService()
    stub.set_result(None)
    client = TestClient(_make_test_app(stub))
    response = client.get(f"/jobs/{uuid4()}")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 400 test
# ---------------------------------------------------------------------------


def test_get_job_rejects_malformed_uuid_with_400() -> None:
    stub = StubJobService()
    stub.set_result(_make_job_detail())
    client = TestClient(_make_test_app(stub))
    response = client.get("/jobs/not-a-uuid")
    assert response.status_code == 400
    assert "Invalid job_id" in response.json()["detail"]
    assert stub.get_job_calls == []
