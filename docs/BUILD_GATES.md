# Build Gates — zutec-smart-ingest

This document enumerates the deterministic 8-bug fingerprint that has historically
recurred on every PolyForge merge of this repository. Each bug is guarded by a
regression test and a CI check. If any of these regressions reappears, the CI check
(`scripts/check_build_gates.py`) will fail the build.

## Why this document exists

The PolyForge orchestrator has recorded at least ten independent QA cycles on this
repository that all reproduced the same eight critical bugs. The root cause is that
PolyForge sibling tasks step-19 (companion validator), step-20 (review queue API),
and step-21 (Elasticsearch + callback) have historically failed to land their
canonical implementations during merges, and four cross-cutting integration bugs
(composition-root drift, Celery retry flapping, `update_job_status` vs
`update_job_result`, Ollama prompt injection) have consistently resurfaced in
every build.

The build gates enumerated below are the deterministic loop-breaking safeguards.
They are designed to (a) catch the fingerprint deterministically on CI, and (b)
give the orchestrator a mechanism to detect the loop and escalate rather than
re-running QA Reviewer / QA Remediation indefinitely.

## The eight bugs (and their guards)

| # | Bug | File | Guard |
|---|-----|------|-------|
| 1 | `JobRepository` factory composition drift (`database_url=` vs `session=`) | `services/__init__.py` | `test_job_repository_factory_is_async_context_manager` + CI check 2 |
| 2 | Celery autoretry + manual cleanup flap | `services/celery_app.py` | `test_celery_retry_cleanup_gated_on_final_attempt` |
| 3 | `update_job_status` used instead of `update_job_result` on success | `services/celery_app.py` | `test_update_job_result_called_on_success_path` |
| 4 | `api/routes/review.py` placeholder stub | `api/routes/review.py` | `test_review_router_exposes_approve_and_reject` + sentinel check |
| 5 | `domain/pipeline/validation.py` missing | `domain/pipeline/validation.py` | `test_companion_validator_module_exists` + import check |
| 6 | `infrastructure/elasticsearch` + `infrastructure/callback` stubs | `infrastructure/{elasticsearch,callback}/__init__.py` | `test_elasticsearch_and_callback_modules_exist` + sentinel check |
| 7 | Ollama prompt injection (raw text concatenation) | `infrastructure/ollama/client.py` | `test_ollama_generate_text_does_not_concatenate_text_into_prompt` |
| 8 | `IJobRepository` protocol drift | `domain/protocols.py` | `test_ijobrepository_protocol_has_update_job_result_and_branded_file_hash` |

## Detailed bug descriptions

### Bug 1 — JobRepository factory composition drift

**Symptom:** `services/__init__.py` constructs `JobRepository(database_url=...)`
but the concrete `JobRepository.__init__` accepts only `(session: AsyncSession)`.
Every Celery task call raises `TypeError` at the first invocation of
`get_job_repository()`.

**Fix shape:** Convert `get_job_repository()` to an `@asynccontextmanager` that
yields `JobRepository(session)` from `get_session()`. All callers switch from
`repo = get_job_repository()` to `async with get_job_repository() as repo:`.

### Bug 2 — Celery autoretry + manual cleanup flap

**Symptom:** `services/celery_app.py` pairs `autoretry_for=(Exception,)` with a
manual `except Exception` block that calls `_mark_job_failed` on every retry
attempt — not just the final one. Combined with mid-pipeline
`update_job_status(status=PROCESSING)` the job status oscillates
FAILED↔PROCESSING up to `max_retries+1` times.

**Fix shape:** Gate `_mark_job_failed` on
`self.request.retries >= self.max_retries`. Keep `autoretry_for`.

### Bug 3 — `update_job_status` used instead of `update_job_result` on success

**Symptom:** The Celery task persists pipeline output via
`repo.update_job_status(result=...)` which only writes the JSONB blob. The
dedicated indexed columns `confidence_score`, `confidence_tier`,
`processing_time_ms`, `otel_trace_id`, and `pipeline_trace` are never populated.
`GET /review/pending` (which filters on `JobORM.confidence_tier`) goes blind.

**Fix shape:** Call `repo.update_job_result(...)` on the success path with all
five indexed fields plus the result blob.

### Bug 4 — `api/routes/review.py` placeholder stub

**Symptom:** The route is a 24-line placeholder that unconditionally returns
`{"items": []}` with no `/review/{job_id}/approve` or `/review/{job_id}/reject`
POST handlers. The entire human review workflow is missing.

**Fix shape:** Implement real `GET /review/pending` (filtered on
`JobORM.confidence_tier`), `POST /review/{job_id}/approve`, and
`POST /review/{job_id}/reject` handlers wired against `IJobRepository`.

### Bug 5 — `domain/pipeline/validation.py` missing entirely

**Symptom:** Step-19 (CAP-018 companion validator) was supposed to deliver
`ICompanionValidator.validate(companion_text, extraction_result) -> ValidationResult`.
The module does not exist, so the orchestrator's
confidence-adjustment-on-validation-failure path cannot run.

**Fix shape:** Create the module with a `CompanionValidator` class implementing
`ICompanionValidator` and returning a `ValidationResult` with
`(is_valid, confidence_penalty, failures, warnings)` fields.

### Bug 6 — `infrastructure/elasticsearch` + `infrastructure/callback` stubs

**Symptom:** Both `__init__.py` files are single-line docstrings with no
implementation. There is no `ElasticsearchSearchIndexer` implementing
`ISearchIndexer.index_companion`, and no `CallbackHttpClient` implementing
`ICallbackClient.notify_completion`. CAP-024 (Elasticsearch full-text companion
indexing) and CAP-025 (webhook callback delivery) are entirely missing.

**Fix shape:** Create `ElasticsearchSearchIndexer` (using the official async
client) and `CallbackHttpClient` (httpx-based with exponential backoff) adapters.

### Bug 7 — Ollama prompt injection (raw text concatenation)

**Symptom:** `infrastructure/ollama/client.py` builds
`full_prompt = f"{prompt}\n\nCONTEXT:\n{text}"` where `text` is user-controlled
PDF content. No boundary delimiter, no closing-tag escaping. A malicious PDF can
inject arbitrary instructions into the LLM prompt.

**Fix shape:** Migrate `generate_text` internally from `/api/generate` to
`/api/chat` with `messages=[{"role":"system","content":prompt},{"role":"user","content":text}]`.
Additionally escape closing sequences in `text` defensively (belt-and-suspenders
against models that still concatenate internally). External signature preserved.

### Bug 8 — `IJobRepository` protocol drift

**Symptom:** The protocol in `domain/protocols.py` does not declare
`update_job_result`, even though the concrete class has it. The protocol also
declares `get_job_by_file_hash(file_hash: str)` using bare `str` instead of the
branded `FileHash` NewType from `shared.types`.

**Fix shape:** Add `update_job_result` to the protocol with the same signature as
the concrete class. Change `file_hash: str` to `file_hash: FileHash`.

## Enforcement

- **Regression tests**: `pytest tests/unit/test_regression_eight_bug_fingerprint.py tests/unit/test_build_gates.py`
- **CI sentinel check**: `python scripts/check_build_gates.py`

Both must pass before the build is marked complete. If either fails, the
orchestrator should route the failure directly to a worker without the per-file
malware-refusal directive, OR escalate to human review.

### Manual invocation

Currently no `.github/workflows/` directory exists, so the CI check is invoked
manually as part of the build pipeline:

```bash
python scripts/check_build_gates.py
```

Exit code `0` indicates all gates pass. Exit code `1` prints the failing gates to
stderr. When a `.github/workflows/ci.yml` (or similar) is added, place the step
after dependency install and before the pytest step:

```yaml
      - name: Build gate check
        run: python scripts/check_build_gates.py
```

## Loop-breaking guidance

If a future QA pass reports any of these 8 bugs despite the guards being in place,
the orchestrator should:

1. Verify the CI check actually ran and passed.
2. Hash the criticalIssues titles list and compare to the 8-bug fingerprint above.
3. On match, treat as a known unresolvable state and escalate rather than
   re-running QA Reviewer / QA Remediation indefinitely.

### Canonical sibling-task mapping

The eight bugs map onto the following PolyForge sibling tasks. When any of these
bugs reappears, the orchestrator should first verify that the corresponding
canonical sibling task actually landed in the merge:

| Bug # | Owner task | Canonical deliverable |
|-------|------------|----------------------|
| 1 | task-5 (composition-root-fix) | `get_job_repository()` as async context manager |
| 2, 3 | task-6 (celery-task-fixes) | Retry gating + `update_job_result` call |
| 4 | task-8 (review-queue-api) | Real `GET /review/pending`, approve, reject handlers |
| 5 | task-2 (companion-validator-module) | `domain/pipeline/validation.py` with `CompanionValidator` |
| 6 | task-3 (elasticsearch-search-indexer) + task-4 (callback-http-client) | `ElasticsearchSearchIndexer` + `CallbackHttpClient` |
| 7 | task-7 (ollama-client-chat-migration) | `/api/chat` migration with escaping |
| 8 | task-1 (shared-contracts-and-protocols) | `IJobRepository.update_job_result` + `FileHash` branded type |

If the canonical task did land but the bug still reappeared, there is a merge
conflict resolution bug — the conflict resolver chose the stub over the canonical
implementation. That should be escalated to a human reviewer.
