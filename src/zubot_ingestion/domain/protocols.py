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
        """Submit a batch of PDFs for extraction.

        Creates batch and job records, checks for duplicates via file_hash,
        saves files to temp storage, and enqueues Celery tasks for new jobs.
        Returns immediately with batch_id for async polling.

        Args:
            files: List of uploaded PDF files with bytes and metadata
            params: Submission parameters (mode, callback_url, deployment_id, node_id)
            auth_context: Authenticated user context

        Returns:
            BatchSubmissionResult with batch_id, job list, and poll_url

        Raises:
            ValidationError: If files are invalid (not PDF, too large)
            RateLimitError: If user has exceeded submission limits
        """
        ...

    async def get_batch(
        self,
        batch_id: UUID,
        auth_context: AuthContext,
    ) -> BatchWithJobs | None:
        """Retrieve batch status with all jobs.

        Args:
            batch_id: UUID of the batch
            auth_context: Authenticated user context for authorization

        Returns:
            BatchWithJobs if found and authorized, None if not found

        Raises:
            AuthorizationError: If user cannot access this batch
        """
        ...

    async def get_job(
        self,
        job_id: UUID,
        auth_context: AuthContext,
    ) -> JobDetail | None:
        """Retrieve single job with full extraction results.

        Args:
            job_id: UUID of the job
            auth_context: Authenticated user context for authorization

        Returns:
            JobDetail if found and authorized, None if not found

        Raises:
            AuthorizationError: If user cannot access this job
        """
        ...

    async def get_job_preview(
        self,
        job_id: UUID,
        auth_context: AuthContext,
    ) -> bytes | None:
        """Retrieve rendered page image for human review.

        Args:
            job_id: UUID of the job
            auth_context: Authenticated user context for authorization

        Returns:
            JPEG image bytes if available, None if not found or not rendered

        Raises:
            AuthorizationError: If user cannot access this job
        """
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
        """List jobs awaiting human review.

        Returns jobs with confidence_tier='review' (overall_confidence < 0.5).
        Results are paginated and ordered by created_at descending.

        Args:
            page: Page number (1-indexed)
            per_page: Items per page (max 100)
            auth_context: Authenticated user context

        Returns:
            Paginated list of review queue items with extracted values and preview URLs
        """
        ...

    async def approve(
        self,
        job_id: UUID,
        corrections: ReviewCorrections | None,
        reviewed_by: str,
        auth_context: AuthContext,
    ) -> ReviewResult:
        """Approve an extraction with optional corrections.

        Applies any provided corrections to the extraction result,
        updates job status to 'completed', records the review action,
        and triggers ChromaDB/Elasticsearch writes.

        Args:
            job_id: UUID of the job to approve
            corrections: Optional field corrections (drawing_number, title, etc.)
            reviewed_by: User ID of the reviewer
            auth_context: Authenticated user context

        Returns:
            ReviewResult with updated job status and corrected result

        Raises:
            NotFoundError: If job doesn't exist or isn't in review queue
            ValidationError: If corrections fail validation
        """
        ...

    async def reject(
        self,
        job_id: UUID,
        reason: str,
        reviewed_by: str,
        auth_context: AuthContext,
    ) -> ReviewResult:
        """Reject an extraction.

        Marks job status as 'rejected', records rejection reason,
        does NOT write to ChromaDB/Elasticsearch.

        Args:
            job_id: UUID of the job to reject
            reason: Human-readable rejection reason
            reviewed_by: User ID of the reviewer
            auth_context: Authenticated user context

        Returns:
            ReviewResult with rejected status

        Raises:
            NotFoundError: If job doesn't exist or isn't in review queue
        """
        ...


@runtime_checkable
class IOrchestrator(Protocol):
    """Orchestrates the three-stage extraction pipeline (§4.6)."""

    async def run_pipeline(
        self,
        job: Job,
        pdf_bytes: bytes,
    ) -> PipelineResult:
        """Execute the complete extraction pipeline.

        Pipeline stages:
        1. Extract: Drawing number, title, document type from vision+text+filename
        2. Companion: Multi-page visual description (if visual PDF)
        3. Sidecar: Merge results into Bedrock KB format

        Handles stage failures gracefully — partial results are preserved.
        Records OTEL spans for each stage.

        Args:
            job: Job entity with metadata
            pdf_bytes: Raw PDF file bytes

        Returns:
            PipelineResult with extraction_result, companion_text, sidecar,
            confidence_tier, and any error information

        Raises:
            ExtractionError: If Stage 1 fails completely (no partial results possible)
        """
        ...


@runtime_checkable
class ITaskQueue(Protocol):
    """Task queue interface for async job processing (§4.7)."""

    def enqueue_extraction(self, job_id: UUID) -> str:
        """Enqueue a job for async extraction processing.

        Creates a Celery task that will:
        1. Fetch job from repository
        2. Load PDF from file storage
        3. Run extraction pipeline
        4. Persist results and update job status
        5. Trigger callback if configured

        Args:
            job_id: UUID of the job to process

        Returns:
            Celery task ID for status tracking
        """
        ...

    def get_task_status(self, task_id: str) -> TaskStatus:
        """Check status of an enqueued task.

        Args:
            task_id: Celery task ID

        Returns:
            TaskStatus with state (PENDING, STARTED, SUCCESS, FAILURE),
            result if completed, and traceback if failed
        """
        ...


# ---------------------------------------------------------------------------
# Domain layer protocols (§4.8–§4.14)
# ---------------------------------------------------------------------------


@runtime_checkable
class IExtractor(Protocol):
    """Stage 1: Multi-source metadata extraction (§4.8)."""

    async def extract(self, context: PipelineContext) -> ExtractionResult:
        """Extract drawing number, title, and document type.

        Uses three extraction sources:
        1. Vision: qwen2.5vl:7b analyzes rendered page 1 image
        2. Text: llama3.1:8b analyzes extracted text content
        3. Filename: regex patterns match naming conventions

        Results are fused with weighted voting based on source confidence.

        Args:
            context: Pipeline context with PDF data, rendered images, extracted text

        Returns:
            ExtractionResult with extracted fields and per-field confidence scores

        Raises:
            ExtractionError: If all extraction sources fail
        """
        ...


@runtime_checkable
class ICompanionGenerator(Protocol):
    """Stage 2: Visual description generation (§4.9)."""

    async def generate(
        self,
        context: PipelineContext,
        extraction_result: ExtractionResult,
    ) -> CompanionResult:
        """Generate visual descriptions of document pages.

        Renders pages 0, 1, -2, -1 (first two and last two) and
        sends each to qwen2.5vl:7b for description. Cross-checks
        descriptions against Stage 1 extraction results for consistency.

        Args:
            context: Pipeline context with PDF data and rendered images
            extraction_result: Stage 1 extraction results for cross-validation

        Returns:
            CompanionResult with concatenated descriptions, page count,
            and validation status

        Note:
            Skipped for text-only PDFs (no visual content to describe).
            Returns empty CompanionResult with pages_described=0.
        """
        ...


@runtime_checkable
class ICompanionValidator(Protocol):
    """Validate companion descriptions against extraction results (§4.10)."""

    def validate(
        self,
        companion_text: str,
        extraction_result: ExtractionResult,
    ) -> ValidationResult:
        """Cross-check companion description with extracted metadata.

        Checks for consistency:
        - Drawing number mentioned in description matches extracted value
        - Document type implied by description matches classification
        - No contradictory information

        Args:
            companion_text: Generated visual description text
            extraction_result: Stage 1 extraction results

        Returns:
            ValidationResult with passed status, warnings list, and
            confidence_adjustment factor (negative if inconsistencies found)
        """
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
        """Build Bedrock KB sidecar document.

        Assembles all extraction and companion data into the standard
        sidecar format with metadataAttributes object. Validates against
        JSON Schema before returning.

        Args:
            extraction_result: Stage 1 extraction results
            companion_result: Stage 2 companion result (None if skipped)
            job: Job entity with filename, file_hash, etc.

        Returns:
            SidecarDocument validated against Bedrock KB schema

        Raises:
            ValidationError: If assembled sidecar fails schema validation
        """
        ...


@runtime_checkable
class IConfidenceCalculator(Protocol):
    """Calculate overall confidence and assign tier (§4.12)."""

    def calculate(
        self,
        extraction_result: ExtractionResult,
        validation_result: ValidationResult | None,
    ) -> ConfidenceAssessment:
        """Compute overall confidence score and determine tier.

        Algorithm:
        1. Weighted average of field confidences:
           - drawing_number_confidence: 40%
           - title_confidence: 30%
           - document_type_confidence: 30%
        2. Apply validation adjustment (if validation failed, reduce by 0.1)
        3. Clamp to [0.0, 1.0]
        4. Assign tier:
           - AUTO: overall >= 0.8 (proceed automatically)
           - SPOT: 0.5 <= overall < 0.8 (spot check recommended)
           - REVIEW: overall < 0.5 (human review required)

        Args:
            extraction_result: Extraction results with per-field confidences
            validation_result: Companion validation result (optional)

        Returns:
            ConfidenceAssessment with overall score, tier, and breakdown
        """
        ...


@runtime_checkable
class IFilenameParser(Protocol):
    """Extract metadata hints from filename patterns (§4.13)."""

    def parse(self, filename: str) -> FilenameHints:
        """Parse filename for drawing number and revision hints.

        Matches against known patterns:
        - KXC-B6-001-Y-27-1905-301.pdf → drawing_number=KXC-B6-001-Y-27-1905-301
        - Some_Drawing_Rev_P02.pdf → revision=P02
        - 12345_A1.pdf → drawing_number=12345, revision=A1

        Args:
            filename: Original filename (with or without extension)

        Returns:
            FilenameHints with extracted hints and confidence score
            (confidence based on pattern match strength)
        """
        ...


@runtime_checkable
class IResponseParser(Protocol):
    """Parse and repair Ollama JSON responses (§4.14)."""

    def parse(self, raw_response: str, expected_schema: type[T]) -> T:
        """Parse Ollama response with repair fallbacks.

        Repair strategies (in order):
        1. Direct JSON parse
        2. json-repair library for common malformations
        3. Regex extraction of key fields
        4. Partial result with nulls for missing fields

        Args:
            raw_response: Raw string response from Ollama
            expected_schema: Pydantic model or dataclass for validation

        Returns:
            Parsed and validated response object

        Raises:
            ParseError: If all repair strategies fail
        """
        ...


# ---------------------------------------------------------------------------
# Infrastructure layer protocols (§4.15–§4.20)
# ---------------------------------------------------------------------------


@runtime_checkable
class IPDFProcessor(Protocol):
    """PDF processing interface for text extraction and page rendering (§4.15)."""

    def load(self, pdf_bytes: bytes) -> PDFData:
        """Load PDF and extract basic information.

        Args:
            pdf_bytes: Raw PDF file bytes

        Returns:
            PDFData with page_count, file_hash, and metadata

        Raises:
            PDFLoadError: If PDF is corrupted or encrypted
        """
        ...

    def extract_text(self, pdf_bytes: bytes) -> str:
        """Extract all text content from PDF.

        Args:
            pdf_bytes: Raw PDF file bytes

        Returns:
            Concatenated text from all pages with page separators

        Raises:
            PDFLoadError: If PDF is corrupted
        """
        ...

    def render_page(
        self,
        pdf_bytes: bytes,
        page_number: int,
        dpi: int = 200,
        scale: float = 2.0,
    ) -> RenderedPage:
        """Render a single page to JPEG image.

        Args:
            pdf_bytes: Raw PDF file bytes
            page_number: Page index (0-based, negative for from-end)
            dpi: Resolution for rendering
            scale: Scale multiplier

        Returns:
            RenderedPage with jpeg_bytes, base64 encoding, and dimensions

        Raises:
            PDFLoadError: If PDF is corrupted
            PageNotFoundError: If page_number is out of range
        """
        ...

    def render_pages(
        self,
        pdf_bytes: bytes,
        page_numbers: list[int],
        dpi: int = 200,
        scale: float = 2.0,
    ) -> list[RenderedPage]:
        """Render multiple pages to JPEG images.

        Args:
            pdf_bytes: Raw PDF file bytes
            page_numbers: List of page indices (0-based, negative for from-end)
            dpi: Resolution for rendering
            scale: Scale multiplier

        Returns:
            List of RenderedPage objects
        """
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
        """Generate response from vision model with image input.

        Args:
            image_base64: Base64-encoded JPEG image
            prompt: Instruction prompt for the model
            model: Vision model name
            temperature: Sampling temperature (0 for deterministic)
            timeout_seconds: Request timeout

        Returns:
            OllamaResponse with response text and model metadata

        Raises:
            OllamaTimeoutError: If request exceeds timeout
            OllamaUnavailableError: If model is not loaded or server is down
        """
        ...

    async def generate_text(
        self,
        text: str,
        prompt: str,
        model: str = "llama3.1:8b",
        temperature: float = 0.0,
        timeout_seconds: float = 30.0,
    ) -> OllamaResponse:
        """Generate response from text model.

        Args:
            text: Input text content
            prompt: Instruction prompt for the model
            model: Text model name
            temperature: Sampling temperature
            timeout_seconds: Request timeout

        Returns:
            OllamaResponse with response text and model metadata

        Raises:
            OllamaTimeoutError: If request exceeds timeout
            OllamaUnavailableError: If model is not loaded
        """
        ...

    async def check_model_available(self, model: str) -> bool:
        """Check if a model is loaded and available.

        Args:
            model: Model name to check

        Returns:
            True if model is available, False otherwise
        """
        ...


@runtime_checkable
class IJobRepository(Protocol):
    """Repository interface for job and batch persistence (§4.17)."""

    async def create_batch(self, batch: Batch, jobs: list[Job]) -> Batch:
        """Create batch with jobs in a single transaction.

        Args:
            batch: Batch entity to create
            jobs: List of job entities to create

        Returns:
            Created batch with assigned IDs
        """
        ...

    async def get_batch(self, batch_id: UUID) -> Batch | None:
        """Retrieve batch by ID.

        Args:
            batch_id: UUID of the batch

        Returns:
            Batch if found, None otherwise
        """
        ...

    async def get_batch_with_jobs(
        self,
        batch_id: UUID,
    ) -> tuple[Batch, list[Job]] | None:
        """Retrieve batch with all associated jobs.

        Args:
            batch_id: UUID of the batch

        Returns:
            Tuple of (Batch, list[Job]) if found, None otherwise
        """
        ...

    async def get_job(self, job_id: UUID) -> Job | None:
        """Retrieve job by ID.

        Args:
            job_id: UUID of the job

        Returns:
            Job if found, None otherwise
        """
        ...

    async def get_job_by_file_hash(self, file_hash: str) -> Job | None:
        """Retrieve job by file hash (for deduplication).

        Args:
            file_hash: SHA-256 hash of the file

        Returns:
            Job if found, None otherwise
        """
        ...

    async def update_job_status(
        self,
        job_id: UUID,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update job status and optionally result/error.

        Args:
            job_id: UUID of the job
            status: New status
            result: Extraction result dict (optional)
            error_message: Error message if failed (optional)
        """
        ...

    async def get_pending_reviews(
        self,
        page: int,
        per_page: int,
    ) -> PaginatedResult[Job]:
        """Get jobs with confidence_tier='review' for human review.

        Args:
            page: Page number (1-indexed)
            per_page: Items per page

        Returns:
            Paginated list of jobs awaiting review
        """
        ...

    async def create_review_action(
        self,
        review_action: ReviewAction,
    ) -> ReviewAction:
        """Record a review action (approval or rejection).

        Args:
            review_action: Review action entity

        Returns:
            Created review action with ID
        """
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
        """Write enriched metadata to ChromaDB collection.

        Collection name format: zubot_metadata_{deployment_id}_{node_id}
        If deployment_id/node_id are None, uses 'default' collection.

        Args:
            document_id: Unique document identifier (file_hash)
            sidecar: Sidecar document with metadataAttributes
            deployment_id: Deployment ID for collection routing
            node_id: Node ID for collection routing

        Returns:
            True if write succeeded, False otherwise
        """
        ...

    async def check_connection(self) -> bool:
        """Check ChromaDB connection health.

        Returns:
            True if connected, False otherwise
        """
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
        """Index companion document in Elasticsearch.

        Index name format: zubot_companion_{deployment_id}
        If deployment_id is None, uses 'zubot_companion_default'.

        Args:
            document_id: Unique document identifier (file_hash)
            extraction_result: Extraction metadata for structured fields
            companion_result: Companion text for full-text search
            deployment_id: Deployment ID for index routing

        Returns:
            True if indexing succeeded, False otherwise
        """
        ...

    async def check_connection(self) -> bool:
        """Check Elasticsearch connection health.

        Returns:
            True if connected, False otherwise
        """
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
        """Send job completion notification to callback URL.

        Sends POST request with job result payload. Retries with
        exponential backoff on failure (max 3 attempts).

        Args:
            callback_url: URL to POST notification to
            job: Completed job with result
            api_key: API key for authentication header

        Returns:
            True if notification delivered, False if all retries failed
        """
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
