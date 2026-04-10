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
- [Regression check](#regression-check)
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

## Regression check

The benchmark at `scripts/bench.py` answers "how fast is it?". The
regression check at `scripts/regression_check.py` answers "if I swap
the text model for something smaller, does the extraction quality
hold up?". Both matter on the T4 because its 16 GB VRAM cannot hold
`qwen2.5vl:7b` and `qwen2.5:7b` simultaneously at reasonable
quantization with room for KV cache and parallel slots — the text
model has to shrink.

### When to run it

Run the regression check **before** changing `OLLAMA_TEXT_MODEL` in
production, and re-run it whenever you change the Stage 1 extractor
prompts. The harness compares structured extraction output
(`drawing_number`, `title`, `document_type`) against a baseline run
using the current text model, so it catches prompt-regression
failures that a pure latency benchmark would miss entirely.

### Fallback ladder

The default candidate order is:

1. `qwen2.5:3b` — primary target. Frees ~3 GB VRAM vs the 7B,
   enables `OLLAMA_NUM_PARALLEL=2` for the vision model.
2. `llama3.2:3b-instruct` — alternate small model. Different
   tokenizer and training corpus, sometimes handles structured-
   output prompts differently than Qwen.
3. `phi3.5:3.8b` — last GPU-resident option before CPU fallback.
4. **CPU fallback**: `qwen2.5:7b` loaded on the CPU instead of the
   T4. Quality is preserved but Stage 1 text-mode latency roughly
   5–10× what it is on GPU. Do NOT adopt this silently — it is the
   operator's explicit escalation path when every 3B-class candidate
   fails the tolerance check.

The first candidate whose mean similarity clears `--tolerance`
(default `0.85`) is the winner. The harness stops iterating once it
finds a winner to conserve Ollama time.

### Basic usage

```bash
# Default: auto-discover corpus, baseline qwen2.5:7b, full ladder.
python scripts/regression_check.py

# Stricter tolerance for high-quality corpora.
python scripts/regression_check.py --tolerance 0.90

# Point at a real corpus and pin the report location.
python scripts/regression_check.py \
    --corpus /data/real_drawings \
    --output /tmp/regression_qwen2.5-3b.json

# Custom fallback ladder (e.g. after adding a new small model).
python scripts/regression_check.py \
    --candidates qwen2.5:3b,llama3.2:3b-instruct,phi3.5:3.8b

# Non-default baseline model for comparisons against a prior winner.
python scripts/regression_check.py --baseline-model qwen2.5:3b
```

### CLI reference

| Flag | Default | Meaning |
| --- | --- | --- |
| `--corpus PATH` | auto-discover (same as `bench.py`) | Directory of PDFs or single PDF file. |
| `--baseline-model TAG` | `qwen2.5:7b` | Reference text model. The candidate outputs are scored against this model's outputs. |
| `--candidates CSV` | `qwen2.5:3b,llama3.2:3b-instruct,phi3.5:3.8b` | Comma-separated ladder of candidate models to try in order. |
| `--cpu-fallback TAG` | `qwen2.5:7b` | Model the report recommends running on CPU if every GPU candidate fails. |
| `--output PATH` | `scripts/bench_results/regression_{timestamp}.json` | JSON report path. |
| `--tolerance FLOAT` | `0.85` | Corpus mean similarity threshold. First candidate >= this value wins. |

### How scoring works

For each document the harness compares three fields — `drawing_number`,
`title`, `document_type` — between the baseline and candidate runs.
Each field score is a normalized `difflib.SequenceMatcher.ratio()`
on lower-cased, whitespace-collapsed strings. The per-document score
is the mean of the three field scores. The candidate's corpus mean
similarity is the mean of every per-document score. A candidate is
classified as "pass" when its corpus mean similarity is greater than
or equal to `--tolerance`.

Two empty values (both `None` or both blank) score `1.0` — this is
the correct semantic for "the baseline model also returned nothing,
so the candidate did not regress".

### JSON report shape

```jsonc
{
  "schema_version": "1.0",
  "timestamp": "2026-04-09T12:34:56+00:00",
  "git_sha": "abc123...",
  "tolerance": 0.85,
  "cpu_fallback": "qwen2.5:7b",
  "baseline": {
    "model": "qwen2.5:7b",
    "corpus_size": 30,
    "mean_latency_seconds": 12.30,
    "docs": [
      { "path": "...", "drawing_number": "A-101",
        "title": "Floor Plan", "document_type": "drawing",
        "latency_seconds": 11.8, "ok": true, "error": null }
    ]
  },
  "candidates": [
    {
      "model": "qwen2.5:3b",
      "mean_similarity": 0.91,
      "mean_latency_seconds": 6.20,
      "latency_delta_pct": -49.6,
      "passed": true,
      "per_doc": [
        {
          "path": "...",
          "baseline": {"drawing_number": "...", "title": "...", "document_type": "..."},
          "candidate": {"drawing_number": "...", "title": "...", "document_type": "..."},
          "field_scores": {"drawing_number": 1.0, "title": 0.94, "document_type": 1.0},
          "overall_score": 0.98,
          "latency_seconds": 6.2
        }
      ]
    }
  ],
  "winner": "qwen2.5:3b",
  "recommendation": "adopt qwen2.5:3b"
}
```

### Operator action: CPU fallback

If the report's `winner` is `null` and the `recommendation` says
"fallback to CPU text-7B", the harness has exhausted every GPU
candidate. The ladder is designed so this is the final escalation
step and requires an Ollama reconfiguration that the harness does
NOT perform automatically:

1. Stop the Ollama process.
2. Set `OLLAMA_VISION_MODEL=qwen2.5vl:7b` and
   `OLLAMA_TEXT_MODEL=qwen2.5:7b` in the Ollama environment.
3. Configure the Ollama runtime so the vision model still runs on
   the T4 but the text model is pinned to CPU (Ollama's per-model
   `num_gpu=0` override is the canonical way to do this).
4. Restart Ollama and re-run the bench harness (`scripts/bench.py`)
   to measure the new steady-state latency — expect Stage 1
   text-mode latency to roughly 5–10× what it was on GPU.
5. Document the new SLA impact in your runbook.

If the CPU fallback is also unacceptable for your workload, the
remaining option is a hardware upgrade. See the
[Hardware scale-up notes](#hardware-scale-up-notes) section below.

### Exit codes

| Exit code | Meaning |
| --- | --- |
| `0` | At least one candidate passed. The report's `winner` field names it. |
| `1` | Every candidate failed. The report recommends CPU fallback. |
| `2` | Usage error — no corpus found, invalid `--tolerance`, empty `--candidates`, etc. |

This makes the harness usable in CI gates: exit `0` means the current
model ladder is safe to use; exit `1` means an operator must decide
whether to accept the CPU fallback or invest in hardware.

## Hardware scale-up notes

The repo runs entirely CPU-side — all heavy inference is delegated to
Ollama over HTTP. The single biggest performance lever is therefore
the hardware that Ollama is running on, not the repo itself. See the
top-level `README.md` "Hardware procurement" section for guidance on
which GPU to buy next. The repo-side env knobs above exist to let you
scale up **after** the hardware changes without editing code.
