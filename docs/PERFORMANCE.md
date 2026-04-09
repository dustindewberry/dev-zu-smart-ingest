# Performance

This document explains how to measure and interpret the extraction
performance of the Zubot Ingestion Service. It is intended for
operators tuning the single-box appliance deployment (Ollama + repo +
Postgres + Redis + Elasticsearch + ChromaDB co-located on one host).

## Contents

- [Running the benchmark](#running-the-benchmark)
- [Environment variables that affect results](#environment-variables-that-affect-results)
- [Interpreting the JSON report](#interpreting-the-json-report)
- [Comparing runs](#comparing-runs)
- [Hardware scale-up notes](#hardware-scale-up-notes)

## Running the benchmark

The benchmark harness lives at `scripts/bench.py`. It measures the
wall-clock performance of `ExtractionOrchestrator.run_pipeline()` by
invoking the same composition root (`build_orchestrator()`) that the
Celery worker uses — it does **not** re-implement the pipeline, start
Celery, or touch Postgres/Redis. Ollama must already be running at the
configured `OLLAMA_HOST` URL.

### Prerequisites

1. Ollama running with both models loaded:

   ```bash
   curl -sS "$OLLAMA_HOST/api/tags" | jq
   ```

2. Python virtualenv with the project installed (so
   `zubot_ingestion` is importable and PyMuPDF / json_repair are
   available).

3. A corpus of PDFs. If you do not pass `--corpus`, the harness will
   auto-discover fixtures under `tests/fixtures/**/*.pdf`,
   `tests/data/**/*.pdf`, and `tests/**/*.pdf`. If none are found the
   harness exits with a non-zero status and instructs you to pass
   `--corpus`.

### Basic usage

```bash
# Sequential baseline run against auto-discovered fixtures.
python scripts/bench.py

# Point at a real corpus and iterate three times for stable percentiles.
python scripts/bench.py --corpus /data/real_drawings --iterations 3

# Warm up Ollama with a single dummy run before measuring.
python scripts/bench.py --warmup 1

# Process two documents at a time via asyncio.gather (not Celery).
python scripts/bench.py --concurrency 2

# Pin the output file location.
python scripts/bench.py --output /tmp/bench_baseline.json
```

### CLI reference

| Flag | Default | Meaning |
| --- | --- | --- |
| `--corpus PATH` | auto-discover | Directory of PDFs or single PDF file. |
| `--iterations N` | `1` | How many times to process the full corpus. |
| `--concurrency N` | `1` | Docs processed concurrently via `asyncio.gather`. |
| `--warmup N` | `0` | Warm-up runs (discarded) before measurement starts. |
| `--output PATH` | `scripts/bench_results/bench_{timestamp}.json` | JSON report path. |

Concurrency is implemented with `asyncio.gather` in fixed-size batches
inside the benchmark process. It does **not** dispatch via Celery — the
harness is intentionally standalone so you can measure the pipeline in
isolation from the queue/worker layer.

## Environment variables that affect results

The following variables are read by the service at process start and
are snapshotted into every report under the `"settings"` block. Keep
them constant across runs you intend to compare.

### Ollama / inference

| Variable | Effect |
| --- | --- |
| `OLLAMA_HOST` | URL of the inference server. Usually `http://ollama:11434`. |
| `OLLAMA_VISION_MODEL` | Model tag used for Stage 1 and Stage 2 vision calls (e.g. `qwen2.5vl:7b`). |
| `OLLAMA_TEXT_MODEL` | Model tag used for Stage 1 text-mode extraction (e.g. `qwen2.5:3b`). |
| `OLLAMA_NUM_PARALLEL` | Number of parallel request slots Ollama itself exposes. Must match the server-side setting to realize any concurrency gains. |
| `OLLAMA_KEEP_ALIVE` | How long Ollama keeps the model resident between calls. Set to `24h` for benchmarks so model load latency does not skew results. |

### Celery

| Variable | Effect |
| --- | --- |
| `CELERY_WORKER_CONCURRENCY` | Only relevant for production throughput — the benchmark bypasses Celery, but the snapshot still records it so reports can be correlated with production runs. |

### Companion page heuristic

| Variable | Effect |
| --- | --- |
| `COMPANION_SKIP_ENABLED` | When `true`, Stage 2 will skip companion vision calls on text-dominant pages. Largest single latency lever on a T4 with `num_parallel=1`. |
| `COMPANION_SKIP_MIN_WORDS` | Minimum word count on a page to trigger the skip. Higher = fewer skips, higher quality, lower throughput. |

### PDF / observability

| Variable | Effect |
| --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTEL collector URL. Set to empty string for a hermetic bench run to avoid exporter overhead. |
| `TEST_PDF_DIR` | Fallback PDF directory honored by integration tests; benchmark honors `--corpus` instead. |

## Interpreting the JSON report

Each invocation writes a report to
`scripts/bench_results/bench_{timestamp}.json` (or the path passed to
`--output`). The report has the following shape:

```jsonc
{
  "schema_version": "1.0",
  "timestamp": "2026-04-09T12:34:56+00:00",
  "git_sha": "abc123...",         // or "unknown"
  "settings": {
    "OLLAMA_NUM_PARALLEL": 2,
    "OLLAMA_TEXT_MODEL": "qwen2.5:3b",
    "OLLAMA_VISION_MODEL": "qwen2.5vl:7b",
    "CELERY_WORKER_CONCURRENCY": 2,
    "COMPANION_SKIP_ENABLED": true,
    "COMPANION_SKIP_MIN_WORDS": 40
  },
  "args": {
    "corpus": "/data/real_drawings",
    "iterations": 3,
    "concurrency": 1,
    "warmup": 1,
    "output": "/abs/path/to/report.json"
  },
  "per_doc": [
    {
      "path": "/data/real_drawings/0001.pdf",
      "filename": "0001.pdf",
      "iteration": 0,
      "ok": true,
      "error": null,
      "total_seconds": 12.34,
      "confidence_tier": "auto",
      "stages": {
        "pdf_load": 0.08,
        "stage1": 4.50,
        "stage2": 5.10,
        "stage3": 0.02,
        "metadata_write": 0.15,
        "search_index": 0.00,
        "callback": 0.00
      }
    }
  ],
  "aggregate": {
    "doc_count": 30,
    "success_count": 30,
    "failure_count": 0,
    "failure_rate": 0.0,
    "throughput_docs_per_minute": 4.86,
    "measured_wall_seconds": 370.5,
    "latency_seconds": {
      "p50": 11.80,
      "p95": 18.90,
      "p99": 22.10,
      "mean": 12.30,
      "min": 9.10,
      "max": 22.80
    },
    "stage_seconds": {
      "stage1": {"mean": 4.50, "max": 6.20, "count": 30},
      "stage2": {"mean": 5.10, "max": 8.70, "count": 30}
    }
  }
}
```

### Stage-name mapping

The harness preserves the orchestrator's native stage keys
(`pdf_load`, `stage1`, `stage2`, `companion_validation`, `stage3`,
`confidence`, `metadata_write`, `search_index`, `callback`) in the
JSON report. For operator-facing summaries, the numeric aliases are:

| Trace key | Alias | What it measures |
| --- | --- | --- |
| `stage1` | Stage 1 | Parallel drawing-number / title / document-type extraction. |
| `stage2` | Stage 2 | Companion page generation (vision). |
| `stage3` | Stage 3 | Sidecar document assembly. |
| `metadata_write` | Stage 4 | ChromaDB metadata write. |
| `search_index` | Stage 5 | Elasticsearch indexing. |
| `callback` | Stage 6 | Outbound webhook delivery. |

### Which numbers to watch

- **`latency_seconds.p95`** — tail latency for single-document SLAs. The most important number if you're optimizing for "no user waits more than N seconds".
- **`throughput_docs_per_minute`** — steady-state throughput. The most important number if you're optimizing for batch ingestion.
- **`stage_seconds.stage1.mean` + `stage_seconds.stage2.mean`** — these dominate total latency; everything else is single-digit milliseconds.
- **`failure_rate`** — anything above zero on a clean corpus means Ollama is dropping requests or timing out.

## Comparing runs

The `scripts/regression_check.py` tool (owned by task-3, expected at
`scripts/regression_check.py`) is the canonical way to compare two
bench reports. It diffs p50/p95/p99 and throughput between a baseline
and a candidate report and fails loudly on regression.

Rules of thumb for manual comparison:

1. Hold all env vars in the `settings` block constant.
2. Use `--warmup 1` or higher on every run so cold-start latency does not dominate small corpora.
3. Prefer `--iterations 3` over single-shot runs — a single run has too much noise for useful percentile estimates.
4. Compare `git_sha` values. A regression between two different SHAs is only meaningful if the env settings are identical.

## Hardware scale-up notes

The repo runs entirely CPU-side — all heavy inference is delegated to
Ollama over HTTP. The single biggest performance lever is therefore
the hardware that Ollama is running on, not the repo itself. See the
top-level `README.md` "Hardware procurement" section for guidance on
which GPU to buy next. The repo-side env knobs above exist to let you
scale up **after** the hardware changes without editing code.
