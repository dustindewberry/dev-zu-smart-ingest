"""Unit tests for POST /extract (CAP-009).

Uses FastAPI's :class:`TestClient` with ``dependency_overrides`` to swap
the real :class:`JobService` for a stub. The auth middleware is also
bypassed by pre-setting ``request.state.auth_context`` via an override of
``get_auth_context``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zubot_ingestion.api.middleware.auth import get_auth_context
from zubot_ingestion.api.routes.extract import (
    get_job_service,
    router as extract_router,
)
from zubot_ingestion.domain.enums import JobStatus
from zubot_ingestion.services.job_service import (
    InvalidPDFError,
    OversizeFileError,
)
from zubot_ingestion.shared.types import (
    AuthContext,
    BatchId,
    BatchSubmissionResult,
    FileHash,
    JobId,
    JobSummary,
)


# ---------------------------------------------------------------------------
# Stub job service
# ---------------------------------------------------------------------------


class StubJobService:
    """Controllable stand-in for IJobService.

    Tests can swap out ``self.submit_batch_impl`` to return custom results
    or raise exceptions. By default it returns a well-formed
    :class:`BatchSubmissionResult` with one QUEUED job.
    """

    def __init__(self) -> None:
        self.submit_calls: list[dict[str, Any]] = []
        self._result_factory = self._default_result
        self._raise: Exception | None = None

    def set_raise(self, exc: Exception) -> None:
        self._raise = exc

    def set_result_factory(self, factory) -> None:
        self._result_factory = factory

    async def submit_batch(self, files, params, auth_context):  # type: ignore[no-untyped-def]
        self.submit_calls.append(
            {
                "files": list(files),
                "params": params,
                "auth_context": auth_context,
            }
        )
        if self._raise is not None:
            raise self._raise
        return self._result_factory(files)

    @staticmethod
    def _default_result(files) -> BatchSubmissionResult:  # type: ignore[no-untyped-def]
        batch_id = BatchId(uuid4())
        summaries = [
            JobSummary(
                job_id=JobId(uuid4()),
                filename=f.filename,
                status=JobStatus.QUEUED,
                file_hash=FileHash("0" * 64),
                result=None,
            )
            for f in files
        ]
        return BatchSubmissionResult(
            batch_id=batch_id,
            jobs=summaries,
            total=len(summaries),
            poll_url=f"/batches/{batch_id}",
        )

    # The router also types the dependency as ``IJobService``; only
    # ``submit_batch`` is reached from the /extract route, so we don't
    # implement the rest here.
    async def get_batch(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def get_job(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def get_job_preview(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


# ---------------------------------------------------------------------------
# Test-app factory
# ---------------------------------------------------------------------------


def _make_test_app(stub: StubJobService) -> FastAPI:
    """Build a minimal FastAPI app wrapping just the extract router.

    Auth is bypassed by overriding ``get_auth_context`` to return a fixed
    :class:`AuthContext`. The real :func:`get_job_service` factory is
    overridden to yield the provided stub.
    """
    app = FastAPI()
    app.include_router(extract_router)

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


def _pdf_bytes(extra: bytes = b"") -> bytes:
    return b"%PDF-1.4\n" + extra + b"\n%%EOF\n"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_post_extract_returns_202_and_batch_summary():
    stub = StubJobService()
    client = TestClient(_make_test_app(stub))

    response = client.post(
        "/extract",
        files={
            "files": ("one.pdf", _pdf_bytes(b"a"), "application/pdf"),
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert "batch_id" in body
    assert body["total"] == 1
    assert body["jobs"][0]["filename"] == "one.pdf"
    assert body["jobs"][0]["status"] == "queued"
    assert body["poll_url"].startswith("/batches/")


def test_post_extract_accepts_multiple_files():
    stub = StubJobService()
    client = TestClient(_make_test_app(stub))

    response = client.post(
        "/extract",
        files=[
            ("files", ("a.pdf", _pdf_bytes(b"a"), "application/pdf")),
            ("files", ("b.pdf", _pdf_bytes(b"b"), "application/pdf")),
        ],
    )
    assert response.status_code == 202
    body = response.json()
    assert body["total"] == 2
    assert {j["filename"] for j in body["jobs"]} == {"a.pdf", "b.pdf"}
    assert len(stub.submit_calls) == 1
    assert len(stub.submit_calls[0]["files"]) == 2


def test_post_extract_passes_form_params_through():
    stub = StubJobService()
    client = TestClient(_make_test_app(stub))

    response = client.post(
        "/extract",
        files={
            "files": ("one.pdf", _pdf_bytes(), "application/pdf"),
        },
        data={
            "mode": "drawing",
            "callback_url": "https://example.test/cb",
            "deployment_id": "42",
            "node_id": "7",
        },
    )
    assert response.status_code == 202
    params = stub.submit_calls[0]["params"]
    assert params.mode.value == "drawing"
    assert params.callback_url == "https://example.test/cb"
    assert params.deployment_id == 42
    assert params.node_id == 7


def test_post_extract_defaults_mode_to_auto():
    stub = StubJobService()
    client = TestClient(_make_test_app(stub))
    client.post(
        "/extract",
        files={"files": ("one.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert stub.submit_calls[0]["params"].mode.value == "auto"


def test_post_extract_rejects_invalid_mode():
    stub = StubJobService()
    client = TestClient(_make_test_app(stub))
    response = client.post(
        "/extract",
        files={"files": ("one.pdf", _pdf_bytes(), "application/pdf")},
        data={"mode": "bogus"},
    )
    assert response.status_code == 400
    assert "Invalid mode" in response.json()["detail"]


def test_post_extract_rejects_non_numeric_deployment_id():
    stub = StubJobService()
    client = TestClient(_make_test_app(stub))
    response = client.post(
        "/extract",
        files={"files": ("one.pdf", _pdf_bytes(), "application/pdf")},
        data={"deployment_id": "not-a-number"},
    )
    assert response.status_code == 400


def test_post_extract_invalid_pdf_raised_by_service_maps_to_400():
    stub = StubJobService()
    stub.set_raise(InvalidPDFError("nope.pdf is not a valid PDF"))
    client = TestClient(_make_test_app(stub))
    response = client.post(
        "/extract",
        files={"files": ("nope.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert response.status_code == 400
    assert "not a valid PDF" in response.json()["detail"]


def test_post_extract_oversize_raised_by_service_maps_to_413():
    stub = StubJobService()
    stub.set_raise(OversizeFileError("huge.pdf exceeds max size of 1024 bytes"))
    client = TestClient(_make_test_app(stub))
    response = client.post(
        "/extract",
        files={"files": ("huge.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert response.status_code == 413
    assert "exceeds max size" in response.json()["detail"]


def test_post_extract_passes_auth_context_to_service():
    stub = StubJobService()
    client = TestClient(_make_test_app(stub))
    client.post(
        "/extract",
        files={"files": ("one.pdf", _pdf_bytes(), "application/pdf")},
    )
    auth = stub.submit_calls[0]["auth_context"]
    assert auth.user_id == "test-user"
    assert auth.auth_method == "api_key"


def test_post_extract_empty_deployment_id_becomes_none():
    stub = StubJobService()
    client = TestClient(_make_test_app(stub))
    response = client.post(
        "/extract",
        files={"files": ("one.pdf", _pdf_bytes(), "application/pdf")},
        data={"deployment_id": "", "node_id": ""},
    )
    assert response.status_code == 202
    params = stub.submit_calls[0]["params"]
    assert params.deployment_id is None
    assert params.node_id is None


def test_post_extract_requires_files():
    stub = StubJobService()
    client = TestClient(_make_test_app(stub))
    response = client.post("/extract", data={"mode": "auto"})
    # FastAPI returns 422 for missing multipart form files (pydantic validation)
    assert response.status_code in (400, 422)


def test_serialized_batch_submission_result_shape():
    """Regression guard for the JSON shape returned to clients."""
    stub = StubJobService()
    client = TestClient(_make_test_app(stub))
    response = client.post(
        "/extract",
        files={"files": ("x.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert response.status_code == 202
    body = response.json()
    assert set(body.keys()) == {"batch_id", "total", "poll_url", "jobs"}
    job = body["jobs"][0]
    assert set(job.keys()) == {"job_id", "filename", "status", "file_hash", "result"}
