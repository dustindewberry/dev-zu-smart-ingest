# Configuration Reference

All runtime configuration for `zubot_ingestion` is managed through environment
variables (or a `.env` file in development). The service reads them into a
single typed `Settings` instance via
[Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
at startup and exposes a process-wide cached singleton through `get_settings()`.

```python
from zubot_ingestion.config import get_settings

settings = get_settings()
print(settings.ZUBOT_PORT)       # 4243
print(settings.database_url)     # lowercase property alias
```

> **Tip:** Copy `.env.example` to `.env` and fill in your deployment values.
> Never commit `.env` to version control.

---

## HTTP Server

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `ZUBOT_HOST` | `str` | `0.0.0.0` | Network interface the Uvicorn server binds to. |
| `ZUBOT_PORT` | `int` | `4243` | TCP port the Uvicorn server listens on. |

---

## Database

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `DATABASE_URL` | `str` | `postgresql+asyncpg://zubot:zubot@localhost:5432/zubot_ingestion` | asyncpg-compatible PostgreSQL connection URL. For docker-compose use `postgresql+asyncpg://zubot:zubot@postgres:5432/zubot_ingestion`. |

The `Settings` class exposes a **lowercase property alias** `database_url`
(read-only) so downstream database-layer code can use either
`settings.DATABASE_URL` or `settings.database_url`.

The following connection-pool parameters are **not** `Settings` fields but are
defined as constants in `shared/constants.py` and used directly by the database
infrastructure layer:

| Constant | Type | Value | Description |
|----------|------|-------|-------------|
| `DB_POOL_SIZE` | `int` | `10` | SQLAlchemy async engine connection pool size. |
| `DB_MAX_OVERFLOW` | `int` | `20` | Maximum overflow connections above pool size. |
| `DB_POOL_TIMEOUT` | `int` | `30` | Seconds to wait for a connection from the pool before raising. |
| `DB_POOL_RECYCLE` | `int` | `3600` | Seconds after which a pooled connection is recycled (closed and re-opened). Prevents stale connections behind load balancers. |

---

## Redis / Celery

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REDIS_URL` | `str` | `redis://redis:6379` | Base Redis URL. Used for constructing Celery broker/result-backend URLs and rate-limit storage when per-DB URLs are not set explicitly. |
| `CELERY_BROKER_URL` | `str` | `redis://redis:6379/2` | Redis URL for the Celery task broker. Uses DB 2 by default (see `CELERY_BROKER_DB` constant). Set via `.env`; not a `Settings` field. |
| `CELERY_RESULT_BACKEND` | `str` | `redis://redis:6379/3` | Redis URL for Celery result storage. Uses DB 3 by default (see `CELERY_RESULT_DB` constant). Set via `.env`; not a `Settings` field. |
| `CELERY_WORKER_CONCURRENCY` | `int` | `2` | Number of concurrent Celery worker processes. Applied via `app.conf.update(...)`. Backed by `PERF_CELERY_WORKER_CONCURRENCY` constant. |
| `CELERY_WORKER_PREFETCH_MULTIPLIER` | `int` | `1` | Celery prefetch multiplier. Kept at 1 for long-running extraction tasks (no batching). Backed by `PERF_CELERY_WORKER_PREFETCH_MULTIPLIER` constant. |

**Lowercase property aliases:** `celery_worker_concurrency`,
`celery_worker_prefetch_multiplier`.

---

## Ollama

### zubot_ingestion Settings Fields

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `OLLAMA_HOST` | `str` | `http://ollama:11434` | Base URL of the Ollama inference server. |
| `OLLAMA_VISION_MODEL` | `str` | `qwen2.5vl:7b` | Default vision model tag passed to Ollama `/api/chat` requests. Backed by `PERF_OLLAMA_VISION_MODEL`. |
| `OLLAMA_TEXT_MODEL` | `str` | `qwen2.5:7b` | Default text model tag passed to Ollama `/api/chat` requests. Backed by `PERF_OLLAMA_TEXT_MODEL`. The T4 appliance overlay downgrades this to `qwen2.5:3b` to fit both models in 16 GB VRAM. |
| `OLLAMA_KEEP_ALIVE` | `str` | `5m` | Value forwarded as the top-level `keep_alive` field in the Ollama `/api/generate` request body. Controls how long Ollama keeps the model resident in VRAM after the last request. Backed by `PERF_OLLAMA_KEEP_ALIVE`. |
| `OLLAMA_REQUEST_TIMEOUT_SECONDS` | `float` | `120` | Alias used in `.env.example` for the overall HTTP request timeout to Ollama. Maps to `OLLAMA_HTTP_TIMEOUT_SECONDS` in the Settings model. |
| `OLLAMA_HTTP_POOL_MAX_CONNECTIONS` | `int` | `20` | Maximum number of connections in the `httpx.AsyncClient` connection pool used by the Ollama client. Backed by `PERF_OLLAMA_HTTP_POOL_MAX_CONNECTIONS`. |
| `OLLAMA_HTTP_POOL_MAX_KEEPALIVE` | `int` | `10` | Maximum number of keep-alive connections retained in the pool. Backed by `PERF_OLLAMA_HTTP_POOL_MAX_KEEPALIVE`. |
| `OLLAMA_HTTP_TIMEOUT_SECONDS` | `float` | `120.0` | Timeout in seconds for each HTTP request to Ollama. Backed by `PERF_OLLAMA_HTTP_TIMEOUT_SECONDS`. |
| `OLLAMA_RETRY_MAX_ATTEMPTS` | `int` | `3` | Total attempt count (1 initial + N-1 retries) before a failed Ollama request raises. Backed by `PERF_OLLAMA_RETRY_MAX_ATTEMPTS`. |
| `OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS` | `float` | `1.0` | Initial delay before the first retry. Subsequent delays grow exponentially: `delay(i) = initial * (multiplier ** i)`. Backed by `PERF_OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS`. |
| `OLLAMA_RETRY_BACKOFF_MULTIPLIER` | `float` | `2.0` | Exponential backoff multiplier applied to the retry delay on each successive attempt. Backed by `PERF_OLLAMA_RETRY_BACKOFF_MULTIPLIER`. |

**Lowercase property aliases:** `ollama_keep_alive`,
`ollama_http_pool_max_connections`, `ollama_http_pool_max_keepalive`,
`ollama_http_timeout_seconds`, `ollama_retry_max_attempts`,
`ollama_retry_initial_backoff_seconds`, `ollama_retry_backoff_multiplier`,
`ollama_vision_model`, `ollama_text_model`.

### Upstream Ollama Container Knobs

`OLLAMA_NUM_PARALLEL` is an **Ollama server-side** environment variable. It
controls how many inference requests the Ollama server processes concurrently
(parallel slots). This variable must be set on the **Ollama container itself**,
not on the `zubot-ingestion` container.

Setting `OLLAMA_NUM_PARALLEL` on the zubot container has **zero runtime
effect** — there is deliberately no corresponding `Settings` field in
`config.py` and no `PERF_OLLAMA_NUM_PARALLEL` constant in
`shared/constants.py`.

To configure parallel slots, set it on the Ollama service in your
`docker-compose.yml` (or overlay):

```yaml
services:
  ollama:
    environment:
      OLLAMA_NUM_PARALLEL: "2"    # allow 2 concurrent inference requests
```

The T4 appliance overlay (`docker-compose.t4.yml`) sets this on the Ollama
service — **not** on `zubot-ingestion-worker` or `zubot-ingestion`. The
overlay files carry explicit `NOTE` comments explaining this distinction.

---

## ChromaDB

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `CHROMADB_HOST` | `str` | `chromadb` | Hostname or IP of the ChromaDB metadata vector store. |
| `CHROMADB_PORT` | `int` | `8000` | Port the ChromaDB server listens on. Note: `.env.example` uses `8002`; the code default in `config.py` is `8000`. Use whichever matches your deployment. |

---

## Elasticsearch

All Elasticsearch settings are optional. When `ELASTICSEARCH_URL` is unset
(`None`), the search-indexer operates as a no-op so local development and CI
run without an Elasticsearch instance.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `ELASTICSEARCH_URL` | `str \| None` | `None` | Base URL of the Elasticsearch cluster (e.g. `http://elasticsearch:9200`). `None` disables the indexer. |
| `ELASTICSEARCH_USERNAME` | `str \| None` | `None` | Username for Elasticsearch basic auth. `None` means no auth. |
| `ELASTICSEARCH_PASSWORD` | `str \| None` | `None` | Password for Elasticsearch basic auth. `None` means no auth. |
| `ELASTICSEARCH_TIMEOUT` | `float` | `10.0` | Request timeout in seconds for Elasticsearch operations. |
| `ELASTICSEARCH_VERIFY_CERTS` | `bool` | `True` | Whether to verify TLS certificates when connecting to Elasticsearch. Set to `False` for self-signed certs in development. |

---

## Authentication

Both authentication secrets intentionally have **no meaningful defaults** and
default to empty strings. Startup should be gated on non-empty values in
production to fail fast when credentials are missing.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `ZUBOT_INGESTION_API_KEY` | `str` | `""` (empty) | Static API key for service-to-service `X-API-Key` header authentication. Also used as the outbound key for webhook callback deliveries. |
| `WOD_JWT_SECRET` | `str` | `""` (empty) | Shared HMAC secret used to verify WOD-issued JWT bearer tokens. |

---

## Rate Limiting

Rate limiting is implemented via [SlowAPI](https://github.com/laurentS/slowapi)
backed by Redis.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `RATE_LIMIT_DEFAULT` | `str` | `100/minute` | Global default rate limit applied to every endpoint that does not declare its own `@limiter.limit(...)` decorator. |
| `RATE_LIMIT_EXTRACT` | `str` | `20/minute` | Per-endpoint rate limit for `POST /extract`. Declared in `.env.example`; consumed by the extract route decorator. |
| `RATE_LIMIT_STORAGE_URL` | `str` | `redis://redis:6379/4` | Redis URL for the SlowAPI rate-limit storage backend. Uses DB 4 by default (see `RATE_LIMIT_REDIS_DB` constant). Declared in `.env.example`. |

---

## Logging

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `LOG_LEVEL` | `str` | `INFO` | Python log level. Valid values: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `LOG_DIR` | `str` | `/var/log/zubot-ingestion` | Directory for log file output. Must be writable by the service process. |
| `LOG_FORMAT` | `str` | `json` | Log output format. Declared in `.env.example`. Typically `json` (structured/machine-readable) or `console` (human-readable). |
| `LOG_RETENTION_DAYS` | `int` | `7` | Number of days to retain rotated log files. Declared in `.env.example`. |

> **Note:** `LOG_FORMAT` and `LOG_RETENTION_DAYS` appear in `.env.example`
> but are not declared as `Settings` fields in `config.py`. They are consumed
> by the structured-logging setup in `infrastructure/logging/`.

---

## OTEL / Observability

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `str` | `http://phoenix:4317` | gRPC endpoint for the OpenTelemetry OTLP exporter. Points to Arize Phoenix by default. |
| `OTEL_SERVICE_NAME` | `str` | `zubot-ingestion` | Service name reported in OTEL traces and resource attributes. Declared in `.env.example`; the constant `SERVICE_NAME` in `shared/constants.py` is `zubot-ingestion`. |

Additional OTEL variables in `.env.example`:

| Variable | Example Value | Description |
|----------|---------------|-------------|
| `OTEL_TRACES_EXPORTER` | `otlp` | Selects the trace exporter (standard OTEL SDK env var). |
| `OTEL_RESOURCE_ATTRIBUTES` | `service.name=zubot-ingestion,service.version=0.1.0` | Comma-separated resource attributes attached to every trace span (standard OTEL SDK env var). |

---

## Webhook Callbacks

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `CALLBACK_ENABLED` | `bool` | `False` | Master switch for outbound webhook delivery. When `False`, the composition root wires a `NoOpCallbackClient` so local dev / CI run without a live receiver. |
| `CALLBACK_SIGNING_SECRET` | `str \| None` | `None` | HMAC-SHA256 signing secret for outbound webhook deliveries. When non-empty, an `X-Zubot-Signature` header is attached to every callback POST. |

---

## Pipeline Tuning

| Variable | Type | Default | Source | Description |
|----------|------|---------|--------|-------------|
| `MAX_COMPANION_PAGES` | `int` | `4` | `shared/constants.py` | Maximum number of PDF pages sent to the Stage 2 companion vision model. Pages beyond this limit are skipped. |
| `CONFIDENCE_AUTO_THRESHOLD` | `float` | `0.8` | `shared/constants.py` (`CONFIDENCE_TIER_AUTO_MIN`) | Minimum composite confidence score for a job to be auto-accepted (tier `auto`). |
| `CONFIDENCE_REVIEW_THRESHOLD` | `float` | `0.5` | `shared/constants.py` (`CONFIDENCE_TIER_SPOT_MIN`) | Minimum composite confidence score for a job to be spot-checked (tier `spot`). Scores below this threshold land in the `review` tier. |
| `COMPANION_SKIP_ENABLED` | `bool` | `False` | `PERF_COMPANION_SKIP_ENABLED` | When enabled, Stage 2 skips companion generation for pages whose extracted text exceeds `COMPANION_SKIP_MIN_WORDS`. Disabled by default to preserve existing behavior. |
| `COMPANION_SKIP_MIN_WORDS` | `int` | `150` | `PERF_COMPANION_SKIP_MIN_WORDS` | Word-count threshold for the companion-skip heuristic. Pages with more extracted words than this value are considered text-dominant and skip the vision model call. |

**Lowercase property aliases:** `companion_skip_enabled`,
`companion_skip_min_words`.

> **Note:** `MAX_COMPANION_PAGES`, `CONFIDENCE_AUTO_THRESHOLD`, and
> `CONFIDENCE_REVIEW_THRESHOLD` are declared in `.env.example` and as
> constants in `shared/constants.py` but are **not** `Settings` fields. They
> are consumed directly by pipeline code via constant imports.

---

## PERF_* Constant Cross-Reference

Every performance-tuning `Settings` field sources its default from a
`PERF_*` constant in `shared/constants.py`. This table maps each Settings
field to its backing constant:

| Settings Field | PERF_* Constant | Default Value |
|----------------|-----------------|---------------|
| `OLLAMA_KEEP_ALIVE` | `PERF_OLLAMA_KEEP_ALIVE` | `5m` |
| `OLLAMA_VISION_MODEL` | `PERF_OLLAMA_VISION_MODEL` | `qwen2.5vl:7b` |
| `OLLAMA_TEXT_MODEL` | `PERF_OLLAMA_TEXT_MODEL` | `qwen2.5:7b` |
| `OLLAMA_HTTP_POOL_MAX_CONNECTIONS` | `PERF_OLLAMA_HTTP_POOL_MAX_CONNECTIONS` | `20` |
| `OLLAMA_HTTP_POOL_MAX_KEEPALIVE` | `PERF_OLLAMA_HTTP_POOL_MAX_KEEPALIVE` | `10` |
| `OLLAMA_HTTP_TIMEOUT_SECONDS` | `PERF_OLLAMA_HTTP_TIMEOUT_SECONDS` | `120.0` |
| `OLLAMA_RETRY_MAX_ATTEMPTS` | `PERF_OLLAMA_RETRY_MAX_ATTEMPTS` | `3` |
| `OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS` | `PERF_OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS` | `1.0` |
| `OLLAMA_RETRY_BACKOFF_MULTIPLIER` | `PERF_OLLAMA_RETRY_BACKOFF_MULTIPLIER` | `2.0` |
| `CELERY_WORKER_CONCURRENCY` | `PERF_CELERY_WORKER_CONCURRENCY` | `2` |
| `CELERY_WORKER_PREFETCH_MULTIPLIER` | `PERF_CELERY_WORKER_PREFETCH_MULTIPLIER` | `1` |
| `COMPANION_SKIP_ENABLED` | `PERF_COMPANION_SKIP_ENABLED` | `False` |
| `COMPANION_SKIP_MIN_WORDS` | `PERF_COMPANION_SKIP_MIN_WORDS` | `150` |

---

## T4 Appliance Overlay

The `docker-compose.t4.yml` overlay tunes the service for a single-box
"appliance" deployment on a **Tesla T4 GPU** (16 GB VRAM). Apply it on top of
the base stack:

```bash
docker compose -f docker-compose.yml -f docker-compose.t4.yml up
```

### What the overlay changes

| Variable | Base Default | T4 Override | Rationale |
|----------|-------------|-------------|-----------|
| `CELERY_WORKER_CONCURRENCY` | `2` | `4` | Saturate 2 Ollama parallel slots even when half the workers are idle on DB/IO. |
| `CELERY_WORKER_PREFETCH_MULTIPLIER` | `1` | `1` | Unchanged — long tasks, no batching. |
| `OLLAMA_KEEP_ALIVE` | `5m` | `24h` | Pin both models in VRAM so quiet periods don't trigger multi-second reloads. |
| `OLLAMA_TEXT_MODEL` | `qwen2.5:7b` | `qwen2.5:3b` | Downgrade text model so both models co-reside on the T4 with room for 2 parallel request slots and KV cache. |
| `OLLAMA_VISION_MODEL` | `qwen2.5vl:7b` | `qwen2.5vl:7b` | Unchanged — vision quality preserved at Q4_K_M quantization (~6 GB VRAM). |
| `COMPANION_SKIP_ENABLED` | `False` | `True` | Skip expensive Stage-2 companion calls for text-dominant pages on limited hardware. |
| `COMPANION_SKIP_MIN_WORDS` | `150` | `150` | Unchanged — pages with >= 150 words of extracted text rarely need vision enrichment. |
| `OLLAMA_HTTP_POOL_MAX_CONNECTIONS` | `20` | `20` | Unchanged — avoids connection churn under higher Celery concurrency. |
| `OLLAMA_HTTP_POOL_MAX_KEEPALIVE` | `10` | `10` | Unchanged — matches pool sizing. |

### T4 VRAM budget

- **Vision model:** `qwen2.5vl:7b` @ Q4_K_M — ~6 GB VRAM
- **Text model:** `qwen2.5:3b` — fits alongside the vision model with room for
  a 2-slot parallel request queue and KV cache within the T4's 16 GB limit
- **`OLLAMA_NUM_PARALLEL=2`** must be set on the **Ollama container** (not
  zubot-ingestion) to enable 2-at-a-time Stage 1 vision extractor overlap

### Removing the overlay

Removing `docker-compose.t4.yml` from the `-f` stack reverts all values to
their base defaults. No code changes required.
