"""Tests for zubot_ingestion.shared.constants.

These tests pin every constant to its expected literal value. Downstream
tasks (6, 7, 9, 10, 14-24) depend on these exact values, so any unexpected
change must be caught immediately.
"""

from __future__ import annotations

import pytest

from zubot_ingestion.shared import constants as C


# ---------------------------------------------------------------------------
# Service identifiers
# ---------------------------------------------------------------------------


def test_service_name():
    assert C.SERVICE_NAME == "zubot-ingestion"


def test_service_version():
    assert C.SERVICE_VERSION == "0.1.0"


# ---------------------------------------------------------------------------
# Celery / Redis
# ---------------------------------------------------------------------------


def test_celery_redis_db_numbers_unique_and_correct():
    assert C.CELERY_BROKER_DB == 2
    assert C.CELERY_RESULT_DB == 3
    assert C.RATE_LIMIT_REDIS_DB == 4
    # The three Redis DB numbers must not collide.
    assert (
        len({C.CELERY_BROKER_DB, C.CELERY_RESULT_DB, C.RATE_LIMIT_REDIS_DB})
        == 3
    )


def test_celery_task_name_extraction():
    assert C.CELERY_TASK_NAME_EXTRACTION == "zubot_ingestion.tasks.extract_document"


def test_celery_queue_default():
    assert C.CELERY_QUEUE_DEFAULT == "zubot_ingestion"


# ---------------------------------------------------------------------------
# ChromaDB
# ---------------------------------------------------------------------------


def test_chroma_collection_prefix_and_defaults():
    assert C.CHROMA_COLLECTION_PREFIX == "zubot_metadata_"
    assert C.CHROMA_COLLECTION_DEFAULT_DEPLOYMENT == "default"
    assert C.CHROMA_COLLECTION_DEFAULT_NODE == "default"


@pytest.mark.parametrize(
    ("deployment_id", "node_id", "expected"),
    [
        (None, None, "zubot_metadata_default_default"),
        ("acme", None, "zubot_metadata_acme_default"),
        (None, "node-1", "zubot_metadata_default_node-1"),
        ("acme", "node-1", "zubot_metadata_acme_node-1"),
        ("", "", "zubot_metadata_default_default"),  # empty string -> default
    ],
)
def test_chroma_collection_name(deployment_id, node_id, expected):
    assert C.chroma_collection_name(deployment_id, node_id) == expected


# ---------------------------------------------------------------------------
# Elasticsearch
# ---------------------------------------------------------------------------


def test_es_index_prefix():
    assert C.ES_INDEX_PREFIX == "zubot_companion_"


def test_es_document_id_prefix():
    assert C.ES_DOCUMENT_ID_PREFIX == "companion_"


@pytest.mark.parametrize(
    ("deployment_id", "expected"),
    [
        (None, "zubot_companion_default"),
        ("acme", "zubot_companion_acme"),
        ("", "zubot_companion_default"),
    ],
)
def test_es_index_name(deployment_id, expected):
    assert C.es_index_name(deployment_id) == expected


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_provenance_ingestion_service():
    assert C.PROVENANCE_INGESTION_SERVICE == "zubot-ingestion"
    # Provenance must match the service name.
    assert C.PROVENANCE_INGESTION_SERVICE == C.SERVICE_NAME


# ---------------------------------------------------------------------------
# Confidence thresholds and weights
# ---------------------------------------------------------------------------


def test_confidence_tier_thresholds():
    assert C.CONFIDENCE_TIER_AUTO_MIN == 0.8
    assert C.CONFIDENCE_TIER_SPOT_MIN == 0.5
    assert C.CONFIDENCE_TIER_AUTO_MIN > C.CONFIDENCE_TIER_SPOT_MIN


def test_confidence_weights():
    assert C.CONFIDENCE_WEIGHT_DRAWING_NUMBER == 0.40
    assert C.CONFIDENCE_WEIGHT_TITLE == 0.30
    assert C.CONFIDENCE_WEIGHT_DOCUMENT_TYPE == 0.30


def test_confidence_weights_sum_to_one():
    total = (
        C.CONFIDENCE_WEIGHT_DRAWING_NUMBER
        + C.CONFIDENCE_WEIGHT_TITLE
        + C.CONFIDENCE_WEIGHT_DOCUMENT_TYPE
    )
    assert total == pytest.approx(1.0)


def test_confidence_validation_penalty():
    assert C.CONFIDENCE_VALIDATION_PENALTY == -0.10


# ---------------------------------------------------------------------------
# Pipeline limits
# ---------------------------------------------------------------------------


def test_max_companion_pages():
    assert C.MAX_COMPANION_PAGES == 4


def test_max_sidecar_metadata_keys():
    # AWS Bedrock Knowledge Base hard limit.
    assert C.MAX_SIDECAR_METADATA_KEYS == 10


# ---------------------------------------------------------------------------
# OTEL span names
# ---------------------------------------------------------------------------


def test_otel_span_names():
    assert C.OTEL_SPAN_BATCH == "zubot.extraction.batch"
    assert C.OTEL_SPAN_JOB == "zubot.extraction.job"
    assert C.OTEL_SPAN_STAGE1_DRAWING_NUMBER == "zubot.extraction.stage1.drawing_number"
    assert C.OTEL_SPAN_STAGE1_TITLE == "zubot.extraction.stage1.title"
    assert C.OTEL_SPAN_STAGE1_DOC_TYPE == "zubot.extraction.stage1.document_type"
    assert C.OTEL_SPAN_STAGE2_COMPANION == "zubot.extraction.stage2.companion"
    assert C.OTEL_SPAN_STAGE3_SIDECAR == "zubot.extraction.stage3.sidecar"
    assert C.OTEL_SPAN_CONFIDENCE == "zubot.confidence.calculate"


def test_otel_span_names_unique():
    spans = {
        C.OTEL_SPAN_BATCH,
        C.OTEL_SPAN_JOB,
        C.OTEL_SPAN_STAGE1_DRAWING_NUMBER,
        C.OTEL_SPAN_STAGE1_TITLE,
        C.OTEL_SPAN_STAGE1_DOC_TYPE,
        C.OTEL_SPAN_STAGE2_COMPANION,
        C.OTEL_SPAN_STAGE3_SIDECAR,
        C.OTEL_SPAN_CONFIDENCE,
    }
    assert len(spans) == 8


# ---------------------------------------------------------------------------
# Prometheus metric names
# ---------------------------------------------------------------------------


def test_prometheus_metric_names():
    assert C.METRIC_EXTRACTION_TOTAL == "zubot_extraction_total"
    assert C.METRIC_EXTRACTION_DURATION == "zubot_extraction_duration_seconds"
    assert C.METRIC_CONFIDENCE_SCORE == "zubot_confidence_score"
    assert C.METRIC_QUEUE_DEPTH == "zubot_queue_depth"
    assert C.METRIC_OLLAMA_REQUESTS == "zubot_ollama_requests_total"
    assert C.METRIC_OLLAMA_DURATION == "zubot_ollama_duration_seconds"


def test_prometheus_metric_names_unique():
    metrics = {
        C.METRIC_EXTRACTION_TOTAL,
        C.METRIC_EXTRACTION_DURATION,
        C.METRIC_CONFIDENCE_SCORE,
        C.METRIC_QUEUE_DEPTH,
        C.METRIC_OLLAMA_REQUESTS,
        C.METRIC_OLLAMA_DURATION,
    }
    assert len(metrics) == 6


# ---------------------------------------------------------------------------
# Ollama models
# ---------------------------------------------------------------------------


def test_ollama_models():
    assert C.OLLAMA_MODEL_VISION == "qwen2.5vl:7b"
    assert C.OLLAMA_MODEL_TEXT == "qwen2.5:7b"


def test_ollama_temperature_deterministic():
    assert C.OLLAMA_TEMPERATURE_DETERMINISTIC == 0.0


# ---------------------------------------------------------------------------
# Auth-exempt routes
# ---------------------------------------------------------------------------


def test_auth_exempt_paths_is_frozenset():
    assert isinstance(C.AUTH_EXEMPT_PATHS, frozenset)


def test_auth_exempt_paths_contents():
    expected = {"/health", "/metrics", "/docs", "/openapi.json", "/redoc"}
    assert C.AUTH_EXEMPT_PATHS == expected


def test_auth_exempt_paths_immutable():
    # frozenset has no .add method, so attempting mutation must fail.
    with pytest.raises(AttributeError):
        C.AUTH_EXEMPT_PATHS.add("/admin")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Module-level: __all__ exports everything we expect downstream tasks to use
# ---------------------------------------------------------------------------


def test_all_exports_present():
    expected_symbols = {
        "SERVICE_NAME",
        "SERVICE_VERSION",
        "CELERY_BROKER_DB",
        "CELERY_RESULT_DB",
        "RATE_LIMIT_REDIS_DB",
        "CELERY_TASK_NAME_EXTRACTION",
        "CELERY_QUEUE_DEFAULT",
        "CHROMA_COLLECTION_PREFIX",
        "CHROMA_COLLECTION_DEFAULT_DEPLOYMENT",
        "CHROMA_COLLECTION_DEFAULT_NODE",
        "chroma_collection_name",
        "ES_INDEX_PREFIX",
        "ES_DOCUMENT_ID_PREFIX",
        "es_index_name",
        "PROVENANCE_INGESTION_SERVICE",
        "CONFIDENCE_TIER_AUTO_MIN",
        "CONFIDENCE_TIER_SPOT_MIN",
        "CONFIDENCE_TIER_AUTO",
        "CONFIDENCE_TIER_SPOT",
        "CONFIDENCE_TIER_REVIEW",
        "CONFIDENCE_WEIGHT_DRAWING_NUMBER",
        "CONFIDENCE_WEIGHT_TITLE",
        "CONFIDENCE_WEIGHT_DOCUMENT_TYPE",
        "CONFIDENCE_VALIDATION_PENALTY",
        "MAX_COMPANION_PAGES",
        "MAX_SIDECAR_METADATA_KEYS",
        "TEXT_ONLY_THRESHOLD_CHARS",
        "TEXT_ONLY_THRESHOLD_PAGES",
        "OTEL_SPAN_BATCH",
        "OTEL_SPAN_JOB",
        "OTEL_SPAN_STAGE1_DRAWING_NUMBER",
        "OTEL_SPAN_STAGE1_TITLE",
        "OTEL_SPAN_STAGE1_DOC_TYPE",
        "OTEL_SPAN_STAGE2_COMPANION",
        "OTEL_SPAN_STAGE3_SIDECAR",
        "OTEL_SPAN_CONFIDENCE",
        "METRIC_EXTRACTION_TOTAL",
        "METRIC_EXTRACTION_DURATION",
        "METRIC_CONFIDENCE_SCORE",
        "METRIC_QUEUE_DEPTH",
        "METRIC_OLLAMA_REQUESTS",
        "METRIC_OLLAMA_DURATION",
        "OLLAMA_MODEL_VISION",
        "OLLAMA_MODEL_TEXT",
        "OLLAMA_TEMPERATURE_DETERMINISTIC",
        "AUTH_EXEMPT_PATHS",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "DB_POOL_TIMEOUT",
        "DB_POOL_RECYCLE",
        "SENSITIVE_KEY_FRAGMENTS",
        "REDACTION_PLACEHOLDER",
        "PLACEHOLDER_SENTINEL_OVERWRITE",
        "PLACEHOLDER_SENTINEL_OWNED_BY",
        "BUILD_GATE_PROTECTED_FILES",
        "COMPANION_DESCRIPTION_PROMPT_V1",
    }
    assert set(C.__all__) == expected_symbols


def test_all_exports_resolvable():
    """Every name in __all__ must actually exist on the module."""
    for name in C.__all__:
        assert hasattr(C, name), f"Missing export: {name}"
