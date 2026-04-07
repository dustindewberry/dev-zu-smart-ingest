"""Single source of truth for string constants, prefixes, queue names, and
collection name patterns used across the zubot-ingestion service.

All downstream modules MUST import these constants rather than hardcoding
literal values, to prevent drift between layers (API, services, domain,
infrastructure) and across worker tasks that cannot see each other's code.

Conventions:
    - All constants are module-level UPPER_CASE.
    - Helper functions are lower_snake_case and pure (no I/O, no globals
      besides the constants defined in this module).
    - This module has ZERO runtime dependencies on other zubot_ingestion
      modules so it can be safely imported anywhere in the dependency graph.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Service identifiers
# ---------------------------------------------------------------------------

SERVICE_NAME: str = "zubot-ingestion"
SERVICE_VERSION: str = "0.1.0"


# ---------------------------------------------------------------------------
# Database connection pool defaults (CAP-004)
# ---------------------------------------------------------------------------

DB_POOL_SIZE: int = 10
DB_MAX_OVERFLOW: int = 20
DB_POOL_TIMEOUT: int = 30
DB_POOL_RECYCLE: int = 3600


# ---------------------------------------------------------------------------
# Celery / Redis
# ---------------------------------------------------------------------------

CELERY_BROKER_DB: int = 2
CELERY_RESULT_DB: int = 3
RATE_LIMIT_REDIS_DB: int = 4

CELERY_TASK_NAME_EXTRACTION: str = "zubot_ingestion.tasks.extract_document"
CELERY_QUEUE_DEFAULT: str = "zubot_ingestion"


# ---------------------------------------------------------------------------
# ChromaDB
# ---------------------------------------------------------------------------

CHROMA_COLLECTION_PREFIX: str = "zubot_metadata_"
CHROMA_COLLECTION_DEFAULT_DEPLOYMENT: str = "default"
CHROMA_COLLECTION_DEFAULT_NODE: str = "default"


def chroma_collection_name(
    deployment_id: str | None,
    node_id: str | None,
) -> str:
    """Build the ChromaDB collection name for a deployment + node pair.

    Both ``deployment_id`` and ``node_id`` are optional; ``None`` values are
    substituted with the literal string ``'default'`` so the function always
    returns a deterministic, valid collection identifier.

    Examples:
        >>> chroma_collection_name(None, None)
        'zubot_metadata_default_default'
        >>> chroma_collection_name('acme', 'node-1')
        'zubot_metadata_acme_node-1'
    """
    deployment = deployment_id or CHROMA_COLLECTION_DEFAULT_DEPLOYMENT
    node = node_id or CHROMA_COLLECTION_DEFAULT_NODE
    return f"{CHROMA_COLLECTION_PREFIX}{deployment}_{node}"


# ---------------------------------------------------------------------------
# Elasticsearch
# ---------------------------------------------------------------------------

ES_INDEX_PREFIX: str = "zubot_companion_"
ES_DOCUMENT_ID_PREFIX: str = "companion_"


def es_index_name(deployment_id: str | None) -> str:
    """Build the Elasticsearch index name for a given deployment.

    A ``None`` deployment_id is substituted with the literal ``'default'``.

    Examples:
        >>> es_index_name(None)
        'zubot_companion_default'
        >>> es_index_name('acme')
        'zubot_companion_acme'
    """
    deployment = deployment_id or CHROMA_COLLECTION_DEFAULT_DEPLOYMENT
    return f"{ES_INDEX_PREFIX}{deployment}"


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

PROVENANCE_INGESTION_SERVICE: str = "zubot-ingestion"


# ---------------------------------------------------------------------------
# Confidence thresholds and weights
# ---------------------------------------------------------------------------

CONFIDENCE_TIER_AUTO_MIN: float = 0.8
CONFIDENCE_TIER_SPOT_MIN: float = 0.5

# String-valued tier constants (used by repository queries against the DB)
CONFIDENCE_TIER_AUTO: str = "auto"
CONFIDENCE_TIER_SPOT: str = "spot"
CONFIDENCE_TIER_REVIEW: str = "review"

CONFIDENCE_WEIGHT_DRAWING_NUMBER: float = 0.40
CONFIDENCE_WEIGHT_TITLE: float = 0.30
CONFIDENCE_WEIGHT_DOCUMENT_TYPE: float = 0.30

CONFIDENCE_VALIDATION_PENALTY: float = -0.10


# ---------------------------------------------------------------------------
# Pipeline limits
# ---------------------------------------------------------------------------

MAX_COMPANION_PAGES: int = 4
MAX_SIDECAR_METADATA_KEYS: int = 10  # AWS Bedrock KB limit

# Text-only PDF detection thresholds (used by Stage 2 companion generator).
# A PDF whose extracted text exceeds TEXT_ONLY_THRESHOLD_CHARS characters AND
# whose page count exceeds TEXT_ONLY_THRESHOLD_PAGES is treated as a text-only
# document and the visual companion stage is skipped (no rendered descriptions).
TEXT_ONLY_THRESHOLD_CHARS: int = 5000
TEXT_ONLY_THRESHOLD_PAGES: int = 10

# Versioned prompt used by Stage 2 (CAP-017) companion description generation.
# The vision model is instructed to return JSON with two top-level keys so the
# response parser can deterministically separate the visual description from
# the technical details when assembling the final markdown companion document.
COMPANION_DESCRIPTION_PROMPT_V1: str = (
    "You are inspecting a single page from a construction document. "
    "Describe the page in two parts and respond with a JSON object containing "
    "exactly two keys: 'visual_description' and 'technical_details'. "
    "The 'visual_description' field must be 2-4 sentences describing what is "
    "visually present on the page (drawings, title blocks, tables, photos, "
    "schedules, plan views, sections, elevations, legends). "
    "The 'technical_details' field must be 2-4 sentences listing any "
    "technical specifics you can read directly from the page (drawing number, "
    "title, scale, revision, discipline, project name, dimensions, callouts, "
    "annotations, schedules). "
    "Respond ONLY with the JSON object, no prose, no markdown fences."
)


# ---------------------------------------------------------------------------
# OpenTelemetry span names
# ---------------------------------------------------------------------------

OTEL_SPAN_BATCH: str = "zubot.extraction.batch"
OTEL_SPAN_JOB: str = "zubot.extraction.job"
OTEL_SPAN_STAGE1_DRAWING_NUMBER: str = "zubot.extraction.stage1.drawing_number"
OTEL_SPAN_STAGE1_TITLE: str = "zubot.extraction.stage1.title"
OTEL_SPAN_STAGE1_DOC_TYPE: str = "zubot.extraction.stage1.document_type"
OTEL_SPAN_STAGE2_COMPANION: str = "zubot.extraction.stage2.companion"
OTEL_SPAN_STAGE3_SIDECAR: str = "zubot.extraction.stage3.sidecar"
OTEL_SPAN_CONFIDENCE: str = "zubot.confidence.calculate"


# ---------------------------------------------------------------------------
# Prometheus metric names
# ---------------------------------------------------------------------------

METRIC_EXTRACTION_TOTAL: str = "zubot_extraction_total"
METRIC_EXTRACTION_DURATION: str = "zubot_extraction_duration_seconds"
METRIC_CONFIDENCE_SCORE: str = "zubot_confidence_score"
METRIC_QUEUE_DEPTH: str = "zubot_queue_depth"
METRIC_OLLAMA_REQUESTS: str = "zubot_ollama_requests_total"
METRIC_OLLAMA_DURATION: str = "zubot_ollama_duration_seconds"


# ---------------------------------------------------------------------------
# Ollama models
# ---------------------------------------------------------------------------

OLLAMA_MODEL_VISION: str = "qwen2.5vl:7b"
OLLAMA_MODEL_TEXT: str = "qwen2.5:7b"
OLLAMA_TEMPERATURE_DETERMINISTIC: float = 0.0


# ---------------------------------------------------------------------------
# Auth-exempt routes
# ---------------------------------------------------------------------------

AUTH_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/redoc",
    }
)


# ---------------------------------------------------------------------------
# Structured logging — sensitive value scrubber (CAP-029)
# ---------------------------------------------------------------------------

# Sensitive key fragments that the structured-logging scrubber must redact.
# Any log field whose key contains one of these (case-insensitive) is replaced
# with the REDACTION_PLACEHOLDER value below.
SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "jwt",
    "token",
    "password",
    "secret",
    "file_bytes",
    "file_contents",
    "authorization",
)

REDACTION_PLACEHOLDER: str = "***REDACTED***"


__all__ = [
    # Service identifiers
    "SERVICE_NAME",
    "SERVICE_VERSION",
    # Database pool
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
    "DB_POOL_TIMEOUT",
    "DB_POOL_RECYCLE",
    # Celery / Redis
    "CELERY_BROKER_DB",
    "CELERY_RESULT_DB",
    "RATE_LIMIT_REDIS_DB",
    "CELERY_TASK_NAME_EXTRACTION",
    "CELERY_QUEUE_DEFAULT",
    # ChromaDB
    "CHROMA_COLLECTION_PREFIX",
    "CHROMA_COLLECTION_DEFAULT_DEPLOYMENT",
    "CHROMA_COLLECTION_DEFAULT_NODE",
    "chroma_collection_name",
    # Elasticsearch
    "ES_INDEX_PREFIX",
    "ES_DOCUMENT_ID_PREFIX",
    "es_index_name",
    # Provenance
    "PROVENANCE_INGESTION_SERVICE",
    # Confidence
    "CONFIDENCE_TIER_AUTO_MIN",
    "CONFIDENCE_TIER_SPOT_MIN",
    "CONFIDENCE_TIER_AUTO",
    "CONFIDENCE_TIER_SPOT",
    "CONFIDENCE_TIER_REVIEW",
    "CONFIDENCE_WEIGHT_DRAWING_NUMBER",
    "CONFIDENCE_WEIGHT_TITLE",
    "CONFIDENCE_WEIGHT_DOCUMENT_TYPE",
    "CONFIDENCE_VALIDATION_PENALTY",
    # Pipeline limits
    "MAX_COMPANION_PAGES",
    "MAX_SIDECAR_METADATA_KEYS",
    "TEXT_ONLY_THRESHOLD_CHARS",
    "TEXT_ONLY_THRESHOLD_PAGES",
    "COMPANION_DESCRIPTION_PROMPT_V1",
    # OTEL
    "OTEL_SPAN_BATCH",
    "OTEL_SPAN_JOB",
    "OTEL_SPAN_STAGE1_DRAWING_NUMBER",
    "OTEL_SPAN_STAGE1_TITLE",
    "OTEL_SPAN_STAGE1_DOC_TYPE",
    "OTEL_SPAN_STAGE2_COMPANION",
    "OTEL_SPAN_STAGE3_SIDECAR",
    "OTEL_SPAN_CONFIDENCE",
    # Prometheus
    "METRIC_EXTRACTION_TOTAL",
    "METRIC_EXTRACTION_DURATION",
    "METRIC_CONFIDENCE_SCORE",
    "METRIC_QUEUE_DEPTH",
    "METRIC_OLLAMA_REQUESTS",
    "METRIC_OLLAMA_DURATION",
    # Ollama
    "OLLAMA_MODEL_VISION",
    "OLLAMA_MODEL_TEXT",
    "OLLAMA_TEMPERATURE_DETERMINISTIC",
    # Auth
    "AUTH_EXEMPT_PATHS",
    # Structured logging (CAP-029)
    "SENSITIVE_KEY_FRAGMENTS",
    "REDACTION_PLACEHOLDER",
]
