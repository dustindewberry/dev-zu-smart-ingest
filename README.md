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

## Performance Tuning

This service is CPU-only in the Python layer; all heavy inference is
delegated over HTTP to an external [Ollama](https://ollama.com/) server
(`qwen2.5vl:7b` vision + a `qwen2.5:3b`-class text model by default).
"Performance tuning" therefore splits into two concerns: (1) making this
service feed Ollama as hard as the hardware allows, and (2) picking the
right Ollama model loadout and runtime flags for the GPU you have. Both
are driven entirely by environment variables so the same image can scale
from a single Tesla T4 appliance up to a dedicated multi-GPU inference
node without a code change.

### Benchmark harness

A standalone benchmark harness lives at `scripts/bench.py`. It runs a
corpus of PDFs through the real `ExtractionOrchestrator.run_pipeline()`
via `build_orchestrator()` (so every env var listed below is exercised
end to end) and emits a stable JSON report with per-document stage
timings plus aggregate p50/p95/p99 latency, throughput, and failure
rate.

```bash
python scripts/bench.py --warmup 2 --iterations 10
```

The harness auto-discovers fixture PDFs under `tests/fixtures/`,
`tests/data/`, and `tests/**` when no `--corpus` path is provided. See
[`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) for the full CLI reference,
JSON report schema, recommended warmup counts, and operator playbook
for A/B'ing tuning changes.

Every tuning change described below should be measured by running the
benchmark before and after the change against the same corpus, at the
same concurrency, on the same host.

### Tunable environment variables

All of the following are plain environment variables bound to the
`Settings` object in `src/zubot_ingestion/config.py`. Defaults preserve
the pre-tuning behavior; overriding them is how you scale the service
up when hardware improves.

| Variable | Default | Type | Description |
| --- | --- | --- | --- |
| `OLLAMA_NUM_PARALLEL` | `1` | `int` | Number of concurrent generation slots the Ollama server is expected to service. Must match the Ollama runtime's own `OLLAMA_NUM_PARALLEL`. |
| `OLLAMA_KEEP_ALIVE` | `5m` | `str` | How long Ollama should keep models resident in VRAM between calls (e.g. `24h`). Longer keep-alive avoids cold-load swap thrash. |
| `OLLAMA_HTTP_POOL_MAX_CONNECTIONS` | `20` | `int` | Upper bound on total concurrent HTTP connections in the shared `httpx.AsyncClient` pool used by `OllamaClient`. |
| `OLLAMA_HTTP_POOL_MAX_KEEPALIVE` | `10` | `int` | Idle HTTP connections kept warm in the pool for reuse. Should be ≤ `OLLAMA_HTTP_POOL_MAX_CONNECTIONS`. |
| `OLLAMA_HTTP_TIMEOUT_SECONDS` | `120` | `float` | Per-request total timeout (connect + read + write) for Ollama HTTP calls. |
| `OLLAMA_RETRY_MAX_ATTEMPTS` | `3` | `int` | Maximum retry attempts (including the initial try) for a single Ollama call before failing the stage. |
| `OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS` | `1.0` | `float` | Backoff delay before the first retry. Subsequent retries multiply this by `OLLAMA_RETRY_BACKOFF_MULTIPLIER`. |
| `OLLAMA_RETRY_BACKOFF_MULTIPLIER` | `2.0` | `float` | Exponential backoff multiplier applied between retry attempts. |
| `OLLAMA_VISION_MODEL` | `qwen2.5vl:7b` | `str` | Ollama model tag used for all Stage 1 / Stage 2 vision calls. |
| `OLLAMA_TEXT_MODEL` | `qwen2.5:3b` | `str` | Ollama model tag used for text-only extraction. See the regression-check ladder below before changing. |
| `CELERY_WORKER_CONCURRENCY` | `2` | `int` | Number of Celery worker child processes per container. Primary knob for throughput scale-up. |
| `CELERY_WORKER_PREFETCH_MULTIPLIER` | `1` | `int` | Celery prefetch multiplier. Keep at `1` for long-running inference tasks to avoid head-of-line blocking. |
| `COMPANION_SKIP_ENABLED` | `false` | `bool` | Feature flag for the Stage 2 companion-page skip heuristic. |
| `COMPANION_SKIP_MIN_WORDS` | `150` | `int` | When the heuristic is enabled, pages whose extracted text-layer word count is ≥ this value skip the companion vision call. |

### Single-box T4 appliance overlay

For the baseline single-box deployment on a Tesla T4, a Compose overlay
is shipped at [`docker-compose.t4.yml`](docker-compose.t4.yml). It
pre-sets the env vars above to values that fit inside 16 GB of VRAM
(`OLLAMA_NUM_PARALLEL=2`, `OLLAMA_KEEP_ALIVE=24h`, text model
`qwen2.5:3b`, vision model `qwen2.5vl:7b`, companion skip enabled at
150 words, `CELERY_WORKER_CONCURRENCY=4`, pool 20/10) and is layered on
top of the main compose file at deploy time:

```bash
docker compose -f docker-compose.yml -f docker-compose.t4.yml up -d
```

Use the overlay as a starting point and re-tune against your measured
benchmark numbers.

### Text-model regression check

Because the default text model is deliberately smaller than the
vision-7B model (so both fit on a T4 together), there is a risk that a
too-small text model mangles the structured extraction prompts. A
standalone regression check automates the fallback ladder:

```bash
python scripts/regression_check.py
```

The script runs the same corpus through the orchestrator once per
candidate text model, diffs the structured outputs against a baseline
using `difflib.SequenceMatcher.ratio()` on normalized drawing number,
title, and document type, and emits a JSON report plus a
`candidate | mean_similarity | pass/fail | latency_delta_pct` summary
table.

**Fallback ladder** — the script tries candidates in this order and
locks in the first one that meets the tolerance:

1. `qwen2.5:3b` — primary target. Smallest VRAM footprint, keeps vision
   at Q4_K_M room on a T4.
2. `llama3.2:3b-instruct` — first alternate if `qwen2.5:3b` fails the
   prompt-fidelity check on your corpus.
3. `phi3.5:3.8b` — second alternate, slightly larger.
4. `qwen2.5:7b` on CPU — last-resort CPU fallback. Quality preserved,
   throughput tanks (~5–10× slower than GPU); the script recommends
   this explicitly instead of silently shipping a GPU swap.

**`--tolerance` flag** — controls the minimum acceptable mean similarity
(default `0.85`, i.e. 85% average fidelity to the baseline's structured
outputs). Raise it (`--tolerance 0.95`) when you cannot afford any
measurable regression, or lower it (`--tolerance 0.75`) when you are
explicitly trading fidelity for speed on well-understood corpora.

See [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) for the full scoring
algorithm, JSON schema, and CPU-fallback operator procedure.

## Hardware Procurement

This section documents the scale-up path for the ingestion service.
Ollama is intentionally kept behind an HTTP boundary (`OLLAMA_BASE_URL`)
so every tier below is reached purely by changing environment variables
and, optionally, moving Ollama to a different host — no code change in
this repo.

All VRAM math in the rationales below uses these reference numbers:

* `qwen2.5vl:7b` (vision) — **~6 GB** at Q4_K_M, **~9 GB** at Q6,
  **~14 GB** at FP16.
* `qwen2.5:7b` (text) — **~4.5 GB** at Q4_K_M, **~6 GB** at Q6.
* KV cache — grows with `OLLAMA_NUM_PARALLEL` and with per-request
  context length. Budget roughly 1–2 GB of headroom per extra parallel
  slot on 7B-class models at typical PDF-page context sizes.

### Baseline tier — single Tesla T4 (16 GB VRAM)

**Limitations.** A T4 cannot simultaneously host vision-7B and text-7B
at reasonable quantization with `OLLAMA_NUM_PARALLEL > 1` and leave
enough VRAM for KV cache. Running both 7B models at Q4_K_M (~6 + ~4.5
= ~10.5 GB) leaves only ~5 GB for KV cache across all parallel slots,
which caps you at `OLLAMA_NUM_PARALLEL=1` and forces model swap on any
larger configuration.

**Recommended loadout.**

| Knob | Value |
| --- | --- |
| `OLLAMA_VISION_MODEL` | `qwen2.5vl:7b` at **Q4_K_M** |
| `OLLAMA_TEXT_MODEL` | `qwen2.5:3b` (frees ~2 GB vs. text-7B Q4_K_M) |
| `OLLAMA_NUM_PARALLEL` | `2` |
| `OLLAMA_KEEP_ALIVE` | `24h` |
| `CELERY_WORKER_CONCURRENCY` | `4` |
| `OLLAMA_HTTP_POOL_MAX_CONNECTIONS` | `20` |
| `OLLAMA_HTTP_POOL_MAX_KEEPALIVE` | `10` |
| `COMPANION_SKIP_ENABLED` | `true` |
| `COMPANION_SKIP_MIN_WORDS` | `150` |

This loadout is the one baked into
[`docker-compose.t4.yml`](docker-compose.t4.yml).

**Expected throughput (anchor).**
`<insert benchmark p50/p95 here after running scripts/bench.py>`

Operators should run `python scripts/bench.py --warmup 2 --iterations 10`
against a representative corpus on their T4 box and replace the
placeholder above with the measured p50 / p95 latency and throughput
numbers. Every subsequent tier's "expected improvement" claim should
be validated against this anchor.

**Rationale.** On 16 GB of VRAM the binding constraint is the combined
footprint of two 7B-class models plus KV cache plus `num_parallel`. At
Q4_K_M, vision-7B is ~6 GB and text-7B is ~4.5 GB — total ~10.5 GB
leaves only ~5.5 GB for KV cache *and* the second parallel slot, which
is right at the edge of OOM under real-world context lengths. Swapping
the text model to `qwen2.5:3b` drops the text footprint to ~2.5 GB and
recovers enough headroom to run `OLLAMA_NUM_PARALLEL=2` reliably. The
regression-check ladder exists precisely so this quality trade-off can
be measured rather than assumed.

### Mid tier — single RTX 4090 or L4 (24 GB VRAM)

**What it unlocks.** 24 GB is the smallest VRAM budget where you can
run **vision at Q6** *and* keep the text-7B model resident concurrently
with room for `OLLAMA_NUM_PARALLEL=2`-to-`4` KV cache. This is the
sweet spot for quality-sensitive workloads that still want
single-box-appliance economics.

**Per-knob guidance.**

| Knob | Value | Rationale |
| --- | --- | --- |
| `OLLAMA_VISION_MODEL` | `qwen2.5vl:7b` at **Q6** | Vision fidelity on dense academic PDFs meaningfully improves over Q4_K_M. |
| `OLLAMA_TEXT_MODEL` | `qwen2.5:7b` | 24 GB fits vision-Q6 (~9 GB) + text-7B-Q4_K_M (~4.5 GB) + KV cache with headroom. |
| `OLLAMA_NUM_PARALLEL` | `2`–`4` | Start at 2 and raise to 4 if bench shows the KV cache still fits your context sizes. |
| `CELERY_WORKER_CONCURRENCY` | `4`–`6` | Keep the worker pool at or slightly above `num_parallel` to feed Ollama without queuing. |
| `OLLAMA_HTTP_POOL_MAX_CONNECTIONS` | `30` | Raised from 20 so the pool cannot become the bottleneck. |
| `OLLAMA_HTTP_POOL_MAX_KEEPALIVE` | `15` | Half of max connections, same ratio as the baseline. |
| `COMPANION_SKIP_ENABLED` | `true` (recommended) | Still a latency win on text-dominant PDFs even when VRAM is plentiful. |

**Rationale.** Vision-Q6 (~9 GB) + text-7B-Q4_K_M (~4.5 GB) = ~13.5 GB
base load. Remaining ~10.5 GB is enough for 2–4 parallel KV caches on
typical PDF page contexts, which is the first tier where you can
actually saturate `num_parallel=4` without evicting a model.

### High tier — single A100 40GB / L40S / RTX 6000 Ada

**What it unlocks.** 40 GB+ of VRAM permits full-precision
(`FP16`) vision inference and a larger text model (e.g. `qwen2.5:14b`
at Q4_K_M, ~9 GB) concurrently, with room for `OLLAMA_NUM_PARALLEL=4`
to `8`. Appropriate for throughput-heavy workloads or quality-critical
single-box deployments.

**Per-knob guidance.**

| Knob | Value |
| --- | --- |
| `OLLAMA_VISION_MODEL` | `qwen2.5vl:7b` at **FP16** (or Q8 for a headroom buffer) |
| `OLLAMA_TEXT_MODEL` | `qwen2.5:7b` Q6, or `qwen2.5:14b` Q4_K_M |
| `OLLAMA_NUM_PARALLEL` | `4`–`8` |
| `CELERY_WORKER_CONCURRENCY` | `8`–`12` |
| `CELERY_WORKER_PREFETCH_MULTIPLIER` | `1` (keep — prefetching is the wrong lever for long tasks) |
| `OLLAMA_HTTP_POOL_MAX_CONNECTIONS` | `40`–`60` |
| `OLLAMA_HTTP_POOL_MAX_KEEPALIVE` | `20`–`30` |

**Rationale.** Vision-FP16 (~14 GB) + text-7B-Q6 (~6 GB) = ~20 GB
base load. On a 40 GB card that leaves ~20 GB for KV cache, which
easily sustains `num_parallel=8` at realistic PDF context sizes.
`CELERY_WORKER_CONCURRENCY=8–12` is sized to keep the 8-slot Ollama
server saturated even while individual Celery workers are blocked on
I/O (PDF rendering, ChromaDB writes, Elasticsearch indexing).

### Scale-out tier — dedicated Ollama inference node(s)

**What it unlocks.** When a single GPU box is no longer enough, or when
you want to amortize expensive inference hardware across multiple
ingestion services, move Ollama onto its own host(s). Because Ollama is
kept as a swappable HTTP endpoint in this repo, the migration is purely
a configuration change:

1. Provision the inference node with one or more of the GPUs from the
   higher tiers above.
2. Start Ollama on that host with `OLLAMA_HOST=0.0.0.0:11434` and the
   runtime flags matching your chosen tier (`OLLAMA_NUM_PARALLEL`,
   `OLLAMA_KEEP_ALIVE`, pre-pulled model tags).
3. On the ingestion service, set `OLLAMA_BASE_URL=http://<inference-node>:11434`
   and leave *this* service running on a much smaller CPU-only box
   (the ingestion pipeline itself is CPU-bound on PDF rendering and
   database I/O, not inference).
4. Front multiple inference nodes with a simple HTTP load balancer
   (HAProxy, nginx, or a cloud L7 LB) and point `OLLAMA_BASE_URL` at
   the LB VIP.

**Scaling lever.** Once Ollama is remote, the **HTTP pool settings**
become the dominant throughput knob on the ingestion side.
`OLLAMA_HTTP_POOL_MAX_CONNECTIONS` must be ≥ the sum of every
downstream `OLLAMA_NUM_PARALLEL` across all inference nodes, otherwise
the client pool caps throughput below what the fleet can actually
serve. Raise `OLLAMA_HTTP_POOL_MAX_KEEPALIVE` in proportion so idle
connections stay warm between bursts, and raise
`CELERY_WORKER_CONCURRENCY` to feed the larger pool.

**Rationale.** The ingestion-service Python layer is CPU-bound and
embarrassingly parallel across Celery workers; there is no benefit to
co-locating it with the GPU once the GPU is no longer the single-box
bottleneck. Moving Ollama onto dedicated hardware lets you (a) pick GPU
SKUs purely on inference economics, (b) run multiple ingestion API
replicas behind a load balancer on cheap CPU nodes, and (c) share one
inference fleet across multiple environments (dev/staging/prod) by
pointing each at a different `OLLAMA_BASE_URL`. The VRAM math from the
previous tiers still applies per-node, and the HTTP pool settings on
*this* service become the only cross-fleet scaling lever you need to
tune.
