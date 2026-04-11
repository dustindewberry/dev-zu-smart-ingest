# API Usage Guide

This guide walks through the primary zubot-ingestion API workflows with `curl`
examples. For the auto-generated OpenAPI reference, visit `/docs` (Swagger UI) or
`/redoc` (ReDoc) on a running instance.

**Base URL used in examples:** `http://localhost:4243`

---

## Table of Contents

- [Authentication](#authentication)
- [Happy-Path Workflow](#happy-path-workflow)
  - [Step 1: Submit PDFs for Extraction](#step-1-submit-pdfs-for-extraction)
  - [Step 2: Poll for Results](#step-2-poll-for-results)
  - [Step 3: Webhook Callback (Alternative to Polling)](#step-3-webhook-callback-alternative-to-polling)
- [Human Review Workflow](#human-review-workflow)
- [Rate Limiting](#rate-limiting)
- [Error Responses](#error-responses)
- [Health Check](#health-check)

---

## Authentication

Every request (except [exempt paths](#exempt-paths)) must include one of two
credentials. The middleware tries them in order — API key first, then JWT.

### 1. Static API Key

Send the key in the `X-API-Key` header. The service compares it against the
`ZUBOT_INGESTION_API_KEY` environment variable.

```bash
curl -H "X-API-Key: $ZUBOT_INGESTION_API_KEY" \
     http://localhost:4243/batches/...
```

On success the middleware attaches an `AuthContext` with:

| Field           | Value       |
|-----------------|-------------|
| `user_id`       | `"api"`     |
| `auth_method`   | `"api_key"` |
| `deployment_id` | `null`      |
| `node_id`       | `null`      |

This mechanism is intended for trusted service-to-service callers (Celery
workers, the WOD orchestrator, internal tooling).

### 2. JWT Bearer Token

Send a WOD-issued JWT in the `Authorization` header:

```bash
curl -H "Authorization: Bearer $WOD_JWT_TOKEN" \
     http://localhost:4243/batches/...
```

The token is decoded with the `WOD_JWT_SECRET` environment variable using the
**HS256** algorithm. The `exp` claim is enforced automatically by PyJWT.

Required claims:

| Claim           | Required | Description                        |
|-----------------|----------|------------------------------------|
| `user_id`       | yes      | Authenticated principal identifier |
| `deployment_id` | no       | Coerced to `int \| null`           |
| `node_id`       | no       | Coerced to `int \| null`           |

On success the middleware attaches an `AuthContext` with:

| Field           | Value                          |
|-----------------|--------------------------------|
| `user_id`       | `str(claims["user_id"])`       |
| `auth_method`   | `"wod_token"`                  |
| `deployment_id` | `int(claims["deployment_id"])` or `null` |
| `node_id`       | `int(claims["node_id"])` or `null`       |

### Authentication Failure

If neither mechanism succeeds the service returns:

```json
HTTP/1.1 401 Unauthorized
Content-Type: application/json

{"detail": "Unauthorized"}
```

### Exempt Paths

The following paths bypass authentication entirely:

- `/health`
- `/metrics`
- `/docs` (and all sub-paths, e.g. `/docs/oauth2-redirect`)
- `/redoc`
- `/openapi.json` (and all `/openapi*` sub-paths)

---

## Happy-Path Workflow

The primary workflow is: **submit PDFs** -> **poll for results** (or receive
a **webhook callback**).

### Step 1: Submit PDFs for Extraction

```
POST /extract
Content-Type: multipart/form-data
Rate limit: 20/minute
```

Submit one or more PDF files for metadata extraction.

#### curl Example

```bash
curl -X POST http://localhost:4243/extract \
  -H "X-API-Key: $ZUBOT_INGESTION_API_KEY" \
  -F "files=@drawing1.pdf" \
  -F "files=@drawing2.pdf" \
  -F "mode=auto" \
  -F "callback_url=https://example.com/webhook"
```

#### Form Fields

| Field           | Type     | Required | Default | Description                                                      |
|-----------------|----------|----------|---------|------------------------------------------------------------------|
| `files`         | file(s)  | yes      | —       | One or more PDF files (multipart upload)                         |
| `mode`          | string   | no       | `auto`  | Extraction mode: `auto`, `drawing`, `title`, `fast`, `accurate`  |
| `callback_url`  | string   | no       | `null`  | Webhook URL to POST results to on completion                     |
| `deployment_id` | string   | no       | `null`  | Optional deployment identifier (coerced to integer)              |
| `node_id`       | string   | no       | `null`  | Optional node identifier (coerced to integer)                    |

#### Sample Response — `202 Accepted`

```json
{
  "batch_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "total": 2,
  "poll_url": "/batches/a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "jobs": [
    {
      "job_id": "11111111-1111-1111-1111-111111111111",
      "filename": "drawing1.pdf",
      "status": "queued",
      "file_hash": "sha256:abc123def456...",
      "result": null
    },
    {
      "job_id": "22222222-2222-2222-2222-222222222222",
      "filename": "drawing2.pdf",
      "status": "queued",
      "file_hash": "sha256:789012fed345...",
      "result": null
    }
  ]
}
```

The `poll_url` value can be used directly to check batch progress (see Step 2).

### Step 2: Poll for Results

#### Get Batch Status

```
GET /batches/{batch_id}
Rate limit: 100/minute
```

Returns the batch with aggregated progress counters and per-job summaries.

```bash
curl -H "X-API-Key: $ZUBOT_INGESTION_API_KEY" \
     http://localhost:4243/batches/a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

##### Sample Response — `200 OK`

```json
{
  "batch_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "completed",
  "progress": {
    "completed": 2,
    "queued": 0,
    "failed": 0,
    "in_progress": 0,
    "total": 2
  },
  "callback_url": "https://example.com/webhook",
  "deployment_id": null,
  "node_id": null,
  "created_at": "2025-01-15T10:30:00+00:00",
  "updated_at": "2025-01-15T10:31:45+00:00",
  "jobs": [
    {
      "job_id": "11111111-1111-1111-1111-111111111111",
      "filename": "drawing1.pdf",
      "status": "completed",
      "file_hash": "sha256:abc123def456...",
      "result": {
        "drawing_number": "DWG-001-A",
        "title": "Ground Floor Plan",
        "document_type": "floor_plan"
      }
    },
    {
      "job_id": "22222222-2222-2222-2222-222222222222",
      "filename": "drawing2.pdf",
      "status": "completed",
      "file_hash": "sha256:789012fed345...",
      "result": {
        "drawing_number": "DWG-002-B",
        "title": "Electrical Layout",
        "document_type": "electrical"
      }
    }
  ]
}
```

#### Get Job Detail

```
GET /jobs/{job_id}
Rate limit: 100/minute
```

Returns the full detail for a single job, including the extraction result,
pipeline trace, and observability data.

```bash
curl -H "X-API-Key: $ZUBOT_INGESTION_API_KEY" \
     http://localhost:4243/jobs/11111111-1111-1111-1111-111111111111
```

##### Sample Response — `200 OK`

```json
{
  "job_id": "11111111-1111-1111-1111-111111111111",
  "batch_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "filename": "drawing1.pdf",
  "file_hash": "sha256:abc123def456...",
  "status": "completed",
  "result": {
    "drawing_number": "DWG-001-A",
    "title": "Ground Floor Plan",
    "document_type": "floor_plan",
    "discipline": "architectural",
    "revision": "A"
  },
  "error_message": null,
  "pipeline_trace": {
    "stages": {
      "stage1": {"duration_ms": 1200, "status": "ok"},
      "stage2": {"duration_ms": 800, "status": "ok"},
      "stage3": {"duration_ms": 350, "status": "ok"},
      "confidence": {"duration_ms": 50, "status": "ok"}
    }
  },
  "otel_trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "processing_time_ms": 2400,
  "created_at": "2025-01-15T10:30:00+00:00",
  "updated_at": "2025-01-15T10:30:02.400+00:00"
}
```

### Step 3: Webhook Callback (Alternative to Polling)

If you provided a `callback_url` when submitting the batch, the service will
POST a notification to that URL when each job completes. This removes the need
to poll.

#### Callback Delivery

- **Method:** `POST`
- **Target:** The `callback_url` from the original submission
- **Content-Type:** `application/json`

#### Callback Headers

| Header              | Description                                                     |
|---------------------|-----------------------------------------------------------------|
| `Content-Type`      | `application/json`                                              |
| `User-Agent`        | `zubot-ingestion/1.0`                                           |
| `X-API-Key`         | Service API key (`ZUBOT_INGESTION_API_KEY`), omitted if empty   |
| `X-Zubot-Signature` | HMAC-SHA256 hex digest of the raw body (if signing secret configured) |

#### Signature Verification

When `CALLBACK_SIGNING_SECRET` is configured, the service signs the raw JSON
body with HMAC-SHA256. To verify:

1. Read the raw request body bytes.
2. Compute `HMAC-SHA256(signing_secret, body)` as a lowercase hex string.
3. Compare the result to the `X-Zubot-Signature` header in constant time.

The body is serialised with sorted keys and compact separators (`",", ":"`) to
ensure a deterministic byte sequence.

#### Sample Webhook Payload

```json
{
  "job_id": "11111111-1111-1111-1111-111111111111",
  "batch_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "filename": "drawing1.pdf",
  "file_hash": "sha256:abc123def456...",
  "status": "completed",
  "confidence_tier": "auto",
  "overall_confidence": 0.95,
  "result": {
    "drawing_number": "DWG-001-A",
    "title": "Ground Floor Plan",
    "document_type": "floor_plan"
  },
  "error_message": null,
  "processing_time_ms": 2400,
  "otel_trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "created_at": "2025-01-15T10:30:00+00:00",
  "updated_at": "2025-01-15T10:30:02.400+00:00"
}
```

#### Retry Behaviour

| Parameter      | Value                      |
|----------------|----------------------------|
| Base delay     | 0.5 seconds                |
| Backoff factor | 2x                         |
| Max retries    | 3                          |
| Retry schedule | 0.5s, 1.0s, 2.0s          |
| 5xx responses  | Retried                    |
| 4xx responses  | **Not** retried (terminal) |
| Network errors | Retried                    |

After all retries are exhausted the failure is logged and the service continues
processing. The webhook system never raises exceptions to the caller.

---

## Human Review Workflow

Jobs whose extraction confidence falls below the auto-accept threshold land in
the `REVIEW` confidence tier and must be triaged by a human reviewer.

### List Pending Reviews

```
GET /review/pending
Rate limit: 100/minute
```

Returns a paginated list of jobs awaiting human review.

```bash
curl -H "X-API-Key: $ZUBOT_INGESTION_API_KEY" \
     "http://localhost:4243/review/pending?limit=20&offset=0"
```

#### Query Parameters

| Parameter | Type | Default | Range   | Description                |
|-----------|------|---------|---------|----------------------------|
| `limit`   | int  | `50`    | 1 - 500 | Max items to return        |
| `offset`  | int  | `0`     | >= 0    | Items to skip              |

#### Sample Response — `200 OK`

```json
{
  "items": [
    {
      "job_id": "33333333-3333-3333-3333-333333333333",
      "drawing_number": "DWG-003-C",
      "title": "Unclear Title",
      "confidence_tier": "review",
      "confidence_score": 0.42,
      "created_at": "2025-01-15T10:30:00+00:00"
    }
  ],
  "limit": 20,
  "offset": 0,
  "total": 1
}
```

### Approve a Review Job

```
POST /review/{job_id}/approve
Rate limit: 50/minute
```

Transitions a review-tier job to `COMPLETED` and stamps reviewer metadata into
the result.

```bash
curl -X POST http://localhost:4243/review/33333333-3333-3333-3333-333333333333/approve \
  -H "X-API-Key: $ZUBOT_INGESTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "reviewer_id": "jane.doe",
    "notes": "Drawing number confirmed against the register"
  }'
```

#### Request Body

| Field         | Type   | Required | Description                           |
|---------------|--------|----------|---------------------------------------|
| `reviewer_id` | string | yes      | Identifier of the approving reviewer  |
| `notes`       | string | no       | Optional free-text reviewer notes     |

#### Sample Response — `200 OK`

The response contains the full updated job record (same shape as
`GET /jobs/{job_id}` with additional `confidence_tier` and `confidence_score`
fields). The `result` blob includes a `review` sub-key with the audit trail:

```json
{
  "job_id": "33333333-3333-3333-3333-333333333333",
  "batch_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "filename": "drawing3.pdf",
  "status": "completed",
  "result": {
    "drawing_number": "DWG-003-C",
    "title": "Unclear Title",
    "review": {
      "action": "approved",
      "reviewer_id": "jane.doe",
      "notes": "Drawing number confirmed against the register",
      "reviewed_at": "2025-01-15T11:00:00+00:00"
    }
  },
  "confidence_tier": "review",
  "confidence_score": 0.42,
  "...": "..."
}
```

### Reject a Review Job

```
POST /review/{job_id}/reject
Rate limit: 50/minute
```

Transitions a review-tier job to `FAILED` with a rejection reason.

```bash
curl -X POST http://localhost:4243/review/33333333-3333-3333-3333-333333333333/reject \
  -H "X-API-Key: $ZUBOT_INGESTION_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "reviewer_id": "jane.doe",
    "reason": "PDF is corrupted, extraction not salvageable"
  }'
```

#### Request Body

| Field         | Type   | Required | Description                            |
|---------------|--------|----------|----------------------------------------|
| `reviewer_id` | string | yes      | Identifier of the rejecting reviewer   |
| `reason`      | string | yes      | Human-readable rejection reason        |

#### Sample Response — `200 OK`

```json
{
  "job_id": "33333333-3333-3333-3333-333333333333",
  "batch_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "filename": "drawing3.pdf",
  "status": "failed",
  "error_message": "Rejected by jane.doe: PDF is corrupted, extraction not salvageable",
  "confidence_tier": "review",
  "confidence_score": 0.42,
  "...": "..."
}
```

### Review Conflict — `409`

If you attempt to approve or reject a job that is **not** in the `REVIEW`
confidence tier, the API returns:

```json
HTTP/1.1 409 Conflict

{
  "detail": "Job 33333333-... is not in the review tier (confidence_tier=auto); only review-tier jobs may be approved or rejected"
}
```

---

## Rate Limiting

All endpoints are rate-limited using a Redis-backed sliding window (Redis DB 4).

### Limits by Endpoint

| Endpoint                        | Limit      | Configurable via          |
|---------------------------------|------------|---------------------------|
| `POST /extract`                 | 20/minute  | `RATE_LIMIT_EXTRACT`*     |
| `GET /batches/{batch_id}`       | 100/minute | `RATE_LIMIT_DEFAULT`      |
| `GET /jobs/{job_id}`            | 100/minute | `RATE_LIMIT_DEFAULT`      |
| `GET /review/pending`           | 100/minute | `RATE_LIMIT_DEFAULT`      |
| `POST /review/{job_id}/approve` | 50/minute  | —                         |
| `POST /review/{job_id}/reject`  | 50/minute  | —                         |
| All other endpoints             | 100/minute | `RATE_LIMIT_DEFAULT`      |

\* The extract endpoint is decorated with a hard-coded `@limiter.limit("20/minute")`.
The global default (`RATE_LIMIT_DEFAULT`, default `"100/minute"`) applies to
endpoints that do not declare their own limit.

### Exempt Endpoints

`/health` and `/metrics` are **never** rate-limited so Kubernetes probes and
Prometheus scrapes are not throttled.

### Key Resolution

The rate-limit key is resolved in this order:

1. **Authenticated user** — if `AuthMiddleware` attached an `AuthContext`, the
   bucket key is `user:{user_id}`.
2. **Client IP** — fallback to `ip:{remote_address}` (reads `X-Forwarded-For`
   when behind a reverse proxy).

This ensures two users sharing an egress NAT do not crowd each other out.

### Rate Limit Response — `429 Too Many Requests`

```json
HTTP/1.1 429 Too Many Requests
X-RateLimit-Limit: 20
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1705312345
Retry-After: 42

{
  "detail": "Rate limit exceeded",
  "retry_after": 42
}
```

#### Response Headers

| Header                 | Description                                    |
|------------------------|------------------------------------------------|
| `X-RateLimit-Limit`    | Total requests allowed per window              |
| `X-RateLimit-Remaining`| Requests remaining in the current window       |
| `X-RateLimit-Reset`    | Unix epoch (seconds) when the window resets    |
| `Retry-After`          | Seconds the client should wait before retrying |

---

## Error Responses

All errors are returned as JSON with a `detail` field.

| Status | Meaning                        | Example Cause                                         |
|--------|--------------------------------|-------------------------------------------------------|
| `400`  | Bad Request                    | Malformed UUID, invalid extraction `mode`, non-numeric `deployment_id` |
| `401`  | Unauthorized                   | Missing or invalid `X-API-Key` / `Authorization` header |
| `404`  | Not Found                      | Batch or Job ID does not exist                        |
| `409`  | Conflict                       | Job is not in `REVIEW` tier for approve/reject action |
| `413`  | Payload Too Large              | PDF file exceeds the maximum allowed size             |
| `429`  | Too Many Requests              | Rate limit exceeded (see [Rate Limiting](#rate-limiting)) |
| `503`  | Service Unavailable            | Health check reports `unhealthy` (critical dependency down) |

### Error Response Shape

```json
{
  "detail": "Human-readable error message"
}
```

---

## Health Check

```
GET /health
```

Exempt from authentication and rate limiting. Returns the aggregated liveness
status of all external dependencies.

```bash
curl http://localhost:4243/health
```

### Status Aggregation Rules

| Overall Status | Condition                                                        | HTTP Code |
|----------------|------------------------------------------------------------------|-----------|
| `healthy`      | All critical AND optional dependencies are healthy               | `200`     |
| `degraded`     | All critical dependencies healthy, but one or more optional deps down | `200`     |
| `unhealthy`    | At least one critical dependency is down                         | `503`     |

### Dependency Classification

| Dependency      | Classification | Probe Method                   |
|-----------------|----------------|--------------------------------|
| PostgreSQL      | **Critical**   | `SELECT 1` via async session   |
| Redis           | **Critical**   | `PING` via async client        |
| Ollama          | Optional       | `GET /api/tags`                |
| ChromaDB        | Optional       | `list_collections()`           |
| Elasticsearch   | Optional       | `cluster.health()`             |
| Celery          | Optional       | `control.inspect()` for worker count and queue depth |

### Sample Response — Healthy

```json
HTTP/1.1 200 OK

{
  "status": "healthy",
  "service": "zubot-ingestion",
  "version": "0.1.0",
  "dependencies": {
    "postgres": {
      "status": "healthy",
      "latency_ms": 12,
      "error": null
    },
    "redis": {
      "status": "healthy",
      "latency_ms": 3,
      "error": null
    },
    "ollama": {
      "status": "healthy",
      "latency_ms": 45,
      "error": null
    },
    "chromadb": {
      "status": "healthy",
      "latency_ms": 28,
      "error": null
    },
    "elasticsearch": {
      "status": "healthy",
      "latency_ms": 15,
      "error": null
    }
  },
  "workers": {
    "active": 4,
    "queue_depth": 7
  }
}
```

### Sample Response — Degraded

```json
HTTP/1.1 200 OK

{
  "status": "degraded",
  "service": "zubot-ingestion",
  "version": "0.1.0",
  "dependencies": {
    "postgres": {
      "status": "healthy",
      "latency_ms": 10,
      "error": null
    },
    "redis": {
      "status": "healthy",
      "latency_ms": 2,
      "error": null
    },
    "ollama": {
      "status": "unhealthy",
      "latency_ms": 5000,
      "error": "probe exceeded 5.0s timeout"
    },
    "chromadb": {
      "status": "healthy",
      "latency_ms": 30,
      "error": null
    },
    "elasticsearch": {
      "status": "healthy",
      "latency_ms": 18,
      "error": null
    }
  },
  "workers": {
    "active": 4,
    "queue_depth": 12
  }
}
```

### Sample Response — Unhealthy

```json
HTTP/1.1 503 Service Unavailable

{
  "status": "unhealthy",
  "service": "zubot-ingestion",
  "version": "0.1.0",
  "dependencies": {
    "postgres": {
      "status": "unhealthy",
      "latency_ms": 5001,
      "error": "probe exceeded 5.0s timeout"
    },
    "redis": {
      "status": "healthy",
      "latency_ms": 2,
      "error": null
    },
    "ollama": {
      "status": "healthy",
      "latency_ms": 50,
      "error": null
    },
    "chromadb": {
      "status": "healthy",
      "latency_ms": 25,
      "error": null
    },
    "elasticsearch": {
      "status": "healthy",
      "latency_ms": 14,
      "error": null
    }
  },
  "workers": {
    "active": 0,
    "queue_depth": 0
  }
}
```
