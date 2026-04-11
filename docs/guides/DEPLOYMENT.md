# Deployment Guide

This guide covers how to build, deploy, and operate the `zubot_ingestion` service in various environments.

## Prerequisites

Before deploying, ensure you have:

- **Docker** and **Docker Compose** (v2+) installed
- **Python 3.12+** (for running migrations or the regression check locally)
- Access to the shared infrastructure services (see [Service Dependencies](#service-dependencies))
- A configured `.env` file (copy from `.env.example` and fill in real values)

## Docker Compose (Standard)

The standard deployment runs three containers defined in `docker-compose.yml`:

| Service | Container | Role |
|---------|-----------|------|
| `zubot-ingestion` | `zubot-ingestion` | FastAPI HTTP API (uvicorn) |
| `zubot-ingestion-worker` | `zubot-ingestion-worker` | Celery worker for async PDF extraction |
| `postgres` | `zubot-ingestion-postgres` | Dedicated PostgreSQL 15 instance |

### Shared Infrastructure

The compose stack is **standalone** -- it only defines the three services above. The following shared infrastructure must be running externally (e.g. from an existing `ai-chatbot` stack or standalone containers):

- **Ollama** -- LLM inference (vision + text models)
- **Redis** -- Celery broker, result backend, and rate limiting
- **ChromaDB** -- Metadata vector store
- **Elasticsearch** -- Companion document search index
- **Phoenix** -- OpenTelemetry trace collector

These services are reached via `host.docker.internal`, which is mapped to the host gateway using the `extra_hosts` directive in compose. The environment variables in `docker-compose.yml` point to `host.docker.internal` by default:

```yaml
OLLAMA_HOST: http://host.docker.internal:11434
REDIS_URL: redis://host.docker.internal:6379
CHROMADB_HOST: host.docker.internal
CHROMADB_PORT: "8000"
ELASTICSEARCH_URL: http://host.docker.internal:9200
OTEL_EXPORTER_OTLP_ENDPOINT: http://host.docker.internal:4317
```

### Networking and Ports

| Port | Service | Notes |
|------|---------|-------|
| `4243` | zubot-ingestion API | Main HTTP API endpoint |
| `5433` | PostgreSQL | Mapped to host `5433` to avoid conflict with any existing PostgreSQL on `5432` |

### Volumes

- `zubot_postgres_data` -- Named volume for PostgreSQL data persistence. Survives container restarts and `docker compose down`. Use `docker compose down -v` to destroy it.
- `./logs:/var/log/zubot-ingestion` -- Bind mount for structured log files from both the API and worker containers.

### Environment

All service configuration is loaded from a `.env` file at the project root. Copy `.env.example` to `.env` and adjust values for your environment:

```bash
cp .env.example .env
# Edit .env with your real credentials and connection strings
```

The compose file also sets environment variables directly (e.g. `DATABASE_URL` pointing to the internal `postgres` service). These override any matching entries in `.env`.

### Build and Run

```bash
# Build images and start all services in detached mode
docker compose up --build -d

# View logs for the API service
docker compose logs -f zubot-ingestion

# View logs for the Celery worker
docker compose logs -f zubot-ingestion-worker

# Stop all services (data preserved)
docker compose down

# Stop all services and destroy volumes (data lost)
docker compose down -v
```

### Worker Configuration

Celery concurrency and prefetch are **not** passed as CLI flags. They are resolved from environment variables (`CELERY_WORKER_CONCURRENCY`, `CELERY_WORKER_PREFETCH_MULTIPLIER`) and applied to `app.conf` at boot inside `zubot_ingestion.services.celery_app`. This allows operators to override them via a compose overlay without editing the command array.

Default values in the base compose file:

| Variable | Default | Description |
|----------|---------|-------------|
| `CELERY_WORKER_CONCURRENCY` | `2` | Number of concurrent Celery worker processes |
| `CELERY_WORKER_PREFETCH_MULTIPLIER` | `1` | Tasks prefetched per worker (1 = no prefetch, appropriate for long-running tasks) |

---

## T4 Appliance Overlay

The `docker-compose.t4.yml` overlay tunes the deployment for a **Tesla T4 GPU** (16 GB VRAM) appliance that co-hosts both vision and text models on a single GPU.

### Purpose

On a T4 with 16 GB VRAM, both the vision model (~6 GB at Q4_K_M quantization) and the text model must fit simultaneously. The overlay downgrades the text model from `qwen2.5:7b` to `qwen2.5:3b` to leave room for the KV cache and a 2-slot parallel request queue.

### Deploy Command

```bash
docker compose -f docker-compose.yml -f docker-compose.t4.yml up --build -d
```

### Key Overrides

| Variable | Base Value | T4 Value | Rationale |
|----------|-----------|----------|-----------|
| `OLLAMA_TEXT_MODEL` | `qwen2.5:7b` | `qwen2.5:3b` | Fits both models in 16 GB VRAM |
| `OLLAMA_VISION_MODEL` | `qwen2.5vl:7b` | `qwen2.5vl:7b` | Unchanged -- no quality regression beyond quantization |
| `CELERY_WORKER_CONCURRENCY` | `2` | `4` | Saturates the 2 Ollama parallel slots even when half the workers are idle on DB/IO |
| `OLLAMA_KEEP_ALIVE` | default | `24h` | Keeps models hot in VRAM; avoids multi-second reload on the next request after idle |
| `COMPANION_SKIP_ENABLED` | `false` | `true` | Enables the companion-page skip heuristic |
| `COMPANION_SKIP_MIN_WORDS` | `150` | `150` | Pages with >= 150 words of extracted text skip vision enrichment (reduces Stage-2 API calls) |
| `OLLAMA_HTTP_POOL_MAX_CONNECTIONS` | default | `20` | Avoids connection churn under higher Celery concurrency |
| `OLLAMA_HTTP_POOL_MAX_KEEPALIVE` | default | `10` | Maintains warm keepalive connections to Ollama |

### Upstream Ollama Server Tuning

The following Ollama env vars must be set on the **Ollama container itself**, not on the zubot-ingestion services. They have no effect when set on the Python client:

| Variable | Recommended T4 Value | Description |
|----------|---------------------|-------------|
| `OLLAMA_NUM_PARALLEL` | `2` | Allows overlapping Stage-1 vision extractor requests instead of serializing |

### Regression Check Before Model Downgrade

Before deploying a text model downgrade (e.g. `7b` to `3b`), run the regression check to ensure extraction quality remains acceptable:

```bash
python -m scripts.regression_check --tolerance 0.85
```

This script compares extraction results between the current and candidate models. A tolerance of `0.85` means the downgraded model must produce results that are at least 85% similar to the baseline. If the check fails, the model downgrade should not proceed.

---

## Dockerfile

The service uses a multi-stage Dockerfile optimized for small image size and security.

### Stage 1: Builder

- **Base image:** `python:3.12-slim`
- Installs build-time system packages (gcc, libpq-dev, etc.) needed to compile native Python wheels (asyncpg, pymupdf, lxml, pillow)
- Creates an isolated virtualenv at `/opt/venv`
- Installs Python dependencies from `requirements.txt` into the venv
- This layer is cached when only source code changes

### Stage 2: Runtime

- **Base image:** `python:3.12-slim`
- Copies the pre-built `/opt/venv` from the builder stage (no compilers in the final image)
- Installs only runtime shared libraries (libpq5, libxml2, curl for healthcheck)
- Creates a **non-root user** `zubot` (uid 1000, gid 1000)
- Copies only `src/` into `/app/src/` (tests, docs excluded via `.dockerignore`)
- Runs as the non-root user

### Runtime Details

| Property | Value |
|----------|-------|
| Exposed port | `4243` |
| Healthcheck | `curl -fsS http://localhost:4243/health` (30s interval, 5s timeout, 3 retries, 30s start period) |
| Entrypoint | `uvicorn zubot_ingestion.api.app:create_app --factory --host 0.0.0.0 --port 4243` |
| PYTHONPATH | `/app/src` |
| User | `zubot` (uid 1000) |

---

## Database Migrations

The project uses **Alembic** for schema migrations, configured for async SQLAlchemy.

### Configuration

- `alembic.ini` -- Main configuration file. The `sqlalchemy.url` is overridden at runtime by `migrations/env.py` to use the value from application settings (`get_settings().database_url`), so you do not need to edit the `.ini` file.
- `migrations/env.py` -- Async-aware environment that uses `async_engine_from_config` with `NullPool`.
- `migrations/versions/` -- Contains migration scripts (initial migration: `0001_initial.py`).
- Target metadata is sourced from `src/zubot_ingestion/infrastructure/database/models.py` (`Base.metadata`).

### Running Migrations

```bash
# Ensure DATABASE_URL is set in your environment or .env file

# Apply all pending migrations (run from the project root)
alembic upgrade head

# Generate a new migration after model changes
alembic revision --autogenerate -m "description of changes"

# View current migration state
alembic current

# View migration history
alembic history

# Generate SQL without applying (offline mode)
alembic upgrade head --sql
```

### Migrations in Docker

When running inside Docker, the database connection points to the internal `postgres` service. You can run migrations by exec-ing into the API container:

```bash
docker compose exec zubot-ingestion alembic upgrade head
```

Or run them from the host against the exposed PostgreSQL port (5433):

```bash
DATABASE_URL=postgresql+asyncpg://zubot:zubot_dev@localhost:5433/zubot_ingestion alembic upgrade head
```

---

## Service Dependencies

The `zubot_ingestion` service depends on several external services. Some are critical (the service cannot function without them), while others degrade gracefully when unavailable.

| Service | Default URL | Purpose | Required? |
|---------|------------|---------|-----------|
| PostgreSQL | `postgresql+asyncpg://zubot:zubot_dev@postgres:5432/zubot_ingestion` | Job and batch persistence | Yes |
| Redis | `redis://redis:6379/2` (broker), `/3` (results), `/4` (rate limits) | Celery task queue, result backend, rate limiting | Yes |
| Ollama | `http://ollama:11434` | LLM inference for vision + text extraction | Yes |
| ChromaDB | `chromadb:8002` | Metadata vector store | No (graceful degradation) |
| Elasticsearch | `http://elasticsearch:9200` | Companion document full-text search index | No (NoOp fallback) |
| Phoenix/OTEL | `http://phoenix:4317` | Distributed tracing (OpenTelemetry) | No (noop tracer provider) |

### Redis Database Allocation

Redis uses separate databases to isolate concerns:

| Database | Purpose |
|----------|---------|
| `/2` | Celery broker (task dispatch) |
| `/3` | Celery result backend (task results) |
| `/4` | slowapi rate limit storage |

### Authentication

Two authentication mechanisms are supported:

| Mechanism | Env Var | Description |
|-----------|---------|-------------|
| API Key | `ZUBOT_INGESTION_API_KEY` | Static key for service-to-service calls (`X-API-Key` header) |
| JWT | `WOD_JWT_SECRET` / `WOD_JWT_ALGORITHM` | WOD-issued JWT bearer tokens |

---

## Health Monitoring

### Health Check Endpoint

`GET /health` returns a comprehensive status report for every external dependency.

**Response structure:**

```json
{
  "status": "healthy",
  "service": "zubot-ingestion",
  "version": "0.1.0",
  "dependencies": {
    "postgres": { "status": "healthy", "latency_ms": 2, "error": null },
    "redis": { "status": "healthy", "latency_ms": 1, "error": null },
    "ollama": { "status": "healthy", "latency_ms": 45, "error": null },
    "chromadb": { "status": "healthy", "latency_ms": 12, "error": null },
    "elasticsearch": { "status": "healthy", "latency_ms": 8, "error": null }
  },
  "workers": {
    "active": 2,
    "queue_depth": 3
  }
}
```

### Status Aggregation Rules

| Overall Status | Condition | HTTP Code |
|---------------|-----------|-----------|
| `healthy` | All critical AND optional deps are healthy | 200 |
| `degraded` | All critical deps healthy, at least one optional dep unhealthy | 200 |
| `unhealthy` | At least one critical dep is unhealthy | 503 |

**Critical dependencies** (service is `unhealthy` if any are down):
- PostgreSQL
- Redis

**Optional dependencies** (service is `degraded` if any are down):
- Ollama
- ChromaDB
- Elasticsearch
- Celery workers

All probes run concurrently with a per-probe timeout of 5 seconds. A hung dependency cannot stall the response.

The health endpoint is exempt from both authentication and rate limiting, so Kubernetes liveness/readiness probes and monitoring systems can poll it without credentials.

### Prometheus Metrics

`GET /metrics` exposes Prometheus-compatible metrics for scraping. Key metrics include:

- **`zubot_queue_depth`** -- Current Celery queue depth (refreshed on every `/health` call)
- Extraction duration, field confidence scores, and extraction status counters

Configure your Prometheus instance to scrape:

```yaml
scrape_configs:
  - job_name: zubot-ingestion
    scrape_interval: 15s
    static_configs:
      - targets: ["zubot-ingestion:4243"]
    metrics_path: /metrics
```

### Docker Healthcheck

Both the Dockerfile and `docker-compose.yml` define a healthcheck that hits `GET /health`:

```
curl -fsS http://localhost:4243/health
```

- **Interval:** 30s
- **Timeout:** 5s
- **Start period:** 30s (grace period for startup)
- **Retries:** 3

Docker will mark the container as `unhealthy` after 3 consecutive failures, which integrates with orchestration tools (Docker Swarm, Kubernetes via container health probes, etc.).
