"""Domain protocols (interfaces) for the Zubot Smart Ingestion Service.

This module is the single source of truth for every cross-layer interface.
All protocols are marked @runtime_checkable so mypy and runtime isinstance()
checks can verify conformance. Infrastructure adapters implement these
protocols; domain logic and application services depend on them.

Every signature here mirrors boundary-contracts.md §4 (Module Boundary
Contracts) exactly. Do not add methods without first updating the contracts
document.

Per the dependency rules (boundary-contracts.md §3), this module may only
import from stdlib, `zubot_ingestion.shared.types`, and
`zubot_ingestion.domain.entities`. It MUST NOT import from api, services, or
infrastructure.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar, runtime_checkable
from uuid import UUID

from zubot_ingestion.domain.entities import (
    Batch,
    CompanionResult,
    ConfidenceAssessment,
    ExtractionResult,
    FilenameHints,
    Job,
    OllamaResponse,
    PDFData,
    PipelineContext,
    PipelineResult,
    RenderedPage,
    ReviewAction,
    SidecarDocument,
    ValidationResult,
)
from zubot_ingestion.domain.enums import JobStatus
from zubot_ingestion.shared.types import (
    AuthContext,
    BatchSubmissionResult,
    BatchWithJobs,
    JobDetail,
    PaginatedResult,
    RateLimitResult,
    ReviewCorrections,
    ReviewQueueItem,
    ReviewResult,
    SubmissionParams,
    TaskStatus,
    UploadedFile,
)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Presentation layer protocols (§4.2, §4.3)
# ---------------------------------------------------------------------------


@runtime_checkable
class IAuthMiddleware(Protocol):
    """Middleware protocol for request authentication (§4.2)."""

    async def authenticate(self, request: Any) -> AuthContext:
        """Validate credentials from request headers.

        Checks for X-API-Key header (service-to-service) or
        Authorization: Bearer <jwt> (user session).

        Args:
            request: FastAPI Request object with headers

        Returns:
            AuthContext with user_id, auth_method, deployment_id, permissions

        Raises:
            AuthenticationError: If credentials are missing or invalid
        """
        ...


@runtime_checkable
class IRateLimiter(Protocol):
    """Rate limiting protocol for request throttling (§4.3)."""

    async def check_limit(
        self,
        key: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitResult:
        """Check if a request key is within its rate limit.

        Uses a sliding window algorithm backed by Redis.

        Args:
            key: Unique identifier for the rate limit bucket (e.g., user_id, api_key)
            limit: Maximum allowed requests in the window
            window_seconds: Duration of the sliding window

        Returns:
            RateLimitResult with allowed status, remaining quota, and reset time
        """
        ...


# ---------------------------------------------------------------------------
# Application layer protocols (§4.4–§4.7)
# ---------------------------------------------------------------------------


@runtime_checkable
class IJobService(Protocol):
    """Application service for job and batch management (§4.4)."""

    async def submit_batch(
        self,
        files: list[UploadedFile],
        params: SubmissionParams,
        auth_context: AuthContext,
    ) -> BatchSubmissionResult:
        """Submit a batch of PDFs for extraction."""
        ...

    async def get_batch(
        self,
        batch_id: UUID,
        auth_context: AuthContext,
    ) -> BatchWithJobs | None:
        """Retrieve batch status with all jobs."""
        ...

    async def get_job(
        self,
        job_id: UUID,
        auth_context: AuthContext,
    ) -> JobDetail | None:
        """Retrieve single job with full extraction results."""
        ...

    async def get_job_preview(
        self,
        job_id: UUID,
        auth_context: AuthContext,
    ) -> bytes | None:
        """Retrieve rendered page image for human review."""
        ...


@runtime_checkable
class IReviewService(Protocol):
    """Application service for human review workflow (§4.5)."""

    async def get_pending_reviews(
        self,
        page: int,
        per_page: int,
        auth_context: AuthContext,
    ) -> PaginatedResult[ReviewQueueItem]:
        """List jobs awaiting human review."""
        ...

    async def approve(
        self,
        job_id: UUID,
        corrections: ReviewCorrections | None,
        reviewed_by: str,
        auth_context: AuthContext,
    ) -> ReviewResult:
        """Approve an extraction with optional corrections."""
        ...

    async def reject(
        self,
        job_id: UUID,
        reason: str,
        reviewed_by: str,
        auth_context: AuthContext,
    ) -> ReviewResult:
        """Reject an extraction."""
        ...


@runtime_checkable
class IOrchestrator(Protocol):
    """Orchestrates the three-stage extraction pipeline (§4.6)."""

    async def run_pipeline(
        self,
        job: Job,
        pdf_bytes: bytes,
    ) -> PipelineResult:
        """Execute the complete extraction pipeline."""
        ...


@runtime_checkable
class ITaskQueue(Protocol):
    """Task queue interface for async job processing (§4.7)."""

    def enqueue_extraction(self, job_id: UUID) -> str:
        """Enqueue a job for async extraction processing."""
        ...

    def get_task_status(self, task_id: str) -> TaskStatus:
        """Check status of an enqueued task."""
        ...


# ---------------------------------------------------------------------------
# Domain layer protocols (§4.8–§4.14)
# ---------------------------------------------------------------------------


@runtime_checkable
class IExtractor(Protocol):
    """Stage 1: Multi-source metadata extraction (§4.8)."""

    async def extract(self, context: PipelineContext) -> ExtractionResult:
        """Extract drawing number, title, and document type."""
        ...


@runtime_checkable
class ICompanionGenerator(Protocol):
    """Stage 2: Visual description generation (§4.9)."""

    async def generate(
        self,
        context: PipelineContext,
        extraction_result: ExtractionResult,
    ) -> CompanionResult:
        """Generate visual descriptions of document pages."""
        ...


@runtime_checkable
class ICompanionValidator(Protocol):
    """Validate companion descriptions against extraction results (§4.10)."""

    def validate(
        self,
        companion_text: str,
        extraction_result: ExtractionResult,
    ) -> ValidationResult:
        """Cross-check companion description with extracted metadata."""
        ...


@runtime_checkable
class ISidecarBuilder(Protocol):
    """Stage 3: Sidecar document assembly (§4.11)."""

    def build(
        self,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult | None,
        job: Job,
    ) -> SidecarDocument:
        """Build Bedrock KB sidecar document."""
        ...


@runtime_checkable
class IConfidenceCalculator(Protocol):
    """Calculate overall confidence and assign tier (§4.12)."""

    def calculate(
        self,
        extraction_result: ExtractionResult,
        validation_result: ValidationResult | None,
    ) -> ConfidenceAssessment:
        """Compute overall confidence score and determine tier."""
        ...


@runtime_checkable
class IFilenameParser(Protocol):
    """Extract metadata hints from filename patterns (§4.13)."""

    def parse(self, filename: str) -> FilenameHints:
        """Parse filename for drawing number and revision hints."""
        ...


@runtime_checkable
class IResponseParser(Protocol):
    """Parse and repair Ollama JSON responses (§4.14)."""

    def parse(self, raw_response: str, expected_schema: type[T]) -> T:
        """Parse Ollama response with repair fallbacks."""
        ...


# ---------------------------------------------------------------------------
# Infrastructure layer protocols (§4.15–§4.20)
# ---------------------------------------------------------------------------


@runtime_checkable
class IPDFProcessor(Protocol):
    """PDF processing interface for text extraction and page rendering (§4.15)."""

    def load(self, pdf_bytes: bytes) -> PDFData:
        """Load PDF and extract basic information."""
        ...

    def extract_text(self, pdf_bytes: bytes) -> str:
        """Extract all text content from PDF."""
        ...

    def render_page(
        self,
        pdf_bytes: bytes,
        page_number: int,
        dpi: int = 200,
        scale: float = 2.0,
    ) -> RenderedPage:
        """Render a single page to JPEG image."""
        ...

    def render_pages(
        self,
        pdf_bytes: bytes,
        page_numbers: list[int],
        dpi: int = 200,
        scale: float = 2.0,
    ) -> list[RenderedPage]:
        """Render multiple pages to JPEG images."""
        ...


@runtime_checkable
class IOllamaClient(Protocol):
    """Ollama API client for vision and text inference (§4.16)."""

    async def generate_vision(
        self,
        image_base64: str,
        prompt: str,
        model: str = "qwen2.5vl:7b",
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
    ) -> OllamaResponse:
        """Generate response from vision model with image input."""
        ...

    async def generate_text(
        self,
        text: str,
        prompt: str,
        model: str = "llama3.1:8b",
        temperature: float = 0.0,
        timeout_seconds: float = 30.0,
    ) -> OllamaResponse:
        """Generate response from text model."""
        ...

    async def check_model_available(self, model: str) -> bool:
        """Check if a model is loaded and available."""
        ...


@runtime_checkable
class IJobRepository(Protocol):
    """Repository interface for job and batch persistence (§4.17)."""

    async def create_batch(self, batch: Batch, jobs: list[Job]) -> Batch:
        """Create batch with jobs in a single transaction."""
        ...

    async def get_batch(self, batch_id: UUID) -> Batch | None:
        """Retrieve batch by ID."""
        ...

    async def get_batch_with_jobs(
        self,
        batch_id: UUID,
    ) -> tuple[Batch, list[Job]] | None:
        """Retrieve batch with all associated jobs."""
        ...

    async def get_job(self, job_id: UUID) -> Job | None:
        """Retrieve job by ID."""
        ...

    async def get_job_by_file_hash(self, file_hash: str) -> Job | None:
        """Retrieve job by file hash (for deduplication)."""
        ...

    async def update_job_status(
        self,
        job_id: UUID,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update job status and optionally result/error."""
        ...

    async def get_pending_reviews(
        self,
        page: int,
        per_page: int,
    ) -> PaginatedResult[Job]:
        """Get jobs with confidence_tier='review' for human review."""
        ...

    async def create_review_action(
        self,
        review_action: ReviewAction,
    ) -> ReviewAction:
        """Record a review action (approval or rejection)."""
        ...


@runtime_checkable
class IMetadataWriter(Protocol):
    """Metadata writer interface for vector store enrichment (§4.18)."""

    async def write_metadata(
        self,
        document_id: str,
        sidecar: SidecarDocument,
        deployment_id: int | None,
        node_id: int | None,
    ) -> bool:
        """Write enriched metadata to ChromaDB collection."""
        ...

    async def check_connection(self) -> bool:
        """Check ChromaDB connection health."""
        ...


@runtime_checkable
class ISearchIndexer(Protocol):
    """Search indexer interface for companion document indexing (§4.19)."""

    async def index_companion(
        self,
        document_id: str,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult,
        deployment_id: int | None,
    ) -> bool:
        """Index companion document in Elasticsearch."""
        ...

    async def check_connection(self) -> bool:
        """Check Elasticsearch connection health."""
        ...


@runtime_checkable
class ICallbackClient(Protocol):
    """Callback client interface for webhook notifications (§4.20)."""

    async def notify_completion(
        self,
        callback_url: str,
        job: Job,
        api_key: str,
    ) -> bool:
        """Send job completion notification to callback URL."""
        ...


__all__ = [
    "IAuthMiddleware",
    "ICallbackClient",
    "ICompanionGenerator",
    "ICompanionValidator",
    "IConfidenceCalculator",
    "IExtractor",
    "IFilenameParser",
    "IJobRepository",
    "IJobService",
    "IMetadataWriter",
    "IOllamaClient",
    "IOrchestrator",
    "IPDFProcessor",
    "IRateLimiter",
    "IResponseParser",
    "IReviewService",
    "ISearchIndexer",
    "ISidecarBuilder",
    "ITaskQueue",
]
