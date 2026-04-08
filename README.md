# dev-zu-smart-ingest

`zubot-ingestion` — FastAPI + Celery service that extracts structured
metadata (drawing number, title, document type, …) from construction
PDFs and writes the results to ChromaDB and Elasticsearch.

## Container layout (CAP-002)

The repo ships a multi-stage `Dockerfile` and a standalone
`docker-compose.yml` that brings up three services:

| Service                  | Purpose                                | Host port |
|--------------------------|----------------------------------------|-----------|
| `zubot-ingestion`        | FastAPI API (uvicorn, `--factory`)     | `4243`    |
| `zubot-ingestion-worker` | Celery worker (`--concurrency=2`)      | —         |
| `postgres`               | Postgres 15 — job + batch metadata     | `5433`    |

The Postgres host port is intentionally `5433` to avoid colliding with
the ai-chatbot Postgres on `5432`.

## Shared infrastructure (reused from `ai-chatbot`)

This stack is **standalone**. The heavy backing services
(Ollama, Redis, ChromaDB, Elasticsearch, Phoenix/OTel collector) are
**not** defined here — they are reused from the running ai-chatbot
stack at `/Users/dustindewberry/gitrepos/ai-chatbot`. The
`zubot-ingestion` and `zubot-ingestion-worker` containers reach those
services over the host network via `host.docker.internal`, which is
mapped to `host-gateway` through the `extra_hosts` directive in
`docker-compose.yml`.

The relevant defaults baked into the compose file are:

| Dependency       | URL                                       | Provided by      |
|------------------|-------------------------------------------|------------------|
| Ollama           | `http://host.docker.internal:11434`       | ai-chatbot       |
| Redis            | `redis://host.docker.internal:6379`       | ai-chatbot       |
| ChromaDB         | `host.docker.internal:8000`               | ai-chatbot       |
| Elasticsearch    | `http://host.docker.internal:9200`        | ai-chatbot       |
| OTel collector   | `http://host.docker.internal:4317`        | ai-chatbot Phoenix |
| Postgres         | `postgres:5432` (in-stack)                | this compose file |

Make sure the ai-chatbot stack is running and that those host ports are
published before you `docker compose up` this service:

```bash
cd /Users/dustindewberry/gitrepos/ai-chatbot
docker compose up -d ollama redis chromadb elasticsearch
```

Then start `zubot-ingestion`:

```bash
cd <this-repo>
cp .env.example .env   # then edit secrets
docker compose up -d
curl http://localhost:4243/health
```

## Joining the ai-chatbot Docker network (optional)

If you would rather have `zubot-ingestion` reach the ai-chatbot
services over Docker's internal network instead of going through the
host loopback, you can attach both stacks to the same user-defined
bridge network. By default the ai-chatbot compose project uses the
network `ai-chatbot_default` (Compose auto-creates `<project>_default`
when no explicit `networks:` block is declared).

To join it, append the following to `docker-compose.yml`:

```yaml
networks:
  default:
    name: ai-chatbot_default
    external: true
```

After joining the shared network, replace the `host.docker.internal`
URLs in the `environment:` blocks with the ai-chatbot service names:

| Variable                       | Value on shared network             |
|--------------------------------|-------------------------------------|
| `OLLAMA_HOST`                  | `http://ollama:11434`               |
| `REDIS_URL`                    | `redis://redis:6379`                |
| `CHROMADB_HOST`                | `chromadb` (port `8000`)            |
| `ELASTICSEARCH_URL`            | `http://elasticsearch:9200`         |
| `OTEL_EXPORTER_OTLP_ENDPOINT`  | `http://phoenix:4317` (if present)  |

You can keep the `extra_hosts` block in place — it is harmless on a
shared network — or remove it for cleanliness.

> ⚠️ The shared-network mode requires that the ai-chatbot stack has
> been started at least once so that `ai-chatbot_default` exists. If
> Docker reports `network ai-chatbot_default not found`, run
> `docker compose up -d` in the ai-chatbot repo first.

## Rate Limiting (CAP-030)

The HTTP API is rate limited with
[`slowapi`](https://pypi.org/project/slowapi/) (a Starlette/FastAPI wrapper
around the `limits` library) backed by Redis so that all uvicorn workers
share the same counters.

### Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `REDIS_URL` | `redis://redis:6379` | Base Redis URL. The rate-limit storage URI is composed as `{REDIS_URL}/{RATE_LIMIT_REDIS_DB}`. |
| `RATE_LIMIT_REDIS_DB` | `4` *(constant)* | Redis logical database used exclusively for rate-limit windows. Distinct from Celery broker (DB 2) and Celery result backend (DB 3) so a queue purge cannot reset rate-limit state. |
| `RATE_LIMIT_DEFAULT` | `100/minute` | Global default limit applied to any endpoint that does not declare its own `@limiter.limit(...)` decorator. Override via environment variable. |

### Per-endpoint limits

| Endpoint | Method | Limit | Rationale |
| --- | --- | --- | --- |
| `/extract` | `POST` | `20/minute` | Expensive: multipart upload, Celery enqueue, downstream Ollama vision inference. |
| `/batches/{batch_id}` | `GET` | `100/minute` | Cheap read against PostgreSQL. |
| `/jobs/{job_id}` | `GET` | `100/minute` | Cheap read against PostgreSQL. |
| `/review/pending` | `GET` | `100/minute` | Cheap paginated read. |
| `/review/{job_id}/approve` | `POST` | `100/minute` | Inherits the default limit; approvals are low volume in practice. |
| `/review/{job_id}/reject` | `POST` | `100/minute` | Inherits the default limit. |
| `/health` | `GET` | *unlimited* (exempt) | Polled by Kubernetes liveness/readiness probes. Throttling would manufacture self-inflicted outages. |
| `/metrics` | `GET` | *unlimited* (exempt) | Scraped by Prometheus on a fixed cadence. Throttling would produce scrape gaps and break alerting / historical continuity. |
| `/docs`, `/openapi.json`, `/redoc` | `GET` | *unlimited* (exempt) | Interactive API exploration should never be throttled. |

Exemptions are enforced by the `@limiter.exempt` decorator on the handler
functions, which registers them in `slowapi.Limiter._exempt_routes` so that
even the global default limit is bypassed.

### Keying strategy

The bucket key is chosen per-request by
`zubot_ingestion.api.middleware.rate_limit.get_rate_limit_key`:

1. **If the request is authenticated** (`request.state.auth_context` was
   populated by `AuthMiddleware` — either an API-key caller or a valid WOD
   JWT bearer token), the key is `user:<user_id>`. This ensures two clients
   sharing an egress NAT cannot starve each other.
2. **Otherwise** (unauthenticated routes, or an early-middleware failure),
   the key is `ip:<remote_address>` where the remote address is resolved
   by `slowapi.util.get_remote_address` (honours `X-Forwarded-For` when
   running behind a trusted reverse proxy).

The two namespaces (`user:` vs `ip:`) are kept distinct so a user whose
`user_id` happens to match an IP literal cannot collide with an IP-keyed
bucket.

### Response shape when limited

On a 429 the middleware returns:

```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/json
X-RateLimit-Limit: 20
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1712505600
Retry-After: 42

{"detail": "Rate limit exceeded", "retry_after": 42}
```

* `X-RateLimit-Limit` — total requests allowed in the current window.
* `X-RateLimit-Remaining` — always `0` on a 429.
* `X-RateLimit-Reset` — epoch-seconds timestamp when the window resets.
* `Retry-After` — delta-seconds the client should wait before retrying
  (mirrored in the JSON body as `retry_after` for convenience).

### Operational notes

* **Storage isolation.** The rate-limit Redis DB (`4`) is separate from
  Celery's broker DB (`2`) and result-backend DB (`3`), so flushing Celery
  state (`celery -A ... purge`) leaves rate-limit counters intact.
* **Shared state.** Because the storage is Redis rather than in-process
  memory, all uvicorn workers and all API replicas share the same counters.
  Horizontal scaling of the API does not weaken rate limiting.
* **Running without Redis.** For local development without Redis, override
  the storage URI to `memory://` by instantiating a fresh `Limiter` in the
  FastAPI factory. This is NOT safe in production because counters are
  per-process.
* **Tuning.** To change the global default at runtime set
  `RATE_LIMIT_DEFAULT` in the service's `.env` (e.g.
  `RATE_LIMIT_DEFAULT=500/minute`). Per-endpoint limits are hard-coded in
  the route decorators and require a code change and redeploy.
