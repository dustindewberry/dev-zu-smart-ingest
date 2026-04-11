# Observability Guide

This guide covers the three observability pillars in `zubot_ingestion`:
**OpenTelemetry tracing**, **Prometheus metrics**, and **structured logging**.
Together they give you end-to-end visibility from an incoming HTTP request
through the Celery extraction pipeline and back.

---

## Table of Contents

1. [OpenTelemetry Tracing](#opentelemetry-tracing)
2. [Prometheus Metrics](#prometheus-metrics)
3. [Structured Logging](#structured-logging)
4. [End-to-End Correlation](#end-to-end-correlation)

---

## OpenTelemetry Tracing

### Setup

OTEL tracing is initialised by calling `setup_otel()` during the FastAPI
lifespan startup (and, optionally, from the Celery `worker_process_init`
signal). The implementation lives in:

```
src/zubot_ingestion/infrastructure/otel/instrumentation.py
```

Key characteristics:

- **Idempotent** -- `setup_otel()` resets the global `TracerProvider` latch
  before installing a new one, so it is safe to call multiple times (e.g. when
  FastAPI's lifespan handler runs more than once under `TestClient`).
- **Service resource** -- every span carries:
  - `service.name = "zubot-ingestion"` (from `SERVICE_NAME`)
  - `service.version = "0.1.0"` (from `SERVICE_VERSION`)
- **OTLP gRPC exporter** -- attached via `BatchSpanProcessor` only when
  `OTEL_EXPORTER_OTLP_ENDPOINT` is a truthy string. When the variable is
  unset (local dev, unit tests), spans remain in-process and the provider acts
  as a noop exporter.
- **Auto-instrumentation** -- the following instrumentors are registered, each
  in its own `try/except` so a missing optional dependency never blocks
  startup:
  - FastAPI (`FastAPIInstrumentor`)
  - HTTPX (`HTTPXClientInstrumentor`)
  - asyncpg (`AsyncPGInstrumentor`)
  - Redis (`RedisInstrumentor`)
  - Celery (`CeleryInstrumentor`)
- **Application tracer scope** -- all application code obtains its `Tracer`
  via `get_tracer()`, which fixes the instrumentation scope to
  `"zubot_ingestion"`.

### Span Hierarchy

The extraction pipeline produces the following span tree for every job:

```
zubot.extraction.batch                         (OTEL_SPAN_BATCH)
  └─ zubot.extraction.job                      (OTEL_SPAN_JOB)
       ├─ zubot.extraction.stage1.drawing_number  (OTEL_SPAN_STAGE1_DRAWING_NUMBER)
       ├─ zubot.extraction.stage1.title            (OTEL_SPAN_STAGE1_TITLE)
       ├─ zubot.extraction.stage1.document_type    (OTEL_SPAN_STAGE1_DOC_TYPE)
       ├─ zubot.extraction.stage2.companion        (OTEL_SPAN_STAGE2_COMPANION)
       ├─ zubot.extraction.stage3.sidecar          (OTEL_SPAN_STAGE3_SIDECAR)
       └─ zubot.confidence.calculate               (OTEL_SPAN_CONFIDENCE)
```

The span constants are defined in `src/zubot_ingestion/shared/constants.py`
(lines 174-181) and consumed by the `ExtractionOrchestrator` in
`src/zubot_ingestion/services/orchestrator.py`.

Each span records:

- **Timing** -- the span's own start/end timestamps give you per-stage latency.
- **Error attributes** -- if a stage raises, the span status is set to `ERROR`
  with a description capturing the exception.
- **Job attributes** (on the `zubot.extraction.job` span):
  - `file_hash`, `filename`, `job_id`, `batch_id`, `page_count`

The OTEL `trace_id` of the job span is formatted as a 32-character lowercase
hex string (W3C trace-context format) and stored in
`PipelineResult.otel_trace_id`. The Celery task persists this value to the
database so it can be returned via `GET /jobs/{id}` for linking to a trace UI.

### Configuration

| Variable | Description | Default |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | gRPC collector endpoint (e.g. `http://phoenix:4317`) | _(unset -- noop)_ |
| `OTEL_SERVICE_NAME` | Override `service.name` resource attribute | `zubot-ingestion` |

#### Local Development with Phoenix

The default `docker-compose.yml` does **not** bundle a trace collector. To
visualise traces locally, run [Phoenix](https://github.com/Arize-ai/phoenix)
(or any OTLP-compatible collector) externally and point the service at it:

```bash
# In a separate terminal
docker run -p 4317:4317 -p 6006:6006 arizephoenix/phoenix:latest

# Then start the service with the endpoint set
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
  python -m zubot_ingestion
```

Open `http://localhost:6006` to view traces.

---

## Prometheus Metrics

### Metric Inventory

All metrics are defined as module-level singletons in:

```
src/zubot_ingestion/infrastructure/metrics/prometheus.py
```

| Metric | Type | Labels | Buckets | Description |
|---|---|---|---|---|
| `zubot_extraction_total` | Counter | `status` | -- | Total extraction attempts (`completed`, `failed`, `review`) |
| `zubot_extraction_duration_seconds` | Histogram | -- | 1, 5, 10, 30, 60, 120, 300 s | Wall-clock duration of a full pipeline run |
| `zubot_confidence_score` | Histogram | `field` | 0, 0.2, 0.4, 0.5, 0.6, 0.8, 0.9, 1.0 | Per-field confidence score (`drawing_number`, `title`, `document_type`) |
| `zubot_queue_depth` | Gauge | -- | -- | Current Celery queue depth (active + reserved tasks) |
| `zubot_ollama_requests_total` | Counter | `model`, `status` | -- | Ollama API request count (`success`, `error`, `retry`) |
| `zubot_ollama_duration_seconds` | Histogram | `model` | 0.5, 1, 2, 5, 10, 30, 60 s | Ollama HTTP request latency |

Metric name constants are defined in `src/zubot_ingestion/shared/constants.py`
(lines 256-261, `METRIC_*` prefix).

### Scrape Endpoint

```
GET /metrics
```

Returns the standard Prometheus text exposition format. The endpoint is:

- **Auth-exempt** -- listed in `AUTH_EXEMPT_PATHS` so Prometheus can scrape
  without API credentials.
- **Rate-limit exempt** -- decorated with `@limiter.exempt` so scrape cadence
  is never throttled.

Implementation: `src/zubot_ingestion/api/routes/metrics.py`

### Where Metrics Are Recorded

| Metric | Recording site | Function / method |
|---|---|---|
| `zubot_extraction_total` | `services/orchestrator.py` | `record_extraction_status()` -- called once per pipeline run |
| `zubot_extraction_duration_seconds` | `services/orchestrator.py` | `record_extraction_duration()` -- called once per pipeline run |
| `zubot_confidence_score` | `services/orchestrator.py` | `record_field_confidences()` -- one observation per populated field |
| `zubot_queue_depth` | `api/routes/health.py` | `update_queue_depth_gauge()` -- refreshed on every `GET /health` response |
| `zubot_ollama_requests_total` | `infrastructure/ollama/client.py` | `record_ollama_request_status()` -- inside the `_post_generate` retry loop |
| `zubot_ollama_duration_seconds` | `infrastructure/ollama/client.py` | `time_ollama_call()` context manager wrapping `_post_generate` |

### Prometheus Scrape Configuration

Add a scrape job to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: zubot-ingestion
    scrape_interval: 15s
    static_configs:
      - targets: ["zubot-ingestion:8000"]
```

### Example PromQL Queries

```promql
# Extraction success rate over the last hour
rate(zubot_extraction_total{status="completed"}[1h])
/ rate(zubot_extraction_total[1h])

# P95 extraction latency
histogram_quantile(0.95, rate(zubot_extraction_duration_seconds_bucket[5m]))

# Ollama error rate by model
rate(zubot_ollama_requests_total{status="error"}[5m])

# Current queue depth
zubot_queue_depth
```

---

## Structured Logging

### Setup

Structured logging is configured by calling `setup_logging()` during the
FastAPI lifespan startup (and from Celery's `worker_process_init` signal).
The implementation lives in:

```
src/zubot_ingestion/infrastructure/logging/config.py
```

Key characteristics:

- **Library** -- [structlog](https://www.structlog.org/) with stdlib
  integration (`structlog.stdlib.LoggerFactory`).
- **Format** -- JSON by default (rendered by `structlog.processors.JSONRenderer`).
  Configurable via `LOG_FORMAT`.
- **Idempotent** -- calling `setup_logging()` twice replaces all root handlers,
  so it is safe to invoke from both FastAPI lifespan and Celery signal handlers
  in the same process.

### Output

When `LOG_DIR` is set, the service writes to daily-rotated log files:

| File | Rotation | Retention | Levels |
|---|---|---|---|
| `{LOG_DIR}/app.log` | Midnight daily | 7 backups | All (>= configured `LOG_LEVEL`) |
| `{LOG_DIR}/error.log` | Midnight daily | 7 backups | ERROR and above only |

A console handler is always attached regardless of `LOG_DIR`.

### Processor Chain

The structlog processor chain (in order):

1. `merge_contextvars` -- merges bound context variables into every log event
2. `add_log_level` -- adds the `level` key
3. `TimeStamper(fmt="iso")` -- adds ISO-8601 `timestamp`
4. `StackInfoRenderer` -- renders stack info if present
5. `format_exc_info` -- formats exception tracebacks
6. `scrub_sensitive_processor` -- redacts sensitive values (see below)
7. `JSONRenderer` -- serialises the event dict to a JSON string

### Sensitive Value Scrubbing

The `scrub_sensitive_processor` automatically redacts any log field whose key
(case-insensitive, with hyphens/dots/spaces normalised to underscores) contains
one of the following fragments:

```
api_key, apikey, jwt, token, password, secret,
file_bytes, file_contents, authorization
```

Matched values are replaced with `***REDACTED***`. The scrubber operates
recursively on nested dicts, lists, and tuples.

These fragments are defined in `SENSITIVE_KEY_FRAGMENTS` in
`src/zubot_ingestion/shared/constants.py` (line 295).

### Context Binding

Structured context is bound via `contextvars`, so every log line emitted on
the same async task automatically inherits the bound fields.

```python
from zubot_ingestion.infrastructure.logging.config import (
    bind_request_context,
    bind_job_context,
    clear_context,
)

# Per HTTP request (called by the request logging middleware)
bind_request_context(
    request_id="abc-123",
    user_id="user-456",
    method="POST",
    path="/submissions",
)

# Per pipeline execution (called by the Celery task)
bind_job_context(
    job_id="job-789",
    batch_id="batch-012",
    file_hash="sha256:...",
)

# At the end of the request/task
clear_context()
```

### Request Logging Middleware

The `RequestLoggingMiddleware` logs every HTTP request with:

- `method`, `path` -- from the incoming request
- `status_code` -- from the response
- `duration_ms` -- wall-clock time of the request

### Configuration

| Variable | Description | Default |
|---|---|---|
| `LOG_LEVEL` | Python log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) | `INFO` |
| `LOG_DIR` | Directory for rotated log files. When unset, only console output. | `/var/log/zubot-ingestion` |
| `LOG_FORMAT` | `json` or `text` | `json` |
| `LOG_RETENTION_DAYS` | Number of days to keep rotated log backups | `7` |

---

## End-to-End Correlation

The three observability pillars are tied together by shared identifiers that
let you trace a single extraction from HTTP request to pipeline result:

```
HTTP Request
  │
  │  1. Request arrives at FastAPI
  │     → request_id bound to structlog context
  │     → RequestLoggingMiddleware logs method, path, status, duration
  │
  ▼
Celery Task Dispatch
  │
  │  2. extract_document_task dispatched
  │     → job_id, batch_id, file_hash bound to structlog context
  │
  ▼
Pipeline Execution
  │
  │  3. ExtractionOrchestrator.run_pipeline runs
  │     → OTEL trace_id captured from the job span
  │     → Prometheus extraction_total, extraction_duration,
  │       confidence_score metrics recorded
  │     → PipelineResult.otel_trace_id populated
  │
  ▼
Result Retrieval
  │
  │  4. GET /jobs/{id} returns otel_trace_id
  │     → Use this to link directly to the trace in your trace UI
  │       (Phoenix, Jaeger, Grafana Tempo, etc.)
  │
  ▼
Log Search
     5. All structured logs include request_id, job_id, batch_id
        → grep / filter by any of these IDs to isolate a single
          request across all log files
```

### Practical Workflow

**Scenario**: A submission completes with an unexpectedly low confidence score.

1. **Start from the API** -- find the `job_id` from the `GET /jobs/{id}`
   response.

2. **Check the trace** -- use the `otel_trace_id` from the response to look up
   the trace in Phoenix / Jaeger. The span tree shows per-stage latencies and
   any error attributes.

3. **Check the metrics** -- query Prometheus for the confidence breakdown:
   ```promql
   zubot_confidence_score{field="drawing_number"}
   zubot_confidence_score{field="title"}
   zubot_confidence_score{field="document_type"}
   ```

4. **Check the logs** -- filter structured logs by `job_id`:
   ```bash
   jq 'select(.job_id == "job-789")' /var/log/zubot-ingestion/app.log
   ```
   Every log line from the pipeline execution will include `job_id`,
   `batch_id`, and `file_hash` for full context.
