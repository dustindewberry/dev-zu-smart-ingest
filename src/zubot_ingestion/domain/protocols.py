"""Domain Protocol interfaces (transitional stub).

Only the IJobRepository protocol required by the database repository module
is defined here. The canonical full set of protocols is produced by task-2
(contracts-and-types-definition); on merge, the canonical version must
overwrite this file.

This stub defines IJobRepository with the method set documented in the
task description and the architecture proposal:
- create_batch
- get_batch
- get_job
- update_job_status
- update_job_result
- list_pending_reviews
- find_by_file_hash
- create_review_action
- update_batch_status
- list_jobs_by_batch
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from zubot_ingestion.domain.entities import (
    Batch,
    BatchWithJobs,
    Job,
    ReviewAction,
)
from zubot_ingestion.domain.enums import (
    BatchStatus,
    ConfidenceTier,
    JobStatus,
    ReviewActionType,
)


@runtime_checkable
class IJobRepository(Protocol):
    """Persistence protocol for batches, jobs, and review actions.

    Implementations must be async and atomic at method granularity.
    create_batch must persist the batch and all associated jobs in a
    single transaction.
    """

    async def create_batch(
        self,
        batch: Batch,
        jobs: list[Job],
    ) -> Batch:
        """Insert batch + jobs in a single transaction. Returns persisted batch."""
        ...

    async def get_batch(self, batch_id: UUID) -> BatchWithJobs | None:
        """Fetch a batch and its jobs. Returns None if not found."""
        ...

    async def get_job(self, job_id: UUID) -> Job | None:
        """Fetch a single job by id. Returns None if not found."""
        ...

    async def update_job_status(
        self,
        job_id: UUID,
        status: JobStatus,
        error_message: str | None = None,
    ) -> Job | None:
        """Atomically update job status (and optional error message)."""
        ...

    async def update_job_result(
        self,
        job_id: UUID,
        result: dict[str, Any],
        confidence_tier: ConfidenceTier,
        confidence_score: float,
        processing_time_ms: int,
        otel_trace_id: str | None = None,
        pipeline_trace: dict[str, Any] | None = None,
    ) -> Job | None:
        """Atomically update job result fields and mark completed."""
        ...

    async def list_pending_reviews(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[Job], int]:
        """Return paginated list of jobs in REVIEW tier and total count."""
        ...

    async def find_by_file_hash(self, file_hash: str) -> Job | None:
        """Return the most recent completed job with given file_hash, if any.

        Used for deduplication on submission.
        """
        ...

    async def create_review_action(
        self,
        review: ReviewAction,
    ) -> ReviewAction:
        """Persist a ReviewAction audit record."""
        ...

    async def update_batch_status(
        self,
        batch_id: UUID,
        status: BatchStatus,
    ) -> Batch | None:
        """Atomically update batch status."""
        ...

    async def list_jobs_by_batch(self, batch_id: UUID) -> list[Job]:
        """Return all jobs belonging to a batch."""
        ...
