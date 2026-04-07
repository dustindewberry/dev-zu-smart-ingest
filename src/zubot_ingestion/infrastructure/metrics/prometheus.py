"""Prometheus metric singletons for the zubot-ingestion service.

Implements CAP-028.

This module is the single source of truth for every Prometheus metric
exposed by the service. All metrics are module-level singletons created
at import time and registered with the default :class:`CollectorRegistry`
of :mod:`prometheus_client`. Any layer (API, services, infrastructure)
that needs to record a measurement should ``from
zubot_ingestion.infrastructure.metrics.prometheus import <metric>`` and
call ``.labels(...).inc()`` / ``.observe(...)`` / ``.set(...)``
directly. The metrics module itself never imports from any other
zubot_ingestion module besides :mod:`zubot_ingestion.shared.constants`,
which keeps the dependency direction clean (metrics is leaf-level
infrastructure).

Metric inventory:

    extraction_total      Counter[status]
    extraction_duration   Histogram (1, 5, 10, 30, 60, 120, 300 s)
    confidence_score      Histogram[field] (0, 0.2, 0.4, 0.5, 0.6, 0.8, 0.9, 1.0)
    queue_depth           Gauge
    ollama_requests       Counter[model, status]
    ollama_duration       Histogram[model] (0.5, 1, 2, 5, 10, 30, 60 s)

Re-exports :data:`CONTENT_TYPE_LATEST` and :func:`generate_latest` from
:mod:`prometheus_client` so the API layer can build the
``GET /metrics`` response without taking a direct dependency on the
upstream library import path.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from zubot_ingestion.shared.constants import (
    METRIC_CONFIDENCE_SCORE,
    METRIC_EXTRACTION_DURATION,
    METRIC_EXTRACTION_TOTAL,
    METRIC_OLLAMA_DURATION,
    METRIC_OLLAMA_REQUESTS,
    METRIC_QUEUE_DEPTH,
)

__all__ = [
    "extraction_total",
    "extraction_duration",
    "confidence_score",
    "queue_depth",
    "ollama_requests",
    "ollama_duration",
    "CONTENT_TYPE_LATEST",
    "generate_latest",
    "EXTRACTION_DURATION_BUCKETS",
    "CONFIDENCE_SCORE_BUCKETS",
    "OLLAMA_DURATION_BUCKETS",
]


# ---------------------------------------------------------------------------
# Bucket definitions (exposed as module-level tuples so tests and callers
# can pin / introspect them)
# ---------------------------------------------------------------------------

EXTRACTION_DURATION_BUCKETS: tuple[float, ...] = (1, 5, 10, 30, 60, 120, 300)
CONFIDENCE_SCORE_BUCKETS: tuple[float, ...] = (
    0.0,
    0.2,
    0.4,
    0.5,
    0.6,
    0.8,
    0.9,
    1.0,
)
OLLAMA_DURATION_BUCKETS: tuple[float, ...] = (0.5, 1, 2, 5, 10, 30, 60)


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

extraction_total: Counter = Counter(
    METRIC_EXTRACTION_TOTAL,
    "Total extractions",
    ["status"],
)

ollama_requests: Counter = Counter(
    METRIC_OLLAMA_REQUESTS,
    "Ollama API requests",
    ["model", "status"],
)


# ---------------------------------------------------------------------------
# Histograms
# ---------------------------------------------------------------------------

extraction_duration: Histogram = Histogram(
    METRIC_EXTRACTION_DURATION,
    "Extraction duration in seconds",
    buckets=EXTRACTION_DURATION_BUCKETS,
)

confidence_score: Histogram = Histogram(
    METRIC_CONFIDENCE_SCORE,
    "Confidence score per field",
    ["field"],
    buckets=CONFIDENCE_SCORE_BUCKETS,
)

ollama_duration: Histogram = Histogram(
    METRIC_OLLAMA_DURATION,
    "Ollama request duration",
    ["model"],
    buckets=OLLAMA_DURATION_BUCKETS,
)


# ---------------------------------------------------------------------------
# Gauges
# ---------------------------------------------------------------------------

queue_depth: Gauge = Gauge(
    METRIC_QUEUE_DEPTH,
    "Current Celery queue depth",
)
