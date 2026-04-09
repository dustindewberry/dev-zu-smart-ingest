"""Unit tests for the human review router (CAP-022, step-20).

These tests run entirely against a fake in-memory :class:`FakeJobRepository`
and an in-memory slowapi limiter so they do not need PostgreSQL, Redis,
or the full application composition root.

Coverage:
    * ``GET /review/pending`` pagination (happy path + offset slicing + empty case).
    * ``POST /review/{job_id}/approve`` happy path, 404, and 409 (wrong
      confidence tier).
    * ``POST /review/{job_id}/reject`` happy path.
    * Rate-limit header presence on read and write endpoints.

The approach is deliberately simple: the prod review router imports its
``limiter`` from :mod:`zubot_ingestion.api.middleware.rate_limit` at
import time, and the module-level limiter is constructed with whatever
``REDIS_URL`` happens to be set. We set ``REDIS_URL=memory://`` at test
module import time (before any ``from zubot_ingestion.api ...`` imports)
so the module-level limiter is backed by the in-memory ``limits`` storage
— identical behaviour to the production Redis path.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Hermetic env setup — MUST run before any zubot_ingestion.api imports so the
# module-level rate-limit singleton lands on a memory-backed storage.
# ---------------------------------------------------------------------------
os.environ["REDIS_URL"] = "memory://"

from contextlib import asynccontextmanager  # noqa: E402
from dataclasses import replace  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from typing import Any  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402
from slowapi.middleware import SlowAPIMiddleware  # noqa: E402

from zubot_ingestion.api.middleware.rate_limit import (  # noqa: E402
    custom_429_handler,
    limiter as prod_limiter,
)
from zubot_ingestion.api.routes import review as review_routes  # noqa: E402
from zubot_ingestion.domain.entities import Job  # noqa: E402
from zubot_ingestion.domain.enums import ConfidenceTier, JobStatus  # noqa: E402
from zubot_ingestion.shared.types import (  # noqa: E402
    BatchId,
    FileHash,
    JobId,
    PaginatedResult,
)


# ---------------------------------------------------------------------------
# Fake repository
# ---------------------------------------------------------------------------


class FakeJobRepository:
    """In-memory fake that implements the subset of IJobRepository the router uses.

    Stores jobs in a dict keyed by UUID. Implements ``get_pending_reviews``,
    ``get_job``, and ``update_job_status`` — the exact methods the review
    router calls. Tests inspect the ``jobs`` attribute directly to verify
    persistence side effects.
    """

    def __init__(self) -> None:
        self.jobs: dict[UUID, Job] = {}

    async def get_pending_reviews(
        self,
        page: int,
        per_page: int,
    ) -> PaginatedResult[Job]:
        pending = [
            j
            for j in self.jobs.values()
            if j.confidence_tier == ConfidenceTier.REVIEW
            and j.status != JobStatus.REJECTED
        ]
        # Mirror the concrete repo's ordering (created_at DESC).
        pending.sort(key=lambda j: j.created_at, reverse=True)
        total = len(pending)
        start = (max(1, page) - 1) * max(1, per_page)
        end = start + max(1, per_page)
        page_items = pending[start:end]
        total_pages = (total + per_page - 1) // per_page if per_page > 0 else 0
        return PaginatedResult[Job](
            items=page_items,
            total=total,
            page=page,
            per_page=per_page,
            total_pages=total_pages,
        )

    async def get_job(self, job_id: UUID) -> Job | None:
        return self.jobs.get(job_id)

    async def update_job_status(
        self,
        job_id: UUID,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        updated = replace(
            job,
            status=status,
            result=result if result is not None else job.result,
            error_message=(
                error_message if error_message is not None else job.error_message
            ),
            updated_at=datetime.now(timezone.utc),
        )
        self.jobs[job_id] = updated
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(
    *,
    job_id: UUID | None = None,
    confidence_tier: ConfidenceTier | None = ConfidenceTier.REVIEW,
    status: JobStatus = JobStatus.REVIEW,
    filename: str = "doc.pdf",
    result: dict[str, Any] | None = None,
    overall_confidence: float = 0.42,
    created_at: datetime | None = None,
) -> Job:
    """Build a frozen :class:`Job` for fixture seeding."""
    jid = job_id or uuid4()
    return Job(
        job_id=JobId(jid),
        batch_id=BatchId(uuid4()),
        filename=filename,
        file_hash=FileHash("0" * 64),
        file_path=f"/tmp/{filename}",
        status=status,
        result=(
            result
            if result is not None
            else {
                "drawing_number": "DWG-001",
                "title": "Sample title",
            }
        ),
        error_message=None,
        pipeline_trace=None,
        otel_trace_id=None,
        processing_time_ms=42,
        created_at=created_at or datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        confidence_tier=confidence_tier,
        overall_confidence=overall_confidence,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo() -> FakeJobRepository:
    return FakeJobRepository()


@pytest.fixture
def client(
    fake_repo: FakeJobRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Build a hermetic FastAPI TestClient wired to the real review router.

    The review router imports ``get_job_repository`` from
    :mod:`zubot_ingestion.services`; we monkeypatch that symbol on the
    review_routes module to yield an in-memory fake so no database or
    session is required.

    The rate limiter captured by the router's ``@limiter.limit(...)``
    decorators is the module-level ``prod_limiter`` singleton. It is
    backed by :class:`limits.storage.MemoryStorage` because this test
    module sets ``REDIS_URL=memory://`` before any zubot_ingestion import.
    We wipe the limiter's storage between tests so rate-limit counters
    do not bleed across test functions.
    """

    @asynccontextmanager
    async def _fake_get_job_repository() -> Any:
        yield fake_repo

    monkeypatch.setattr(
        review_routes,
        "get_job_repository",
        _fake_get_job_repository,
    )

    # Reset the limiter's storage so each test starts with a clean quota.
    try:
        prod_limiter.limiter.storage.reset()
    except Exception:  # pragma: no cover - defensive
        pass

    app = FastAPI()
    app.state.limiter = prod_limiter
    app.add_exception_handler(RateLimitExceeded, custom_429_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.include_router(review_routes.router)

    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /review/pending
# ---------------------------------------------------------------------------


class TestGetPending:
    def test_empty_queue_returns_empty_items(
        self,
        client: TestClient,
    ) -> None:
        response = client.get("/review/pending")
        assert response.status_code == 200
        body = response.json()
        assert body == {"items": [], "limit": 50, "offset": 0, "total": 0}

    def test_returns_review_tier_jobs(
        self,
        client: TestClient,
        fake_repo: FakeJobRepository,
    ) -> None:
        j1 = _make_job()
        j2 = _make_job()
        fake_repo.jobs[j1.job_id] = j1
        fake_repo.jobs[j2.job_id] = j2

        response = client.get("/review/pending?limit=10&offset=0")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert body["limit"] == 10
        assert body["offset"] == 0
        assert len(body["items"]) == 2
        ids = {item["job_id"] for item in body["items"]}
        assert ids == {str(j1.job_id), str(j2.job_id)}
        item = body["items"][0]
        for key in (
            "job_id",
            "drawing_number",
            "title",
            "confidence_tier",
            "confidence_score",
            "created_at",
        ):
            assert key in item
        assert item["confidence_tier"] == "review"

    def test_pagination_slices_results(
        self,
        client: TestClient,
        fake_repo: FakeJobRepository,
    ) -> None:
        jobs = []
        for i in range(5):
            ts = datetime(2026, 1, 1, 12, i, 0, tzinfo=timezone.utc)
            j = _make_job(created_at=ts, filename=f"doc{i}.pdf")
            fake_repo.jobs[j.job_id] = j
            jobs.append(j)

        r1 = client.get("/review/pending?limit=2&offset=0")
        assert r1.status_code == 200
        b1 = r1.json()
        assert b1["total"] == 5
        assert b1["limit"] == 2
        assert b1["offset"] == 0
        assert len(b1["items"]) == 2

        r2 = client.get("/review/pending?limit=2&offset=2")
        assert r2.status_code == 200
        b2 = r2.json()
        assert b2["total"] == 5
        assert b2["offset"] == 2
        assert len(b2["items"]) == 2

        r3 = client.get("/review/pending?limit=2&offset=4")
        assert r3.status_code == 200
        b3 = r3.json()
        assert b3["total"] == 5
        assert len(b3["items"]) == 1

        seen = (
            [i["job_id"] for i in b1["items"]]
            + [i["job_id"] for i in b2["items"]]
            + [i["job_id"] for i in b3["items"]]
        )
        assert set(seen) == {str(j.job_id) for j in jobs}

    def test_excludes_non_review_tier_jobs(
        self,
        client: TestClient,
        fake_repo: FakeJobRepository,
    ) -> None:
        auto_job = _make_job(
            confidence_tier=ConfidenceTier.AUTO,
            status=JobStatus.COMPLETED,
        )
        review_job = _make_job()
        fake_repo.jobs[auto_job.job_id] = auto_job
        fake_repo.jobs[review_job.job_id] = review_job

        response = client.get("/review/pending")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["job_id"] == str(review_job.job_id)


# ---------------------------------------------------------------------------
# POST /review/{job_id}/approve
# ---------------------------------------------------------------------------


class TestApprove:
    def test_happy_path_transitions_to_completed(
        self,
        client: TestClient,
        fake_repo: FakeJobRepository,
    ) -> None:
        job = _make_job()
        fake_repo.jobs[job.job_id] = job

        response = client.post(
            f"/review/{job.job_id}/approve",
            json={"reviewer_id": "alice", "notes": "Looks good to me"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["job_id"] == str(job.job_id)
        assert body["status"] == "completed"
        assert body["result"]["review"]["action"] == "approved"
        assert body["result"]["review"]["reviewer_id"] == "alice"
        assert body["result"]["review"]["notes"] == "Looks good to me"
        # Original extraction fields are preserved alongside the review sub-blob.
        assert body["result"]["drawing_number"] == "DWG-001"
        assert body["result"]["title"] == "Sample title"

        # Verify persistence side effect on the fake.
        persisted = fake_repo.jobs[job.job_id]
        assert persisted.status == JobStatus.COMPLETED
        assert persisted.result is not None
        assert persisted.result["review"]["action"] == "approved"
        assert persisted.result["review"]["reviewer_id"] == "alice"

    def test_approve_not_found_returns_404(
        self,
        client: TestClient,
    ) -> None:
        response = client.post(
            f"/review/{uuid4()}/approve",
            json={"reviewer_id": "alice", "notes": None},
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_approve_wrong_tier_returns_409(
        self,
        client: TestClient,
        fake_repo: FakeJobRepository,
    ) -> None:
        job = _make_job(
            confidence_tier=ConfidenceTier.AUTO,
            status=JobStatus.COMPLETED,
        )
        fake_repo.jobs[job.job_id] = job

        response = client.post(
            f"/review/{job.job_id}/approve",
            json={"reviewer_id": "alice", "notes": None},
        )
        assert response.status_code == 409
        body = response.json()
        assert "review tier" in body["detail"].lower()

        # Side-effect check: the job must NOT have transitioned.
        persisted = fake_repo.jobs[job.job_id]
        assert persisted.status == JobStatus.COMPLETED  # unchanged
        assert persisted.confidence_tier == ConfidenceTier.AUTO
        assert persisted.result == {
            "drawing_number": "DWG-001",
            "title": "Sample title",
        }  # not mutated


# ---------------------------------------------------------------------------
# POST /review/{job_id}/reject
# ---------------------------------------------------------------------------


class TestReject:
    def test_happy_path_transitions_to_failed_with_reason(
        self,
        client: TestClient,
        fake_repo: FakeJobRepository,
    ) -> None:
        job = _make_job()
        fake_repo.jobs[job.job_id] = job

        response = client.post(
            f"/review/{job.job_id}/reject",
            json={"reviewer_id": "bob", "reason": "illegible drawing"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["job_id"] == str(job.job_id)
        assert body["status"] == "failed"
        assert body["error_message"] == "Rejected by bob: illegible drawing"

        # Verify persistence side effect.
        persisted = fake_repo.jobs[job.job_id]
        assert persisted.status == JobStatus.FAILED
        assert persisted.error_message == "Rejected by bob: illegible drawing"

    def test_reject_not_found_returns_404(
        self,
        client: TestClient,
    ) -> None:
        response = client.post(
            f"/review/{uuid4()}/reject",
            json={"reviewer_id": "bob", "reason": "illegible"},
        )
        assert response.status_code == 404

    def test_reject_wrong_tier_returns_409(
        self,
        client: TestClient,
        fake_repo: FakeJobRepository,
    ) -> None:
        job = _make_job(
            confidence_tier=ConfidenceTier.AUTO,
            status=JobStatus.COMPLETED,
        )
        fake_repo.jobs[job.job_id] = job

        response = client.post(
            f"/review/{job.job_id}/reject",
            json={"reviewer_id": "bob", "reason": "illegible"},
        )
        assert response.status_code == 409

        # Side-effect check: the job must NOT have transitioned.
        persisted = fake_repo.jobs[job.job_id]
        assert persisted.status == JobStatus.COMPLETED
        assert persisted.error_message is None


# ---------------------------------------------------------------------------
# Rate-limit header presence
# ---------------------------------------------------------------------------


class TestRateLimitHeaders:
    def test_pending_has_rate_limit_headers(
        self,
        client: TestClient,
    ) -> None:
        response = client.get("/review/pending")
        assert response.status_code == 200
        # slowapi's ``headers_enabled=True`` installs these on every
        # response from a rate-limited route.
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers

    def test_approve_has_rate_limit_headers(
        self,
        client: TestClient,
        fake_repo: FakeJobRepository,
    ) -> None:
        job = _make_job()
        fake_repo.jobs[job.job_id] = job
        response = client.post(
            f"/review/{job.job_id}/approve",
            json={"reviewer_id": "alice", "notes": None},
        )
        assert response.status_code == 200
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers

    def test_reject_has_rate_limit_headers(
        self,
        client: TestClient,
        fake_repo: FakeJobRepository,
    ) -> None:
        job = _make_job()
        fake_repo.jobs[job.job_id] = job
        response = client.post(
            f"/review/{job.job_id}/reject",
            json={"reviewer_id": "bob", "reason": "illegible"},
        )
        assert response.status_code == 200
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers
