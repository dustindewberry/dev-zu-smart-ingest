# MVP Boundary

This document marks the **MVP COMPLETE** point in the Zubot Smart Ingestion
Service build. Steps 1 through 17 of the 25-step execution plan together
form the Minimum Viable Product: a service that can accept a batch of
construction PDFs, extract drawing number / title / document type with
confidence scoring, assemble a Bedrock-KB-compatible sidecar, and persist
high-confidence results into ChromaDB for downstream search.

## What is in the MVP (steps 1–17)

| # | Step | Capability |
|---|---|---|
| 1 | project-structure-setup | Layered Python package layout, dependencies pinned |
| 2 | contracts-and-types-definition | Protocols, entities, enums, shared types |
| 3 | lint-configuration-setup | import-linter dependency direction enforcement |
| 4 | fastapi-application-skeleton | Pydantic Settings + FastAPI factory (CAP-001) |
| 5 | docker-setup | Dockerfile + docker-compose service definitions (CAP-002) |
| 6 | postgresql-database-schema | SQLAlchemy models, Alembic migration, JobRepository (CAP-004) |
| 7 | celery-configuration | Redis-broker Celery app + ITaskQueue (CAP-005) |
| 8 | pdf-processor-implementation | PyMuPDF load/extract/render (CAP-006, CAP-007) |
| 9 | ollama-client-implementation | Vision + text Ollama HTTP client with retries (CAP-008) |
| 10 | auth-middleware-implementation | API key + WOD JWT authentication (CAP-026) |
| 11 | job-submission-endpoint | POST /extract with deduplication (CAP-009) |
| 12 | status-endpoints | GET /batches/{id}, GET /jobs/{id} (CAP-010, CAP-011) |
| 13 | health-endpoint | GET /health with dependency probes (CAP-003) |
| 14 | stage1-extractors | Drawing number, title, document type extraction (CAP-013/14/15) |
| 15 | sidecar-builder | Stage 3 Bedrock-KB sidecar assembly (CAP-019) |
| 16 | orchestrator-and-confidence | ExtractionOrchestrator + ConfidenceCalculator (CAP-020, CAP-021) |
| 17 | chromadb-integration | **ChromaDBMetadataWriter (CAP-023) — MVP COMPLETE marker** |

After step 17, an end-to-end happy path is functional:

```
client → POST /extract → JobService → Celery queue
                                          ↓
                                ExtractionOrchestrator
                                          ↓
              [Stage 1: drawing/title/document_type extractors]
                                          ↓
                          [Stage 3: SidecarBuilder]
                                          ↓
                          [ConfidenceCalculator]
                                          ↓
                AUTO/SPOT tier ──→ ChromaDBMetadataWriter ──→ ChromaDB
                REVIEW tier ────→ Review queue (held for human review)
```

A reviewer can poll `GET /batches/{id}` and `GET /jobs/{id}` to monitor
status. AUTO and SPOT-tier results are automatically searchable in
ChromaDB; REVIEW-tier results wait for human approval (the review queue
endpoints land in step 20).

## What is NOT in the MVP (steps 18–25)

These steps add observability, advanced extraction quality, and
operational hardening on top of the working MVP:

- **Step 18** companion-generator (CAP-017) — Stage 2 vision-model
  companion document generation. The orchestrator currently skips Stage
  2 entirely.
- **Step 19** companion-validator (CAP-018) — cross-checks the companion
  against the extraction result and applies a confidence penalty when
  contradictions are detected.
- **Step 20** review-queue-api (CAP-022, CAP-012) — endpoints for human
  reviewers to approve/reject REVIEW-tier results and a preview image
  endpoint.
- **Step 21** elasticsearch-and-webhooks (CAP-024, CAP-025) — full-text
  companion indexing in Elasticsearch and asynchronous callback delivery
  to client webhook URLs on batch completion.
- **Step 22** otel-instrumentation (CAP-027) — OpenTelemetry tracing
  spans for every pipeline stage with trace IDs persisted to the job
  record for log correlation.
- **Step 23** prometheus-metrics (CAP-028) — `/metrics` endpoint with
  per-tier counters, latency histograms, and queue depth gauges.
- **Step 24** rate-limiting (CAP-030) — slowapi-based per-user/per-IP
  rate limits with Redis-backed counters.
- **Step 25** structured-logging (CAP-029) — structlog JSON output with
  request/job context binding and PII/secret redaction.

## MVP exit criteria — verified at step 17

1. **Architectural correctness** — `ExtractionOrchestrator` injects
   `IMetadataWriter` via its constructor, depends only on the protocol
   not on a concrete adapter, and is wired in `services/__init__.py`
   via `build_orchestrator()` which constructs the
   `ChromaDBMetadataWriter` from `Settings.CHROMADB_HOST` and
   `Settings.CHROMADB_PORT`.

2. **Tier routing is enforced** — REVIEW-tier results are NEVER written
   to ChromaDB by the orchestrator; only AUTO and SPOT tiers reach the
   metadata writer. This is verified by
   `tests/unit/test_extraction_orchestrator.py::test_review_tier_does_not_write_metadata_to_writer`.

3. **Failure is recoverable** — `ChromaDBMetadataWriter.write_metadata`
   never propagates exceptions to the caller. Failures are logged and
   returned as `False`. Both the writer's behavior and the
   orchestrator's response (recording the failure in
   `pipeline_trace["errors"]` and the `errors` list) are covered by
   unit tests.

4. **Provenance is unambiguous** — every persisted ChromaDB record is
   stamped with `ingestion_service: zubot-ingestion` so downstream
   consumers can distinguish records originating from this service.
   Verified by
   `tests/unit/test_chromadb_writer.py::test_write_metadata_payload_stamps_provenance_marker`.

5. **Collection naming is canonical** — collection names are derived via
   `zubot_ingestion.shared.constants.chroma_collection_name`, the same
   helper used everywhere else in the codebase, so the writer cannot
   drift from sibling components. Verified by three collection-routing
   tests covering default-default, scoped, and partially-scoped IDs.

## Test status at the MVP boundary

Unit tests at the close of step 17 (in this worktree, which contains
only the tasks needed to validate CAP-020/021/023 in isolation):

```
tests/unit/test_celery_extract_task.py        6 passed
tests/unit/test_chromadb_writer.py            21 passed
tests/unit/test_confidence_calculator.py      25 passed
tests/unit/test_extraction_orchestrator.py    26 passed
                                              ----------
                                              78 passed
```

The full integration suite (with PostgreSQL, Redis, Ollama, and
ChromaDB containers) runs against the merged main branch in CI; this
worktree's unit suite uses in-memory test doubles for every external
dependency.
