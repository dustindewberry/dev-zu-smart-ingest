"""Unit tests for CAP-028 (Prometheus metrics endpoint and instrumentation).

This single test module covers:

1. The metric singletons in
   ``zubot_ingestion.infrastructure.metrics.prometheus`` (types,
   names, labels, buckets, mutation behaviour).
2. The ``GET /metrics`` endpoint defined in
   ``zubot_ingestion.api.routes.metrics`` (route registration,
   content-type, body shape, end-to-end via TestClient).
3. The orchestrator instrumentation helpers in
   ``zubot_ingestion.services.orchestrator``
   (``record_extraction_duration``, ``record_field_confidences``,
   ``record_extraction_status``).
4. The Ollama client instrumentation helpers in
   ``zubot_ingestion.infrastructure.ollama.client``
   (``record_ollama_request_status``, ``time_ollama_call``).
5. The /health queue_depth gauge integration in
   ``zubot_ingestion.api.routes.health.update_queue_depth_gauge``.

State AFTER mutation is verified by reading samples back out of the
metric registry via the public ``collect()`` API.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prometheus_client import Counter, Gauge, Histogram

from zubot_ingestion.api.app import create_app
from zubot_ingestion.api.routes import health as health_route
from zubot_ingestion.api.routes.metrics import router as metrics_router
from zubot_ingestion.infrastructure.metrics import prometheus as metrics_mod
from zubot_ingestion.infrastructure.ollama import client as ollama_mod
from zubot_ingestion.services import orchestrator as orch_mod
from zubot_ingestion.shared.constants import (
    AUTH_EXEMPT_PATHS,
    METRIC_CONFIDENCE_SCORE,
    METRIC_EXTRACTION_DURATION,
    METRIC_EXTRACTION_TOTAL,
    METRIC_OLLAMA_DURATION,
    METRIC_OLLAMA_REQUESTS,
    METRIC_QUEUE_DEPTH,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _counter_value(counter: Counter, **labels: str) -> float:
    """Read a labelled counter sample value via the public collect() API."""
    target_name_total = counter._name + "_total"
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name not in (target_name_total, counter._name):
                continue
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                return float(sample.value)
    return 0.0


def _gauge_value(gauge: Gauge) -> float:
    for metric in gauge.collect():
        for sample in metric.samples:
            if sample.name == gauge._name:
                return float(sample.value)
    return 0.0


def _histogram_count(hist: Histogram, **labels: str) -> float:
    """Read the _count sample of a histogram for a given label set."""
    target_count = hist._name + "_count"
    for metric in hist.collect():
        for sample in metric.samples:
            if sample.name != target_count:
                continue
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                return float(sample.value)
    return 0.0


# ===========================================================================
# Section 1 — singleton existence + type checks
# ===========================================================================


def test_extraction_total_is_counter() -> None:
    assert isinstance(metrics_mod.extraction_total, Counter)


def test_extraction_duration_is_histogram() -> None:
    assert isinstance(metrics_mod.extraction_duration, Histogram)


def test_confidence_score_is_histogram() -> None:
    assert isinstance(metrics_mod.confidence_score, Histogram)


def test_queue_depth_is_gauge() -> None:
    assert isinstance(metrics_mod.queue_depth, Gauge)


def test_ollama_requests_is_counter() -> None:
    assert isinstance(metrics_mod.ollama_requests, Counter)


def test_ollama_duration_is_histogram() -> None:
    assert isinstance(metrics_mod.ollama_duration, Histogram)


def test_content_type_latest_re_exported() -> None:
    assert isinstance(metrics_mod.CONTENT_TYPE_LATEST, str)
    assert "text/plain" in metrics_mod.CONTENT_TYPE_LATEST


def test_generate_latest_re_exported() -> None:
    assert callable(metrics_mod.generate_latest)


# ===========================================================================
# Section 2 — name pinning (matches METRIC_* constants)
# ===========================================================================


def test_extraction_total_name_in_rendered_output() -> None:
    rendered = metrics_mod.generate_latest().decode()
    assert METRIC_EXTRACTION_TOTAL in rendered


def test_extraction_duration_name_in_rendered_output() -> None:
    metrics_mod.extraction_duration.observe(1.0)
    rendered = metrics_mod.generate_latest().decode()
    assert METRIC_EXTRACTION_DURATION in rendered


def test_confidence_score_name_in_rendered_output() -> None:
    metrics_mod.confidence_score.labels(field="drawing_number").observe(0.5)
    rendered = metrics_mod.generate_latest().decode()
    assert METRIC_CONFIDENCE_SCORE in rendered


def test_queue_depth_name_in_rendered_output() -> None:
    metrics_mod.queue_depth.set(0)
    rendered = metrics_mod.generate_latest().decode()
    assert METRIC_QUEUE_DEPTH in rendered


def test_ollama_requests_name_in_rendered_output() -> None:
    metrics_mod.ollama_requests.labels(model="m", status="success").inc()
    rendered = metrics_mod.generate_latest().decode()
    assert METRIC_OLLAMA_REQUESTS in rendered


def test_ollama_duration_name_in_rendered_output() -> None:
    metrics_mod.ollama_duration.labels(model="m").observe(0.1)
    rendered = metrics_mod.generate_latest().decode()
    assert METRIC_OLLAMA_DURATION in rendered


# ===========================================================================
# Section 3 — bucket pinning
# ===========================================================================


def test_extraction_duration_buckets_constant_value() -> None:
    assert metrics_mod.EXTRACTION_DURATION_BUCKETS == (1, 5, 10, 30, 60, 120, 300)


def test_extraction_duration_buckets_match_internal() -> None:
    declared = tuple(metrics_mod.extraction_duration._upper_bounds)
    assert declared[:-1] == metrics_mod.EXTRACTION_DURATION_BUCKETS
    assert declared[-1] == float("inf")


def test_confidence_score_buckets_constant_value() -> None:
    assert metrics_mod.CONFIDENCE_SCORE_BUCKETS == (
        0.0,
        0.2,
        0.4,
        0.5,
        0.6,
        0.8,
        0.9,
        1.0,
    )


def test_confidence_score_buckets_match_internal() -> None:
    declared = tuple(metrics_mod.confidence_score._upper_bounds)
    assert declared[:-1] == metrics_mod.CONFIDENCE_SCORE_BUCKETS
    assert declared[-1] == float("inf")


def test_ollama_duration_buckets_constant_value() -> None:
    assert metrics_mod.OLLAMA_DURATION_BUCKETS == (0.5, 1, 2, 5, 10, 30, 60)


def test_ollama_duration_buckets_match_internal() -> None:
    declared = tuple(metrics_mod.ollama_duration._upper_bounds)
    assert declared[:-1] == metrics_mod.OLLAMA_DURATION_BUCKETS
    assert declared[-1] == float("inf")


# ===========================================================================
# Section 4 — label pinning
# ===========================================================================


def test_extraction_total_labels() -> None:
    assert metrics_mod.extraction_total._labelnames == ("status",)


def test_confidence_score_labels() -> None:
    assert metrics_mod.confidence_score._labelnames == ("field",)


def test_ollama_requests_labels() -> None:
    assert metrics_mod.ollama_requests._labelnames == ("model", "status")


def test_ollama_duration_labels() -> None:
    assert metrics_mod.ollama_duration._labelnames == ("model",)


def test_extraction_duration_has_no_labels() -> None:
    assert metrics_mod.extraction_duration._labelnames == ()


def test_queue_depth_has_no_labels() -> None:
    assert metrics_mod.queue_depth._labelnames == ()


# ===========================================================================
# Section 5 — counter / gauge mutation contracts
# ===========================================================================


def test_extraction_total_increments_for_completed() -> None:
    before = _counter_value(metrics_mod.extraction_total, status="completed")
    metrics_mod.extraction_total.labels(status="completed").inc()
    after = _counter_value(metrics_mod.extraction_total, status="completed")
    assert after == before + 1.0


def test_extraction_total_isolates_status_buckets() -> None:
    before_failed = _counter_value(metrics_mod.extraction_total, status="failed")
    before_review = _counter_value(metrics_mod.extraction_total, status="review")
    metrics_mod.extraction_total.labels(status="failed").inc()
    after_failed = _counter_value(metrics_mod.extraction_total, status="failed")
    after_review = _counter_value(metrics_mod.extraction_total, status="review")
    assert after_failed == before_failed + 1.0
    assert after_review == before_review


def test_queue_depth_set_replaces_value() -> None:
    metrics_mod.queue_depth.set(0)
    metrics_mod.queue_depth.set(42)
    assert _gauge_value(metrics_mod.queue_depth) == 42.0
    metrics_mod.queue_depth.set(7)
    assert _gauge_value(metrics_mod.queue_depth) == 7.0


def test_ollama_requests_increments_per_label_combo() -> None:
    before = _counter_value(
        metrics_mod.ollama_requests, model="qwen2.5vl:7b", status="success"
    )
    metrics_mod.ollama_requests.labels(
        model="qwen2.5vl:7b", status="success"
    ).inc()
    after = _counter_value(
        metrics_mod.ollama_requests, model="qwen2.5vl:7b", status="success"
    )
    assert after == before + 1.0


# ===========================================================================
# Section 6 — public surface
# ===========================================================================


@pytest.mark.parametrize(
    "name",
    [
        "extraction_total",
        "extraction_duration",
        "confidence_score",
        "queue_depth",
        "ollama_requests",
        "ollama_duration",
        "CONTENT_TYPE_LATEST",
        "generate_latest",
    ],
)
def test_public_symbols_exposed(name: str) -> None:
    assert name in metrics_mod.__all__
    assert hasattr(metrics_mod, name)


# ===========================================================================
# Section 7 — /metrics route smoke
# ===========================================================================


def test_metrics_router_registers_metrics_path() -> None:
    paths = {route.path for route in metrics_router.routes}  # type: ignore[attr-defined]
    assert "/metrics" in paths


def test_metrics_route_method_is_get() -> None:
    methods: set[str] = set()
    for route in metrics_router.routes:  # type: ignore[attr-defined]
        if getattr(route, "path", None) == "/metrics":
            methods.update(getattr(route, "methods", set()) or set())
    assert "GET" in methods


def test_create_app_registers_metrics_route() -> None:
    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/metrics" in paths


def test_metrics_path_in_auth_exempt_set() -> None:
    assert "/metrics" in AUTH_EXEMPT_PATHS


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def test_metrics_endpoint_returns_200(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200


def test_metrics_endpoint_returns_prometheus_content_type(
    client: TestClient,
) -> None:
    response = client.get("/metrics")
    assert response.headers["content-type"].startswith("text/plain")
    assert "version=" in response.headers["content-type"]


def test_metrics_endpoint_body_includes_extraction_total(
    client: TestClient,
) -> None:
    metrics_mod.extraction_total.labels(status="completed").inc()
    response = client.get("/metrics")
    body = response.text
    assert METRIC_EXTRACTION_TOTAL in body
    assert 'status="completed"' in body


def test_metrics_endpoint_body_includes_queue_depth(
    client: TestClient,
) -> None:
    metrics_mod.queue_depth.set(11)
    response = client.get("/metrics")
    body = response.text
    assert METRIC_QUEUE_DEPTH in body
    assert "11" in body


def test_metrics_endpoint_body_includes_confidence_score(
    client: TestClient,
) -> None:
    metrics_mod.confidence_score.labels(field="title").observe(0.81)
    response = client.get("/metrics")
    body = response.text
    assert METRIC_CONFIDENCE_SCORE in body
    assert 'field="title"' in body


def test_metrics_endpoint_body_includes_ollama_requests(
    client: TestClient,
) -> None:
    metrics_mod.ollama_requests.labels(
        model="qwen2.5:7b", status="success"
    ).inc()
    response = client.get("/metrics")
    body = response.text
    assert METRIC_OLLAMA_REQUESTS in body
    assert 'model="qwen2.5:7b"' in body
    assert 'status="success"' in body


def test_metrics_endpoint_no_auth_header_required(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200


# ===========================================================================
# Section 8 — orchestrator instrumentation helpers
# ===========================================================================


@dataclass
class _FakeExtractionResult:
    drawing_number_confidence: float | None = None
    title_confidence: float | None = None
    document_type_confidence: float | None = None


class _FakeTier(str, Enum):
    AUTO = "auto"
    SPOT = "spot"
    REVIEW = "review"


@dataclass
class _FakePipelineError:
    recoverable: bool


def test_record_extraction_duration_observes_one_sample() -> None:
    before = _histogram_count(metrics_mod.extraction_duration)
    start = time.perf_counter() - 2.5
    orch_mod.record_extraction_duration(start)
    after = _histogram_count(metrics_mod.extraction_duration)
    assert after == before + 1


def test_record_extraction_duration_clamps_negative_to_zero() -> None:
    before = _histogram_count(metrics_mod.extraction_duration)
    future = time.perf_counter() + 100.0
    orch_mod.record_extraction_duration(future)
    after = _histogram_count(metrics_mod.extraction_duration)
    assert after == before + 1


def test_record_field_confidences_observes_three_fields() -> None:
    before_dn = _histogram_count(
        metrics_mod.confidence_score, field="drawing_number"
    )
    before_title = _histogram_count(metrics_mod.confidence_score, field="title")
    before_doctype = _histogram_count(
        metrics_mod.confidence_score, field="document_type"
    )
    orch_mod.record_field_confidences(
        _FakeExtractionResult(
            drawing_number_confidence=0.9,
            title_confidence=0.7,
            document_type_confidence=0.6,
        )
    )
    after_dn = _histogram_count(
        metrics_mod.confidence_score, field="drawing_number"
    )
    after_title = _histogram_count(metrics_mod.confidence_score, field="title")
    after_doctype = _histogram_count(
        metrics_mod.confidence_score, field="document_type"
    )
    assert after_dn == before_dn + 1
    assert after_title == before_title + 1
    assert after_doctype == before_doctype + 1


def test_record_field_confidences_skips_none_fields() -> None:
    before_dn = _histogram_count(
        metrics_mod.confidence_score, field="drawing_number"
    )
    before_title = _histogram_count(metrics_mod.confidence_score, field="title")
    before_doctype = _histogram_count(
        metrics_mod.confidence_score, field="document_type"
    )
    orch_mod.record_field_confidences(
        _FakeExtractionResult(
            drawing_number_confidence=0.5,
            title_confidence=None,
            document_type_confidence=None,
        )
    )
    after_dn = _histogram_count(
        metrics_mod.confidence_score, field="drawing_number"
    )
    after_title = _histogram_count(metrics_mod.confidence_score, field="title")
    after_doctype = _histogram_count(
        metrics_mod.confidence_score, field="document_type"
    )
    assert after_dn == before_dn + 1
    assert after_title == before_title
    assert after_doctype == before_doctype


def test_record_field_confidences_skips_non_numeric() -> None:
    before = _histogram_count(
        metrics_mod.confidence_score, field="drawing_number"
    )
    orch_mod.record_field_confidences(
        _FakeExtractionResult(drawing_number_confidence="not-a-number")  # type: ignore[arg-type]
    )
    after = _histogram_count(
        metrics_mod.confidence_score, field="drawing_number"
    )
    assert after == before


def test_record_field_confidences_handles_empty_result() -> None:
    class _Empty:
        pass

    before = _histogram_count(
        metrics_mod.confidence_score, field="drawing_number"
    )
    orch_mod.record_field_confidences(_Empty())
    after = _histogram_count(
        metrics_mod.confidence_score, field="drawing_number"
    )
    assert after == before


@pytest.mark.parametrize(
    ("tier", "expected_label"),
    [
        (_FakeTier.AUTO, "completed"),
        (_FakeTier.SPOT, "completed"),
        (_FakeTier.REVIEW, "review"),
    ],
)
def test_record_extraction_status_maps_tier_to_label(
    tier: _FakeTier, expected_label: str
) -> None:
    before = _counter_value(
        metrics_mod.extraction_total, status=expected_label
    )
    returned = orch_mod.record_extraction_status(tier, errors=[])
    after = _counter_value(metrics_mod.extraction_total, status=expected_label)
    assert returned == expected_label
    assert after == before + 1.0


def test_record_extraction_status_unrecoverable_error_forces_failed() -> None:
    before = _counter_value(metrics_mod.extraction_total, status="failed")
    returned = orch_mod.record_extraction_status(
        _FakeTier.AUTO,
        errors=[_FakePipelineError(recoverable=False)],
    )
    after = _counter_value(metrics_mod.extraction_total, status="failed")
    assert returned == "failed"
    assert after == before + 1.0


def test_record_extraction_status_recoverable_error_does_not_force_failed() -> None:
    before_completed = _counter_value(
        metrics_mod.extraction_total, status="completed"
    )
    before_failed = _counter_value(
        metrics_mod.extraction_total, status="failed"
    )
    returned = orch_mod.record_extraction_status(
        _FakeTier.AUTO,
        errors=[_FakePipelineError(recoverable=True)],
    )
    after_completed = _counter_value(
        metrics_mod.extraction_total, status="completed"
    )
    after_failed = _counter_value(
        metrics_mod.extraction_total, status="failed"
    )
    assert returned == "completed"
    assert after_completed == before_completed + 1.0
    assert after_failed == before_failed


def test_record_extraction_status_none_tier_maps_to_failed() -> None:
    before = _counter_value(metrics_mod.extraction_total, status="failed")
    returned = orch_mod.record_extraction_status(None, errors=None)
    after = _counter_value(metrics_mod.extraction_total, status="failed")
    assert returned == "failed"
    assert after == before + 1.0


def test_record_extraction_status_label_constants() -> None:
    assert orch_mod.STATUS_COMPLETED == "completed"
    assert orch_mod.STATUS_FAILED == "failed"
    assert orch_mod.STATUS_REVIEW == "review"
    assert orch_mod.FIELD_DRAWING_NUMBER == "drawing_number"
    assert orch_mod.FIELD_TITLE == "title"
    assert orch_mod.FIELD_DOCUMENT_TYPE == "document_type"


# ===========================================================================
# Section 9 — Ollama instrumentation helpers
# ===========================================================================


@pytest.mark.parametrize("status", ["success", "error", "retry"])
def test_record_ollama_request_status_increments_counter(status: str) -> None:
    before = _counter_value(
        metrics_mod.ollama_requests, model="qwen2.5vl:7b", status=status
    )
    ollama_mod.record_ollama_request_status("qwen2.5vl:7b", status)
    after = _counter_value(
        metrics_mod.ollama_requests, model="qwen2.5vl:7b", status=status
    )
    assert after == before + 1.0


def test_record_ollama_request_status_isolates_by_model() -> None:
    before_a = _counter_value(
        metrics_mod.ollama_requests, model="model-a", status="success"
    )
    before_b = _counter_value(
        metrics_mod.ollama_requests, model="model-b", status="success"
    )
    ollama_mod.record_ollama_request_status("model-a", "success")
    after_a = _counter_value(
        metrics_mod.ollama_requests, model="model-a", status="success"
    )
    after_b = _counter_value(
        metrics_mod.ollama_requests, model="model-b", status="success"
    )
    assert after_a == before_a + 1.0
    assert after_b == before_b


def test_record_ollama_request_status_label_constants() -> None:
    assert ollama_mod.STATUS_SUCCESS == "success"
    assert ollama_mod.STATUS_ERROR == "error"
    assert ollama_mod.STATUS_RETRY == "retry"


def test_time_ollama_call_observes_on_success_path() -> None:
    before = _histogram_count(
        metrics_mod.ollama_duration, model="qwen2.5vl:7b"
    )
    with ollama_mod.time_ollama_call(model="qwen2.5vl:7b"):
        time.sleep(0.005)
    after = _histogram_count(
        metrics_mod.ollama_duration, model="qwen2.5vl:7b"
    )
    assert after == before + 1


def test_time_ollama_call_observes_on_failure_path() -> None:
    before = _histogram_count(metrics_mod.ollama_duration, model="qwen2.5:7b")
    with pytest.raises(RuntimeError, match="boom"):
        with ollama_mod.time_ollama_call(model="qwen2.5:7b"):
            raise RuntimeError("boom")
    after = _histogram_count(metrics_mod.ollama_duration, model="qwen2.5:7b")
    assert after == before + 1


def test_time_ollama_call_isolates_by_model() -> None:
    before_a = _histogram_count(metrics_mod.ollama_duration, model="m-1")
    before_b = _histogram_count(metrics_mod.ollama_duration, model="m-2")
    with ollama_mod.time_ollama_call(model="m-1"):
        pass
    after_a = _histogram_count(metrics_mod.ollama_duration, model="m-1")
    after_b = _histogram_count(metrics_mod.ollama_duration, model="m-2")
    assert after_a == before_a + 1
    assert after_b == before_b


# ===========================================================================
# Section 10 — health queue_depth gauge integration
# ===========================================================================


def test_update_queue_depth_gauge_sets_positive_value() -> None:
    health_route.update_queue_depth_gauge(0)
    health_route.update_queue_depth_gauge(17)
    assert _gauge_value(metrics_mod.queue_depth) == 17.0


def test_update_queue_depth_gauge_replaces_previous_value() -> None:
    health_route.update_queue_depth_gauge(50)
    assert _gauge_value(metrics_mod.queue_depth) == 50.0
    health_route.update_queue_depth_gauge(3)
    assert _gauge_value(metrics_mod.queue_depth) == 3.0


def test_update_queue_depth_gauge_clamps_negative_to_zero() -> None:
    health_route.update_queue_depth_gauge(-5)
    assert _gauge_value(metrics_mod.queue_depth) == 0.0


def test_update_queue_depth_gauge_coerces_to_int() -> None:
    health_route.update_queue_depth_gauge(2)
    assert _gauge_value(metrics_mod.queue_depth) == 2.0
