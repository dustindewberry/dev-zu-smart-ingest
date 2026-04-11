# Domain Protocols & External Contracts

This document is the single reference for every cross-layer interface
(protocol) and every HTTP contract exposed or consumed by the
`zubot_ingestion` service. It is aimed at developers who need to
implement a new adapter, extend the pipeline, or integrate with the
service from the outside.

All protocol definitions live in
`src/zubot_ingestion/domain/protocols.py`. Shared DTOs live in
`src/zubot_ingestion/shared/types.py`.

---

## Table of Contents

- [Domain Protocols](#domain-protocols)
  - [Presentation Layer Protocols](#presentation-layer-protocols)
  - [Application Layer Protocols](#application-layer-protocols)
  - [Domain Layer Protocols](#domain-layer-protocols)
  - [Infrastructure Layer Protocols](#infrastructure-layer-protocols)
- [How to Implement a Protocol](#how-to-implement-a-protocol)
- [External HTTP Contracts](#external-http-contracts)
  - [Request Schemas](#request-schemas)
  - [Response Schemas](#response-schemas)
  - [Webhook Callback Contract](#webhook-callback-contract)
  - [Error Response Format](#error-response-format)

---

## Domain Protocols

Every protocol is decorated with `@runtime_checkable` so `isinstance()`
checks work at runtime. **However**, `@runtime_checkable` only verifies
that method **names** exist on a class — it does **not** verify that
parameter names, types, or return types match. Always manually compare
your implementation's signatures against the protocol.

### Presentation Layer Protocols

#### `IAuthProvider` / `IAuthMiddleware` — Authentication Verification

Validates credentials from incoming HTTP request headers. Supports
API-key (`X-API-Key`) and JWT bearer-token authentication.

**Concrete implementation:** `AuthMiddleware` in
`api/middleware/auth.py`

```python
@runtime_checkable
class IAuthMiddleware(Protocol):
    """Middleware protocol for request authentication."""

    async def authenticate(self, request: Any) -> AuthContext:
        """Validate credentials from request headers.

        Checks for X-API-Key header (service-to-service) or
        Authorization: Bearer <jwt> (user session).

        Returns:
            AuthContext with user_id, auth_method, deployment_id, permissions

        Raises:
            AuthenticationError: If credentials are missing or invalid
        """
        ...
```

#### `IRateLimiter` — Request Throttling

Sliding-window rate limiter backed by Redis.

**Concrete implementation:** Rate limiting is handled via the `slowapi`
library integrated as FastAPI middleware (see `api/middleware/rate_limit.py`).
No standalone class implements this protocol directly.

```python
@runtime_checkable
class IRateLimiter(Protocol):
    """Rate limiting protocol for request throttling."""

    async def check_limit(
        self,
        key: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitResult:
        """Check if a request key is within its rate limit."""
        ...
```

---

### Application Layer Protocols

#### `IJobService` — Job Lifecycle Management

Central application service for batch submission, job retrieval, and
status tracking. The `POST /extract` route delegates to this service.

**Concrete implementation:** `JobService` in `services/job_service.py`

```python
@runtime_checkable
class IJobService(Protocol):
    """Application service for job and batch management."""

    async def submit_batch(
        self,
        files: list[UploadedFile],
        params: SubmissionParams,
        auth_context: AuthContext,
    ) -> BatchSubmissionResult:
        """Submit a batch of PDFs for extraction.

        Creates batch and job records, checks for duplicates via file_hash,
        saves files to temp storage, and enqueues Celery tasks for new jobs.
        """
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
```

#### `IOrchestrator` — Pipeline Orchestration

Orchestrates the three-stage extraction pipeline (Extract, Companion,
Sidecar) plus cross-cutting stages (metadata write, search indexing,
webhook callback).

**Concrete implementation:** `ExtractionOrchestrator` in
`services/orchestrator.py`

> **Note:** `deployment_id`, `node_id`, `callback_url`, and `api_key`
> are **keyword-only** parameters. Any implementation must match this
> calling convention exactly.

```python
@runtime_checkable
class IOrchestrator(Protocol):
    """Orchestrates the three-stage extraction pipeline."""

    async def run_pipeline(
        self,
        job: Job,
        pdf_bytes: bytes,
        *,
        deployment_id: int | None = None,
        node_id: int | None = None,
        callback_url: str | None = None,
        api_key: str = "",
    ) -> PipelineResult:
        """Execute the complete extraction pipeline.

        Pipeline stages:
        1. Extract: Drawing number, title, document type from vision+text+filename
        2. Companion: Multi-page visual description (if visual PDF)
        3. Sidecar: Merge results into Bedrock KB format

        Handles stage failures gracefully -- partial results are preserved.
        Records OTEL spans for each stage.
        """
        ...
```

#### `IReviewService` — Human Review Workflow

Application service for listing, approving, and rejecting review-tier
jobs (jobs with `overall_confidence < 0.5`).

**Concrete implementation:** No dedicated service class exists. The
review workflow is implemented directly in the `api/routes/review.py`
route handlers, which call `IJobRepository` methods directly.

```python
@runtime_checkable
class IReviewService(Protocol):
    """Application service for human review workflow."""

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
```

---

### Domain Layer Protocols

#### `IExtractor` — Metadata Extraction from PDF

Stage 1 of the pipeline. Each extractor targets a single metadata
field using three sources: vision model, text model, and filename
regex patterns. Results are fused with weighted voting.

**Concrete implementations:**

| Class | File | Target Field |
|---|---|---|
| `DrawingNumberExtractor` | `domain/pipeline/extractors/drawing_number.py` | Drawing number |
| `TitleExtractor` | `domain/pipeline/extractors/title.py` | Title |
| `DocumentTypeExtractor` | `domain/pipeline/extractors/document_type.py` | Document type |

```python
@runtime_checkable
class IExtractor(Protocol):
    """Stage 1: Multi-source metadata extraction."""

    async def extract(self, context: PipelineContext) -> ExtractionResult:
        """Extract drawing number, title, and document type.

        Uses three extraction sources:
        1. Vision: qwen2.5vl:7b analyzes rendered page 1 image
        2. Text: llama3.1:8b analyzes extracted text content
        3. Filename: regex patterns match naming conventions
        """
        ...
```

#### `ICompanionGenerator` — Companion Document Generation

Stage 2 of the pipeline. Generates visual descriptions of document
pages by rendering selected pages (first two, last two) and sending
them to the vision model.

**Concrete implementation:** `CompanionGenerator` in
`domain/pipeline/companion.py`

```python
@runtime_checkable
class ICompanionGenerator(Protocol):
    """Stage 2: Visual description generation."""

    async def generate(
        self,
        context: PipelineContext,
        extraction_result: ExtractionResult,
    ) -> CompanionResult:
        """Generate visual descriptions of document pages.

        Renders pages 0, 1, -2, -1 (first two and last two) and
        sends each to qwen2.5vl:7b for description.
        """
        ...
```

#### `ISidecarBuilder` — Sidecar Document Assembly

Stage 3 of the pipeline. Assembles extraction and companion data into
the Bedrock Knowledge Base sidecar format, validated against JSON
Schema.

**Concrete implementation:** `SidecarBuilder` in
`domain/pipeline/sidecar.py`

```python
@runtime_checkable
class ISidecarBuilder(Protocol):
    """Stage 3: Sidecar document assembly."""

    def build(
        self,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult | None,
        job: Job,
    ) -> SidecarDocument:
        """Build Bedrock KB sidecar document.

        Assembles all extraction and companion data into the standard
        sidecar format with metadataAttributes object.
        """
        ...
```

#### `IConfidenceCalculator` — Confidence Scoring

Computes an overall confidence score from per-field confidences and
assigns a tier that determines downstream routing.

**Concrete implementation:** `ConfidenceCalculator` in
`domain/pipeline/confidence.py`

```python
@runtime_checkable
class IConfidenceCalculator(Protocol):
    """Calculate overall confidence and assign tier."""

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
           - AUTO:   overall >= 0.8 (proceed automatically)
           - SPOT:   0.5 <= overall < 0.8 (spot check recommended)
           - REVIEW: overall < 0.5 (human review required)
        """
        ...
```

#### `ICompanionValidator` — Companion Text Validation

Cross-checks companion descriptions against Stage 1 extraction results
to detect inconsistencies.

**Concrete implementation:** `CompanionValidator` in
`domain/pipeline/validation.py`

```python
@runtime_checkable
class ICompanionValidator(Protocol):
    """Validate companion descriptions against extraction results."""

    def validate(
        self,
        companion_text: str,
        extraction_result: ExtractionResult,
    ) -> ValidationResult:
        """Cross-check companion description with extracted metadata.

        Returns:
            ValidationResult with passed status, warnings list, and
            confidence_adjustment factor (negative if inconsistencies found)
        """
        ...
```

#### `IResponseParser` — LLM Response Parsing

Parses raw Ollama JSON responses with multi-strategy repair fallbacks.

**Concrete implementation:** `JsonResponseParser` in
`domain/pipeline/json_parser.py`

```python
@runtime_checkable
class IResponseParser(Protocol):
    """Parse and repair Ollama JSON responses."""

    def parse(self, raw_response: str, expected_schema: type[T]) -> T:
        """Parse Ollama response with repair fallbacks.

        Repair strategies (in order):
        1. Direct JSON parse
        2. json-repair library for common malformations
        3. Regex extraction of key fields
        4. Partial result with nulls for missing fields
        """
        ...
```

#### `IFilenameParser` — Filename-Based Hint Extraction

Extracts drawing number and revision hints from filename patterns
using regex matching.

**Concrete implementation:** `FilenameParser` in
`domain/pipeline/extractors/filename_parser.py`

```python
@runtime_checkable
class IFilenameParser(Protocol):
    """Extract metadata hints from filename patterns."""

    def parse(self, filename: str) -> FilenameHints:
        """Parse filename for drawing number and revision hints.

        Matches against known patterns:
        - KXC-B6-001-Y-27-1905-301.pdf -> drawing_number=KXC-B6-001-Y-27-1905-301
        - Some_Drawing_Rev_P02.pdf -> revision=P02
        - 12345_A1.pdf -> drawing_number=12345, revision=A1
        """
        ...
```

---

### Infrastructure Layer Protocols

#### `IOllamaClient` — Ollama LLM HTTP Client

HTTP client for the Ollama inference server. Supports vision (image +
prompt) and text (text + prompt) generation endpoints.

**Concrete implementation:** `OllamaClient` in
`infrastructure/ollama/client.py`

```python
@runtime_checkable
class IOllamaClient(Protocol):
    """Ollama API client for vision and text inference."""

    async def generate_vision(
        self,
        image_base64: str,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
    ) -> OllamaResponse:
        """Generate response from vision model with image input.

        model=None means the implementation reads the model name from
        Settings (OLLAMA_VISION_MODEL) so operators can swap via env vars.
        """
        ...

    async def generate_text(
        self,
        text: str,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.0,
        timeout_seconds: float = 30.0,
    ) -> OllamaResponse:
        """Generate response from text model.

        model=None means the implementation reads the model name from
        Settings (OLLAMA_TEXT_MODEL) so operators can swap via env vars.
        """
        ...

    async def check_model_available(self, model: str) -> bool:
        """Check if a model is loaded and available."""
        ...
```

#### `IPDFProcessor` — PDF Loading, Text Extraction, Page Rendering

Loads PDF files, extracts text content, and renders pages to JPEG
images for the vision model.

**Concrete implementation:** `PyMuPDFProcessor` in
`infrastructure/pdf/processor.py`

```python
@runtime_checkable
class IPDFProcessor(Protocol):
    """PDF processing interface for text extraction and page rendering."""

    def load(self, pdf_bytes: bytes) -> PDFData:
        """Load PDF and extract basic information (page_count, file_hash, metadata)."""
        ...

    def extract_text(self, pdf_bytes: bytes) -> str:
        """Extract all text content from PDF (all pages concatenated)."""
        ...

    def extract_page_text(self, pdf_bytes: bytes, page_number: int) -> str:
        """Extract the text layer for a single page (zero-based index)."""
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
```

#### `IJobRepository` — Database Persistence

Repository interface for CRUD operations on batches, jobs, and review
actions. Backed by PostgreSQL via SQLAlchemy async.

**Concrete implementation:** `JobRepository` in
`infrastructure/database/repository.py`

```python
@runtime_checkable
class IJobRepository(Protocol):
    """Repository interface for job and batch persistence."""

    async def create_batch(self, batch: Batch, jobs: list[Job]) -> Batch:
        """Create batch with jobs in a single transaction."""
        ...

    async def get_batch(self, batch_id: UUID) -> Batch | None:
        """Retrieve batch by ID."""
        ...

    async def get_batch_with_jobs(self, batch_id: UUID) -> BatchWithJobs | None:
        """Retrieve batch with all associated jobs."""
        ...

    async def get_job(self, job_id: UUID) -> Job | None:
        """Retrieve job by ID."""
        ...

    async def get_job_by_file_hash(self, file_hash: FileHash) -> Job | None:
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
    ) -> None:
        """Persist a completed extraction result with indexed columns.

        Writes confidence_tier, confidence_score, processing_time_ms,
        otel_trace_id, and pipeline_trace as dedicated indexed columns
        (not just the JSONB blob) so downstream queries can filter on them.
        """
        ...

    async def get_pending_reviews(self, page: int, per_page: int) -> PaginatedResult[Job]:
        """Get jobs with confidence_tier='review' for human review."""
        ...

    async def create_review_action(self, review_action: ReviewAction) -> ReviewAction:
        """Record a review action (approval or rejection)."""
        ...
```

#### `IMetadataWriter` — ChromaDB Metadata Persistence

Writes enriched metadata to ChromaDB vector store collections routed
by deployment/node IDs.

**Concrete implementation:** `ChromaDBMetadataWriter` in
`infrastructure/chromadb/writer.py`

```python
@runtime_checkable
class IMetadataWriter(Protocol):
    """Metadata writer interface for vector store enrichment."""

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
        """
        ...

    async def check_connection(self) -> bool:
        """Check ChromaDB connection health."""
        ...
```

#### `ISearchIndexer` — Elasticsearch Document Indexing

Indexes companion documents in Elasticsearch for full-text search,
routed by deployment ID.

**Concrete implementations:**

| Class | File | Description |
|---|---|---|
| `ElasticsearchSearchIndexer` | `infrastructure/elasticsearch/indexer.py` | Production HTTP adapter |
| `NoOpSearchIndexer` | `infrastructure/elasticsearch/indexer.py` | No-op for dev/test |

```python
@runtime_checkable
class ISearchIndexer(Protocol):
    """Search indexer interface for companion document indexing."""

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
        """
        ...

    async def check_connection(self) -> bool:
        """Check Elasticsearch connection health."""
        ...
```

#### `ICallbackClient` — Webhook Delivery

Delivers job-completion webhooks to external callback URLs with
HMAC-SHA256 signing and exponential-backoff retry.

**Concrete implementations:**

| Class | File | Description |
|---|---|---|
| `CallbackHttpClient` | `infrastructure/callback/client.py` | Production HTTP webhook client |
| `NoOpCallbackClient` | `infrastructure/callback/client.py` | No-op (returns `True`) for dev/test |

```python
@runtime_checkable
class ICallbackClient(Protocol):
    """Callback client interface for webhook notifications."""

    async def notify_completion(
        self,
        callback_url: str,
        job: Job,
        api_key: str,
    ) -> bool:
        """Send job completion notification to callback URL.

        Retries with exponential backoff on failure (max 3 attempts).
        Returns True if delivered, False if all retries failed.
        """
        ...
```

---

## How to Implement a Protocol

This section walks through adding a new concrete implementation of a
domain protocol.

### Step-by-Step

1. **Find the protocol** in `src/zubot_ingestion/domain/protocols.py`.
   Note every method name, parameter name, parameter kind (positional
   vs keyword-only), type annotation, and return type.

2. **Create a concrete class** in the appropriate `infrastructure/`
   subdirectory. For example, if you are implementing
   `ISearchIndexer`, create a file under
   `infrastructure/elasticsearch/`.

3. **Implement ALL methods** with signatures that exactly match the
   protocol. Pay close attention to:
   - Parameter names (must match exactly)
   - Parameter kinds (`*` separator makes everything after it keyword-only)
   - Default values
   - Return types

4. **Add a factory function** in `services/__init__.py` (the
   composition root). The factory should accept a `settings` object
   and return the concrete instance.

5. **Wire into the orchestrator or relevant service.** For pipeline
   adapters, add a constructor parameter to `ExtractionOrchestrator`
   and pass the instance from `build_orchestrator()` in
   `services/__init__.py`.

6. **Test** against the protocol with `isinstance()` checks and full
   integration tests.

### Example: Minimal `ISearchIndexer` Implementation

```python
# infrastructure/elasticsearch/my_indexer.py

from zubot_ingestion.domain.entities import CompanionResult, ExtractionResult
from zubot_ingestion.domain.protocols import ISearchIndexer


class MySearchIndexer:
    """Custom search indexer — implements ISearchIndexer."""

    def __init__(self, *, base_url: str) -> None:
        self._base_url = base_url

    async def index_companion(
        self,
        document_id: str,
        extraction_result: ExtractionResult,
        companion_result: CompanionResult,
        deployment_id: int | None,
    ) -> bool:
        # Your indexing logic here
        return True

    async def check_connection(self) -> bool:
        # Your health check logic here
        return True


# Verify at import time (optional but recommended):
assert isinstance(MySearchIndexer(base_url="http://localhost:9200"), ISearchIndexer)
```

Then add a factory in `services/__init__.py`:

```python
def build_my_search_indexer(settings):
    from infrastructure.elasticsearch.my_indexer import MySearchIndexer
    return MySearchIndexer(base_url=settings.ELASTICSEARCH_URL)
```

And wire it into `build_orchestrator()`:

```python
search_indexer = build_my_search_indexer(settings)
# ... pass to ExtractionOrchestrator(search_indexer=search_indexer, ...)
```

### `@runtime_checkable` Gotcha

`@runtime_checkable` `isinstance()` checks only verify that method
**names** exist on the class. They do **not** verify:

- Parameter names
- Parameter kinds (positional vs keyword-only)
- Parameter types
- Return types

This means `isinstance(my_obj, IOrchestrator)` can return `True` even
if your `run_pipeline` method has completely different parameter names.
The mismatch will only surface as a `TypeError` at call time.

**Always manually compare** every method's full signature between the
protocol definition and your implementation. Use `mypy --strict` to
catch drift statically.

---

## External HTTP Contracts

All endpoints are served by FastAPI and documented via the auto-generated
OpenAPI spec at `/docs` (Swagger UI) and `/redoc` (ReDoc).

### Authentication

Most endpoints require authentication via one of:

- **API key:** `X-API-Key: <key>` header (service-to-service)
- **JWT:** `Authorization: Bearer <token>` header (user session)

Exempt paths (no auth required): `/health`, `/metrics`

### Rate Limits

| Endpoint | Limit |
|---|---|
| `POST /extract` | 20/minute |
| `GET /batches/{batch_id}` | 100/minute |
| `GET /jobs/{job_id}` | 100/minute |
| `GET /review/pending` | 100/minute |
| `POST /review/{job_id}/approve` | 50/minute |
| `POST /review/{job_id}/reject` | 50/minute |
| `GET /health` | Exempt |
| `GET /metrics` | Exempt |

---

### Request Schemas

#### `POST /extract` — Submit PDFs for Extraction

**Content-Type:** `multipart/form-data`

| Field | Type | Required | Description |
|---|---|---|---|
| `files` | File[] | Yes | One or more PDF files |
| `mode` | string | No | Extraction mode: `auto` (default), `drawing`, `title` |
| `callback_url` | string | No | Webhook URL for completion notification |
| `deployment_id` | string | No | Zutec deployment ID (coerced to int) |
| `node_id` | string | No | Zutec node ID (coerced to int) |

**Example (curl):**

```bash
curl -X POST http://localhost:8000/extract \
  -H "X-API-Key: your-api-key" \
  -F "files=@drawing_001.pdf" \
  -F "files=@drawing_002.pdf" \
  -F "mode=auto" \
  -F "callback_url=https://example.com/webhook" \
  -F "deployment_id=42" \
  -F "node_id=7"
```

#### `GET /batches/{batch_id}` — Get Batch Status

**Path parameter:** `batch_id` (UUID string)

```bash
curl http://localhost:8000/batches/550e8400-e29b-41d4-a716-446655440000 \
  -H "X-API-Key: your-api-key"
```

#### `GET /jobs/{job_id}` — Get Job Detail

**Path parameter:** `job_id` (UUID string)

```bash
curl http://localhost:8000/jobs/6ba7b810-9dad-11d1-80b4-00c04fd430c8 \
  -H "X-API-Key: your-api-key"
```

#### `GET /review/pending` — List Pending Reviews

**Query parameters:**

| Param | Type | Default | Description |
|---|---|---|---|
| `limit` | int | 50 | Max items to return (1-500) |
| `offset` | int | 0 | Items to skip |

```bash
curl "http://localhost:8000/review/pending?limit=20&offset=0" \
  -H "X-API-Key: your-api-key"
```

#### `POST /review/{job_id}/approve` — Approve a Review Job

**Path parameter:** `job_id` (UUID string)
**Content-Type:** `application/json`

```json
{
  "reviewer_id": "jsmith",
  "notes": "Drawing number confirmed against physical copy"
}
```

```bash
curl -X POST http://localhost:8000/review/6ba7b810-9dad-11d1-80b4-00c04fd430c8/approve \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"reviewer_id": "jsmith", "notes": "Looks correct"}'
```

#### `POST /review/{job_id}/reject` — Reject a Review Job

**Path parameter:** `job_id` (UUID string)
**Content-Type:** `application/json`

```json
{
  "reviewer_id": "jsmith",
  "reason": "Drawing number does not match the title block"
}
```

```bash
curl -X POST http://localhost:8000/review/6ba7b810-9dad-11d1-80b4-00c04fd430c8/reject \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"reviewer_id": "jsmith", "reason": "Wrong drawing number"}'
```

#### `GET /health` — Health Check

No parameters. No authentication required.

```bash
curl http://localhost:8000/health
```

#### `GET /metrics` — Prometheus Metrics

No parameters. No authentication required. Returns Prometheus text
exposition format.

```bash
curl http://localhost:8000/metrics
```

---

### Response Schemas

#### `POST /extract` — `202 Accepted`

```json
{
  "batch_id": "550e8400-e29b-41d4-a716-446655440000",
  "total": 2,
  "poll_url": "/batches/550e8400-e29b-41d4-a716-446655440000",
  "jobs": [
    {
      "job_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
      "filename": "drawing_001.pdf",
      "status": "queued",
      "file_hash": "a1b2c3d4e5f6...",
      "result": null
    },
    {
      "job_id": "6ba7b811-9dad-11d1-80b4-00c04fd430c8",
      "filename": "drawing_002.pdf",
      "status": "queued",
      "file_hash": "f6e5d4c3b2a1...",
      "result": null
    }
  ]
}
```

DTO: `BatchSubmissionResult` (`shared/types.py`)

#### `GET /batches/{batch_id}` — `200 OK`

```json
{
  "batch_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "processing",
  "progress": {
    "completed": 1,
    "queued": 0,
    "failed": 0,
    "in_progress": 1,
    "total": 2
  },
  "callback_url": "https://example.com/webhook",
  "deployment_id": 42,
  "node_id": 7,
  "created_at": "2025-01-15T10:30:00+00:00",
  "updated_at": "2025-01-15T10:31:45+00:00",
  "jobs": [
    {
      "job_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
      "filename": "drawing_001.pdf",
      "status": "completed",
      "file_hash": "a1b2c3d4e5f6...",
      "result": {
        "drawing_number": "KXC-B6-001-Y-27-1905-301",
        "title": "Ground Floor Plan",
        "document_type": "drawing"
      }
    },
    {
      "job_id": "6ba7b811-9dad-11d1-80b4-00c04fd430c8",
      "filename": "drawing_002.pdf",
      "status": "processing",
      "file_hash": "f6e5d4c3b2a1...",
      "result": null
    }
  ]
}
```

DTO: `BatchWithJobs` (`shared/types.py`)

#### `GET /jobs/{job_id}` — `200 OK`

```json
{
  "job_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "batch_id": "550e8400-e29b-41d4-a716-446655440000",
  "filename": "drawing_001.pdf",
  "file_hash": "a1b2c3d4e5f6...",
  "status": "completed",
  "result": {
    "drawing_number": "KXC-B6-001-Y-27-1905-301",
    "title": "Ground Floor Plan",
    "document_type": "drawing"
  },
  "error_message": null,
  "pipeline_trace": {
    "stages": {
      "pdf_load": {"duration_ms": 120},
      "stage1": {"duration_ms": 3200},
      "stage2": {"duration_ms": 5100},
      "stage3": {"duration_ms": 45}
    }
  },
  "otel_trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "processing_time_ms": 8465,
  "created_at": "2025-01-15T10:30:00+00:00",
  "updated_at": "2025-01-15T10:31:25+00:00"
}
```

DTO: `JobDetail` (`shared/types.py`)

#### `GET /review/pending` — `200 OK`

```json
{
  "items": [
    {
      "job_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
      "drawing_number": "KXC-B6-001",
      "title": null,
      "confidence_tier": "review",
      "confidence_score": 0.35,
      "created_at": "2025-01-15T10:30:00+00:00"
    }
  ],
  "limit": 50,
  "offset": 0,
  "total": 1
}
```

#### `POST /review/{job_id}/approve` — `200 OK`

Returns the full updated job record (same shape as `GET /jobs/{job_id}`
response, plus `confidence_tier` and `confidence_score` fields).

```json
{
  "job_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "batch_id": "550e8400-e29b-41d4-a716-446655440000",
  "filename": "drawing_001.pdf",
  "file_hash": "a1b2c3d4e5f6...",
  "status": "completed",
  "result": {
    "drawing_number": "KXC-B6-001",
    "title": "Ground Floor Plan",
    "document_type": "drawing",
    "review": {
      "action": "approved",
      "reviewer_id": "jsmith",
      "notes": "Looks correct",
      "reviewed_at": "2025-01-15T14:22:00+00:00"
    }
  },
  "error_message": null,
  "pipeline_trace": null,
  "otel_trace_id": null,
  "processing_time_ms": 8465,
  "confidence_tier": "review",
  "confidence_score": 0.35,
  "created_at": "2025-01-15T10:30:00+00:00",
  "updated_at": "2025-01-15T14:22:00+00:00"
}
```

#### `POST /review/{job_id}/reject` — `200 OK`

Returns the full updated job record with `status: "failed"` and the
rejection reason in `error_message`.

```json
{
  "job_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "batch_id": "550e8400-e29b-41d4-a716-446655440000",
  "filename": "drawing_001.pdf",
  "file_hash": "a1b2c3d4e5f6...",
  "status": "failed",
  "result": {
    "drawing_number": "KXC-B6-001",
    "title": null,
    "document_type": "drawing"
  },
  "error_message": "Rejected by jsmith: Wrong drawing number",
  "pipeline_trace": null,
  "otel_trace_id": null,
  "processing_time_ms": 8465,
  "confidence_tier": "review",
  "confidence_score": 0.35,
  "created_at": "2025-01-15T10:30:00+00:00",
  "updated_at": "2025-01-15T14:25:00+00:00"
}
```

#### `GET /health` — `200 OK` / `503 Service Unavailable`

HTTP 200 for `healthy` or `degraded`, HTTP 503 for `unhealthy`.

```json
{
  "status": "healthy",
  "service": "zubot-ingestion",
  "version": "1.0.0",
  "dependencies": {
    "postgres": {
      "status": "healthy",
      "latency_ms": 12,
      "error": null
    },
    "redis": {
      "status": "healthy",
      "latency_ms": 3,
      "error": null
    },
    "ollama": {
      "status": "healthy",
      "latency_ms": 45,
      "error": null
    },
    "chromadb": {
      "status": "healthy",
      "latency_ms": 28,
      "error": null
    },
    "elasticsearch": {
      "status": "healthy",
      "latency_ms": 15,
      "error": null
    }
  },
  "workers": {
    "active": 2,
    "queue_depth": 5
  }
}
```

Status aggregation rules:

- **healthy** — all dependencies (critical + optional) are healthy
- **degraded** — all critical deps (postgres, redis) healthy, but at
  least one optional dep (ollama, chromadb, elasticsearch) is unhealthy
- **unhealthy** — at least one critical dep is unhealthy

DTO: `HealthStatus` (`shared/types.py`)

#### `GET /metrics` — `200 OK`

Returns Prometheus text exposition format. Content-Type:
`text/plain; version=0.0.4; charset=utf-8`

```
# HELP zubot_extraction_duration_seconds Time to run extraction pipeline
# TYPE zubot_extraction_duration_seconds histogram
zubot_extraction_duration_seconds_bucket{le="1.0"} 0
zubot_extraction_duration_seconds_bucket{le="5.0"} 12
...
```

---

### Webhook Callback Contract

When a `callback_url` is provided during batch submission, the service
POSTs a completion notification to that URL after each job finishes.

#### Transport

- **Method:** `POST`
- **URL:** The `callback_url` submitted with the batch
- **Content-Type:** `application/json`

#### Headers

| Header | Value | Description |
|---|---|---|
| `Content-Type` | `application/json` | Always present |
| `User-Agent` | `zubot-ingestion/1.0` | Identifies the sender |
| `X-API-Key` | `<service-api-key>` | Present when the api key is non-empty. Uses the service-wide `ZUBOT_INGESTION_API_KEY`, not a per-submission key. |
| `X-Zubot-Signature` | `<hmac-sha256-hex>` | Present when `CALLBACK_SIGNING_SECRET` is configured. Lowercase hex HMAC-SHA256 of the raw request body. |

#### Signature Verification

When the `X-Zubot-Signature` header is present, receivers should verify
it as follows:

1. Read the raw request body bytes (do not parse JSON first).
2. Compute `HMAC-SHA256(signing_secret, raw_body)` using the shared
   signing secret.
3. Compare the hex digest to the `X-Zubot-Signature` header value
   using a constant-time comparison function.

The body is serialised with `json.dumps(sort_keys=True, separators=(',', ':'))`
so the byte representation is deterministic across invocations.

#### Retry Policy

| Setting | Value |
|---|---|
| Max retries | 3 |
| Backoff base | 0.5 seconds |
| Backoff factor | 2x |
| Backoff schedule | 0.5s, 1.0s, 2.0s |
| 5xx responses | Retried |
| 4xx responses | Not retried (treated as permanent failure) |
| Network errors | Retried |

The adapter never raises. A final failure logs a warning and returns
`False` to the caller.

#### Sample Payload

```json
{
  "job_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "batch_id": "550e8400-e29b-41d4-a716-446655440000",
  "filename": "drawing_001.pdf",
  "file_hash": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
  "status": "completed",
  "confidence_tier": "auto",
  "overall_confidence": 0.92,
  "result": {
    "drawing_number": "KXC-B6-001-Y-27-1905-301",
    "title": "Ground Floor Plan",
    "document_type": "drawing"
  },
  "error_message": null,
  "processing_time_ms": 8465,
  "otel_trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "created_at": "2025-01-15T10:30:00+00:00",
  "updated_at": "2025-01-15T10:31:25+00:00"
}
```

#### Disabling Callbacks

Set `CALLBACK_ENABLED=False` in your environment. The composition root
will inject a `NoOpCallbackClient` that returns `True` without making
any HTTP requests.

---

### Error Response Format

All API errors use the standard `ErrorResponse` shape defined in
`shared/types.py`:

```python
@dataclass(frozen=True)
class ErrorResponse:
    code: str
    message: str
    details: dict[str, Any] | None = None
    trace_id: str | None = None
```

**Sample error responses:**

```json
{
  "code": "404",
  "message": "Job not found",
  "details": null,
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"
}
```

```json
{
  "code": "400",
  "message": "Invalid mode 'fast'. Must be one of: auto, drawing, title",
  "details": null,
  "trace_id": null
}
```

```json
{
  "code": "413",
  "message": "File drawing_001.pdf exceeds maximum size of 100MB",
  "details": null,
  "trace_id": null
}
```

```json
{
  "code": "409",
  "message": "Job 6ba7b810-... is not in the review tier (confidence_tier=auto); only review-tier jobs may be approved or rejected",
  "details": null,
  "trace_id": null
}
```

**HTTP status codes used:**

| Code | When |
|---|---|
| `200` | Successful GET / review action |
| `202` | Batch submitted and queued |
| `400` | Invalid request (bad UUID, bad mode, non-PDF file) |
| `401` | Missing or invalid authentication |
| `404` | Batch or job not found |
| `409` | Review action on non-review-tier job |
| `413` | File exceeds size limit |
| `429` | Rate limit exceeded |
| `503` | Service unhealthy (health endpoint only) |
