# Developer Quickstart

Get the `zubot_ingestion` service running locally for development. This guide covers both Docker Compose and native Python setups.

## Prerequisites

Before you begin, ensure you have:

- **Python 3.12+**
- **Docker & Docker Compose**
- **Access to shared infrastructure services:**
  - PostgreSQL (provided by the compose stack, or your own instance)
  - Redis
  - Ollama with the following models pulled:
    - `qwen2.5vl:7b` (vision model)
    - `qwen2.5:7b` (text model)
  - ChromaDB
  - Elasticsearch
- **GCP credentials** (optional) — only needed if using Google Document AI for PDF extraction
- A copy of `.env.example` renamed to `.env` with secrets filled in (see below)

### Required Credentials & Secrets

You will need to configure these values in your `.env` file:

| Variable | Purpose |
|---|---|
| `ZUBOT_INGESTION_API_KEY` | Static API key used for service-to-service authentication. Sent via the `X-API-Key` header. |
| `WOD_JWT_SECRET` | Shared secret used to verify WOD-issued JWT bearer tokens. Must match the signing secret used by the issuing service. |
| `DATABASE_URL` | PostgreSQL connection string in asyncpg format, e.g. `postgresql+asyncpg://user:pass@host:5432/zubot_ingestion`. |

The `.env.example` file contains placeholder values (`replace-me-with-...`) for `ZUBOT_INGESTION_API_KEY` and `WOD_JWT_SECRET`. Replace them with strong, random secrets before running the service.

---

## Docker Compose Setup

The `docker-compose.yml` defines three services:

| Service | Description | Port |
|---|---|---|
| `zubot-ingestion` | FastAPI application server | `4243` (host) |
| `zubot-ingestion-worker` | Celery worker for async PDF extraction tasks | — |
| `postgres` | PostgreSQL 15 database | `5433` (host) → `5432` (container) |

> **Note:** Shared infrastructure (Ollama, Redis, ChromaDB, Elasticsearch, Phoenix/OTEL) is **not** included in this compose file. These services are expected to be running externally and are reached via `host.docker.internal` networking. The compose file sets `extra_hosts: ["host.docker.internal:host-gateway"]` on both application services to enable this.

### Start the stack

```bash
# Copy and configure your environment
cp .env.example .env
# Edit .env — fill in ZUBOT_INGESTION_API_KEY, WOD_JWT_SECRET, etc.

# Build and start all services
docker compose up --build -d
```

### Verify it's running

```bash
curl http://localhost:4243/health
```

You should get a JSON response with dependency health checks for postgres, redis, ollama, chromadb, elasticsearch, and celery.

### T4 Appliance Overlay

For deployment on a Tesla T4 GPU appliance, use the `docker-compose.t4.yml` overlay. This overlay tunes the worker and inference settings for a single-box T4 deployment (16 GB VRAM):

- Drops the text model to `qwen2.5:3b` so both models fit in VRAM
- Increases Celery concurrency from 2 to 4
- Enables companion-skip for text-heavy pages (>= 150 words)
- Pins `OLLAMA_KEEP_ALIVE=24h` to avoid model reload latency
- Widens the HTTP connection pool for higher concurrency

```bash
docker compose -f docker-compose.yml -f docker-compose.t4.yml up --build -d
```

All values in the overlay are pure environment variable overrides — the base stack structure (services, networks, volumes, healthchecks, ports) is reused as-is. Removing the overlay file reverts to default behaviour.

---

## Native Python Setup

If you prefer running the service outside of Docker:

### 1. Create a virtualenv and install dependencies

```bash
python -m venv .venv
source .venv/bin/activate

pip install -e '.[dev]'
```

The `dev` extras group (defined in `pyproject.toml`) includes pytest, mypy, ruff, import-linter, and pre-commit.

### 2. Configure environment variables

```bash
cp .env.example .env
# Edit .env — set DATABASE_URL to point at your local Postgres, etc.
```

When running natively, `DATABASE_URL` should point to your local PostgreSQL instance (e.g. `postgresql+asyncpg://zubot:zubot_dev@localhost:5433/zubot_ingestion` if using the compose Postgres on port 5433). Likewise, update `REDIS_URL`, `OLLAMA_HOST`, `CHROMADB_HOST`, and `ELASTICSEARCH_URL` to match your local setup.

### 3. Run the API server

```bash
python -m zubot_ingestion
```

This invokes `src/zubot_ingestion/__main__.py`, which reads `ZUBOT_HOST` and `ZUBOT_PORT` from Settings and launches the FastAPI app under uvicorn. By default the server binds to `0.0.0.0:4243`.

### 4. Run the Celery worker (separate terminal)

```bash
celery -A zubot_ingestion.services.celery_app:celery_app worker \
  --loglevel=info \
  --concurrency=2 \
  --prefetch-multiplier=1
```

> **Note:** In the Docker Compose setup, concurrency and prefetch multiplier are resolved from Settings environment variables (`CELERY_WORKER_CONCURRENCY`, `CELERY_WORKER_PREFETCH_MULTIPLIER`) and applied at boot via `app.conf`, so they are not passed as CLI flags. When running natively, you can either set those env vars or pass them as CLI flags as shown above.

### 5. Verify

```bash
curl http://localhost:4243/health
```

---

## Verification

Once the service is running (via either method), here is how to verify everything is working:

### Health endpoint

```bash
curl http://localhost:4243/health
```

The response includes dependency health checks for: **postgres**, **redis**, **ollama**, **chromadb**, **elasticsearch**, and **celery**. A healthy response confirms all infrastructure connections are live.

### Interactive API docs

FastAPI auto-generates interactive documentation:

- **Swagger UI:** [http://localhost:4243/docs](http://localhost:4243/docs)
- **ReDoc:** [http://localhost:4243/redoc](http://localhost:4243/redoc)

### Submit a test PDF

```bash
curl -X POST http://localhost:4243/extract \
  -H 'X-API-Key: <your-api-key>' \
  -F 'files=@test.pdf'
```

Replace `<your-api-key>` with the value of `ZUBOT_INGESTION_API_KEY` from your `.env` file.

---

## Running Tests

### Unit tests

```bash
python -m pytest tests/unit/
```

### Integration tests

Integration tests require running infrastructure (Postgres, Redis, Ollama, ChromaDB, Elasticsearch):

```bash
python -m pytest tests/ -m integration
```

### Notes on the test setup

- **Async tests run automatically.** `pytest.ini` (and `pyproject.toml`) set `asyncio_mode = auto`, so async test functions run without needing explicit `@pytest.mark.asyncio` markers.

- **Hermetic environment.** `tests/conftest.py` sets safe environment variables **before** any test module is collected, preventing module-level singletons from attempting real connections during test discovery:

  | Variable | Test Value | Why |
  |---|---|---|
  | `LOG_DIR` | A temporary directory | Avoids `PermissionError` writing to `/var/log/zubot-ingestion` |
  | `REDIS_URL` | `memory://` | Prevents slowapi Limiter from connecting to real Redis |
  | `ZUBOT_INGESTION_API_KEY` | `test-api-key` | Keeps auth middleware happy without a real secret |
  | `OTEL_EXPORTER_OTLP_ENDPOINT` | `""` (empty) | Installs a noop OTEL provider instead of attempting gRPC connection |
