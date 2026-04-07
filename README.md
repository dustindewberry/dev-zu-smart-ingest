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
