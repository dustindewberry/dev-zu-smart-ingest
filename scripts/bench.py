#!/usr/bin/env python
"""End-to-end extraction benchmark harness for the Zubot Ingestion Service.

Measures the wall-clock performance of :class:`ExtractionOrchestrator`
against a corpus of PDFs. The harness is purely additive: it does not
modify any runtime code and invokes the pipeline through the same
composition root (:func:`build_orchestrator`) the Celery worker uses.

Usage (also documented in ``docs/PERFORMANCE.md``)::

    python scripts/bench.py                              # auto-discover PDFs
    python scripts/bench.py --corpus path/to/pdfs
    python scripts/bench.py --iterations 3 --warmup 1
    python scripts/bench.py --concurrency 2 --output /tmp/out.json

The harness assumes Ollama is already running at ``settings.OLLAMA_HOST``
— it does not start, stop, or health-check the inference service.

JSON report schema (see ``docs/PERFORMANCE.md`` for prose docs)::

    {
      "schema_version": "1.0",
      "timestamp": "2026-04-09T12:34:56+00:00",
      "git_sha": "abc123..." | "unknown",
      "settings": {
        "OLLAMA_TEXT_MODEL": str | null,
        "OLLAMA_VISION_MODEL": str | null,
        "CELERY_WORKER_CONCURRENCY": int | null,
        "COMPANION_SKIP_ENABLED": bool | null,
        "COMPANION_SKIP_MIN_WORDS": int | null
      },
      "args": {...},
      "per_doc": [
        {
          "path": "/abs/path.pdf",
          "filename": "foo.pdf",
          "iteration": 0,
          "ok": true,
          "error": null,
          "total_seconds": 12.34,
          "confidence_tier": "auto" | "spot" | "review" | null,
          "stages": {"stage1": 4.5, "stage2": 2.1, ...}
        }
      ],
      "aggregate": {
        "doc_count": int,
        "success_count": int,
        "failure_count": int,
        "failure_rate": float,
        "throughput_docs_per_minute": float,
        "latency_seconds": {"p50": float, "p95": float, "p99": float,
                            "mean": float, "min": float, "max": float},
        "stage_seconds": {
          "stage1": {"mean": float, "max": float, "count": int},
          ...
        }
      }
    }
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence
from uuid import uuid4

# Default output directory for JSON reports. Kept under scripts/ so it
# ships with the harness and can be gitignored without touching repo-
# level ignore patterns for the tests directory.
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "bench_results"

# Corpus discovery roots relative to the repo root. The first match wins
# only in the sense that results are merged — every directory glob is
# searched and deduplicated.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DISCOVERY_GLOBS: tuple[tuple[Path, str], ...] = (
    (_REPO_ROOT / "tests" / "fixtures", "**/*.pdf"),
    (_REPO_ROOT / "tests" / "data", "**/*.pdf"),
    (_REPO_ROOT / "tests", "**/*.pdf"),
)

# Map orchestrator pipeline_trace stage keys to Stage 1..6 labels. The
# orchestrator uses descriptive names internally (stage1/stage2/stage3/
# metadata_write/search_index/callback); we also surface the numeric
# Stage N alias for operator-facing summaries.
STAGE_TRACE_KEYS: tuple[str, ...] = (
    "pdf_load",
    "stage1",
    "stage2",
    "companion_validation",
    "stage3",
    "confidence",
    "metadata_write",
    "search_index",
    "callback",
)

STAGE_NUMBER_ALIAS: dict[str, str] = {
    "stage1": "Stage 1",
    "stage2": "Stage 2",
    "stage3": "Stage 3",
    "metadata_write": "Stage 4",
    "search_index": "Stage 5",
    "callback": "Stage 6",
}

SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------


@dataclass
class PerDocRecord:
    """One row in the benchmark's per-doc results table."""

    path: str
    filename: str
    iteration: int
    ok: bool
    error: str | None
    total_seconds: float
    confidence_tier: str | None
    stages: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Corpus discovery
# ---------------------------------------------------------------------------


def discover_corpus(
    corpus_arg: str | None,
    *,
    discovery_globs: Sequence[tuple[Path, str]] | None = None,
) -> list[Path]:
    """Resolve the benchmark corpus.

    * If ``corpus_arg`` is a directory, every ``*.pdf`` beneath it is
      returned (recursively).
    * If ``corpus_arg`` is a file, that single file is returned.
    * If ``corpus_arg`` is None, we auto-discover fixtures under the
      repo's tests tree using :data:`_DISCOVERY_GLOBS`.

    Returns a sorted, de-duplicated list. An empty list is returned
    when no PDFs were found; the caller is responsible for emitting
    the user-facing error message and exiting non-zero.
    """
    globs = tuple(discovery_globs) if discovery_globs is not None else _DISCOVERY_GLOBS

    if corpus_arg:
        corpus_path = Path(corpus_arg).expanduser().resolve()
        if not corpus_path.exists():
            return []
        if corpus_path.is_file():
            return [corpus_path] if corpus_path.suffix.lower() == ".pdf" else []
        # Directory: recursive PDF discovery.
        return sorted({p for p in corpus_path.rglob("*.pdf") if p.is_file()})

    # Auto-discovery fallback.
    found: set[Path] = set()
    for root, pattern in globs:
        if not root.exists():
            continue
        for match in root.glob(pattern):
            if match.is_file() and match.suffix.lower() == ".pdf":
                found.add(match)
    return sorted(found)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser.

    Exposed as a module-level function so unit tests can exercise it
    without having to run the main entrypoint.
    """
    parser = argparse.ArgumentParser(
        prog="bench.py",
        description=(
            "Measure end-to-end extraction performance of the "
            "ExtractionOrchestrator against a corpus of PDFs."
        ),
    )
    parser.add_argument(
        "--corpus",
        type=str,
        default=None,
        help=(
            "Path to a directory of PDFs or a single PDF file. If omitted "
            "the harness auto-discovers test fixtures under tests/fixtures, "
            "tests/data, and tests/."
        ),
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1,
        help=(
            "Number of times to process the full corpus. Default 1. "
            "Use >1 for more stable percentile estimates."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help=(
            "Number of documents to process concurrently via asyncio.gather. "
            "Default 1 (sequential baseline). Does NOT use Celery."
        ),
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=0,
        help=(
            "Number of warm-up runs before measurement begins, to avoid "
            "first-call latency skew from model load. Default 0."
        ),
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help=(
            "Path to the JSON report output file. Default "
            "scripts/bench_results/bench_{timestamp}.json."
        ),
    )
    return parser


def default_output_path(now: datetime | None = None) -> Path:
    """Compute the default JSON report path under scripts/bench_results/."""
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_DIR / f"bench_{ts}.json"


# ---------------------------------------------------------------------------
# Settings snapshot + git SHA
# ---------------------------------------------------------------------------


def snapshot_settings() -> dict[str, Any]:
    """Capture the perf-tuning settings fields for the report.

    Uses ``getattr(settings, FIELD, None)`` everywhere so the harness
    works whether or not the task-1 perf-tuning fields have landed in
    :class:`Settings`. Missing fields surface as ``null`` in the JSON
    report, which is the correct semantic for 'unconfigured'.
    """
    try:
        from zubot_ingestion.config import get_settings

        settings = get_settings()
    except Exception:  # noqa: BLE001 - tolerate any import-time error
        return {
            "OLLAMA_TEXT_MODEL": None,
            "OLLAMA_VISION_MODEL": None,
            "CELERY_WORKER_CONCURRENCY": None,
            "COMPANION_SKIP_ENABLED": None,
            "COMPANION_SKIP_MIN_WORDS": None,
        }

    return {
        "OLLAMA_TEXT_MODEL": getattr(settings, "OLLAMA_TEXT_MODEL", None),
        "OLLAMA_VISION_MODEL": getattr(settings, "OLLAMA_VISION_MODEL", None),
        "CELERY_WORKER_CONCURRENCY": getattr(
            settings, "CELERY_WORKER_CONCURRENCY", None
        ),
        "COMPANION_SKIP_ENABLED": getattr(settings, "COMPANION_SKIP_ENABLED", None),
        "COMPANION_SKIP_MIN_WORDS": getattr(
            settings, "COMPANION_SKIP_MIN_WORDS", None
        ),
    }


def get_git_sha() -> str:
    """Return HEAD SHA via ``git rev-parse`` or 'unknown' on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
    except Exception:  # noqa: BLE001 - git may be missing, non-fatal
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Job construction + pipeline invocation
# ---------------------------------------------------------------------------


def _make_synthetic_job(pdf_path: Path) -> Any:
    """Construct a minimal in-memory :class:`Job` for the pipeline.

    The orchestrator only needs a few fields (job_id, batch_id, filename,
    file_hash, file_path) to run. We synthesize everything else with
    sensible placeholders. No database row is created or persisted.
    """
    # Lazy import so unit tests that mock the orchestrator don't pay
    # the entity import cost.
    from hashlib import sha256

    from zubot_ingestion.domain.entities import Job
    from zubot_ingestion.domain.enums import JobStatus
    from zubot_ingestion.shared.types import BatchId, FileHash, JobId

    pdf_bytes = pdf_path.read_bytes()
    digest = sha256(pdf_bytes).hexdigest()
    now = datetime.now(timezone.utc)

    return Job(
        job_id=JobId(uuid4()),
        batch_id=BatchId(uuid4()),
        filename=pdf_path.name,
        file_hash=FileHash(digest),
        file_path=str(pdf_path),
        status=JobStatus.QUEUED,
        result=None,
        error_message=None,
        pipeline_trace=None,
        otel_trace_id=None,
        processing_time_ms=None,
        created_at=now,
        updated_at=now,
    )


def _extract_stage_seconds(pipeline_trace: dict[str, Any]) -> dict[str, float]:
    """Pull per-stage duration (seconds) out of a PipelineResult trace.

    The orchestrator records stages as
    ``pipeline_trace['stages'][NAME] = {'duration_ms': int, ...}``.
    Missing stages are simply omitted.
    """
    stages: dict[str, float] = {}
    stages_block = pipeline_trace.get("stages") if pipeline_trace else None
    if not isinstance(stages_block, dict):
        return stages
    for key in STAGE_TRACE_KEYS:
        entry = stages_block.get(key)
        if isinstance(entry, dict) and "duration_ms" in entry:
            try:
                stages[key] = float(entry["duration_ms"]) / 1000.0
            except (TypeError, ValueError):
                continue
    return stages


async def _run_single(
    orchestrator: Any,
    pdf_path: Path,
    iteration: int,
) -> PerDocRecord:
    """Invoke ``orchestrator.run_pipeline`` for a single PDF and record metrics.

    Exceptions raised by the orchestrator are captured in the returned
    record — they do not abort the benchmark run.
    """
    started = time.perf_counter()
    try:
        job = _make_synthetic_job(pdf_path)
        pdf_bytes = pdf_path.read_bytes()
        result = await orchestrator.run_pipeline(job, pdf_bytes)
    except Exception as exc:  # noqa: BLE001 - benchmark keeps going
        elapsed = time.perf_counter() - started
        return PerDocRecord(
            path=str(pdf_path),
            filename=pdf_path.name,
            iteration=iteration,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            total_seconds=elapsed,
            confidence_tier=None,
        )

    elapsed = time.perf_counter() - started

    # Confidence tier may live either on the assessment or on the
    # extraction_result; fall back gracefully if neither has it.
    tier_value: str | None = None
    assessment = getattr(result, "confidence_assessment", None)
    if assessment is not None:
        tier = getattr(assessment, "tier", None)
        if tier is not None:
            tier_value = getattr(tier, "value", str(tier))
    if tier_value is None:
        extraction = getattr(result, "extraction_result", None)
        if extraction is not None:
            tier = getattr(extraction, "confidence_tier", None)
            if tier is not None:
                tier_value = getattr(tier, "value", str(tier))

    trace = getattr(result, "pipeline_trace", None) or {}
    return PerDocRecord(
        path=str(pdf_path),
        filename=pdf_path.name,
        iteration=iteration,
        ok=True,
        error=None,
        total_seconds=elapsed,
        confidence_tier=tier_value,
        stages=_extract_stage_seconds(trace),
    )


async def _run_batch(
    orchestrator: Any,
    pdf_paths: Sequence[Path],
    iteration: int,
    concurrency: int,
) -> list[PerDocRecord]:
    """Run a list of PDFs with up to ``concurrency`` in-flight at once.

    Concurrency is implemented as ``asyncio.gather`` over fixed-size
    slices of the input list. This keeps batch boundaries obvious in
    logs while giving the operator a tight concurrency knob.
    """
    if concurrency < 1:
        concurrency = 1
    results: list[PerDocRecord] = []
    for i in range(0, len(pdf_paths), concurrency):
        chunk = pdf_paths[i : i + concurrency]
        batch = await asyncio.gather(
            *(_run_single(orchestrator, p, iteration) for p in chunk)
        )
        results.extend(batch)
    return results


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile helper.

    ``q`` is a fraction in ``[0, 1]``. Returns ``0.0`` on empty input
    to keep the JSON report shape stable. Uses the nearest-rank method
    rather than linear interpolation because the sample sizes in a
    benchmark run are typically small (tens of docs) and nearest-rank
    is easier to reason about.
    """
    if not sorted_values:
        return 0.0
    if q <= 0:
        return float(sorted_values[0])
    if q >= 1:
        return float(sorted_values[-1])
    rank = math.ceil(q * len(sorted_values))
    idx = max(0, min(len(sorted_values) - 1, rank - 1))
    return float(sorted_values[idx])


def aggregate_records(
    records: Sequence[PerDocRecord],
    *,
    measured_wall_seconds: float,
) -> dict[str, Any]:
    """Compute aggregate latency/throughput stats from per-doc records."""
    doc_count = len(records)
    success = [r for r in records if r.ok]
    failure_count = doc_count - len(success)
    latencies = sorted(r.total_seconds for r in success)

    if latencies:
        latency_stats = {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
            "mean": statistics.fmean(latencies),
            "min": latencies[0],
            "max": latencies[-1],
        }
    else:
        latency_stats = {
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "mean": 0.0,
            "min": 0.0,
            "max": 0.0,
        }

    # Per-stage aggregation: mean and max seconds across all records
    # that reported a duration for that stage.
    stage_seconds: dict[str, dict[str, float]] = {}
    for stage_key in STAGE_TRACE_KEYS:
        values = [r.stages[stage_key] for r in success if stage_key in r.stages]
        if not values:
            continue
        stage_seconds[stage_key] = {
            "mean": statistics.fmean(values),
            "max": max(values),
            "count": len(values),
        }

    if measured_wall_seconds > 0 and len(success) > 0:
        throughput = len(success) * 60.0 / measured_wall_seconds
    else:
        throughput = 0.0

    return {
        "doc_count": doc_count,
        "success_count": len(success),
        "failure_count": failure_count,
        "failure_rate": (failure_count / doc_count) if doc_count else 0.0,
        "throughput_docs_per_minute": throughput,
        "measured_wall_seconds": measured_wall_seconds,
        "latency_seconds": latency_stats,
        "stage_seconds": stage_seconds,
    }


def build_report(
    *,
    args_dict: dict[str, Any],
    records: Sequence[PerDocRecord],
    measured_wall_seconds: float,
    settings_snapshot: dict[str, Any],
    git_sha: str,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the full JSON report dict."""
    ts = (timestamp or datetime.now(timezone.utc)).isoformat()
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": ts,
        "git_sha": git_sha,
        "settings": settings_snapshot,
        "args": args_dict,
        "per_doc": [asdict(r) for r in records],
        "aggregate": aggregate_records(
            records, measured_wall_seconds=measured_wall_seconds
        ),
    }


# ---------------------------------------------------------------------------
# Human-readable summary
# ---------------------------------------------------------------------------


def format_summary(report: dict[str, Any]) -> str:
    """Render a concise human-readable summary of a report dict."""
    lines: list[str] = []
    agg = report["aggregate"]
    lines.append("=" * 60)
    lines.append("Zubot Ingestion Benchmark Summary")
    lines.append("=" * 60)
    lines.append(f"timestamp:            {report['timestamp']}")
    lines.append(f"git_sha:              {report['git_sha']}")
    lines.append(f"documents:            {agg['doc_count']}")
    lines.append(f"successes:            {agg['success_count']}")
    lines.append(f"failures:             {agg['failure_count']}")
    lines.append(f"failure_rate:         {agg['failure_rate']:.2%}")
    lines.append(
        f"wall_seconds:         {agg['measured_wall_seconds']:.3f}"
    )
    lines.append(
        f"throughput (docs/min):{agg['throughput_docs_per_minute']:10.2f}"
    )
    lat = agg["latency_seconds"]
    lines.append("latency (seconds):")
    lines.append(
        f"  p50={lat['p50']:.3f}  p95={lat['p95']:.3f}  p99={lat['p99']:.3f}"
    )
    lines.append(
        f"  mean={lat['mean']:.3f}  min={lat['min']:.3f}  max={lat['max']:.3f}"
    )
    if agg["stage_seconds"]:
        lines.append("per-stage seconds (mean / max):")
        for stage_key, stats in agg["stage_seconds"].items():
            alias = STAGE_NUMBER_ALIAS.get(stage_key, "")
            label = f"{stage_key}" + (f" ({alias})" if alias else "")
            lines.append(
                f"  {label:30s} mean={stats['mean']:.3f}  max={stats['max']:.3f}  n={stats['count']}"
            )
    lines.append("settings snapshot:")
    for k, v in report["settings"].items():
        lines.append(f"  {k}: {v}")
    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


async def run_benchmark(
    *,
    corpus_paths: Sequence[Path],
    iterations: int,
    concurrency: int,
    warmup: int,
    orchestrator_factory: Callable[[], Awaitable[Any] | Any] | None = None,
) -> tuple[list[PerDocRecord], float]:
    """Execute the benchmark and return per-doc records + measured wall time.

    ``orchestrator_factory`` is injectable so unit tests can pass a
    mocked factory without patching :mod:`zubot_ingestion.services`.
    When ``None`` we import :func:`build_orchestrator` lazily so the
    bench module stays importable in environments that lack the full
    runtime dependency graph.
    """
    if orchestrator_factory is None:

        def _default_factory() -> Any:
            from zubot_ingestion.services import build_orchestrator

            return build_orchestrator()

        orchestrator_factory = _default_factory

    maybe = orchestrator_factory()
    if asyncio.iscoroutine(maybe):
        orchestrator = await maybe
    else:
        orchestrator = maybe

    # Warm-up phase: N runs against the head of the corpus, results
    # discarded. Cycles through the corpus if N > len(corpus).
    if warmup > 0 and corpus_paths:
        warmup_slice = [
            corpus_paths[i % len(corpus_paths)] for i in range(warmup)
        ]
        await _run_batch(
            orchestrator, warmup_slice, iteration=-1, concurrency=concurrency
        )

    # Measured phase.
    all_records: list[PerDocRecord] = []
    measured_start = time.perf_counter()
    for it in range(iterations):
        batch_records = await _run_batch(
            orchestrator, list(corpus_paths), iteration=it, concurrency=concurrency
        )
        all_records.extend(batch_records)
    measured_wall = time.perf_counter() - measured_start

    return all_records, measured_wall


def _write_report(path: Path, report: dict[str, Any]) -> None:
    """Persist the JSON report, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def main(argv: Iterable[str] | None = None) -> int:
    """CLI entrypoint. Returns a process exit code."""
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    corpus_paths = discover_corpus(args.corpus)
    if not corpus_paths:
        sys.stderr.write(
            "bench: no PDFs found.\n"
            "  auto-discovery searched tests/fixtures/**/*.pdf, "
            "tests/data/**/*.pdf, and tests/**/*.pdf.\n"
            "  pass --corpus PATH to point at a directory or single PDF.\n"
        )
        return 2

    if args.iterations < 1:
        sys.stderr.write("bench: --iterations must be >= 1\n")
        return 2
    if args.concurrency < 1:
        sys.stderr.write("bench: --concurrency must be >= 1\n")
        return 2
    if args.warmup < 0:
        sys.stderr.write("bench: --warmup must be >= 0\n")
        return 2

    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else default_output_path()
    )

    settings_snapshot = snapshot_settings()
    git_sha = get_git_sha()

    sys.stdout.write(
        f"bench: corpus={len(corpus_paths)} docs  "
        f"iterations={args.iterations}  concurrency={args.concurrency}  "
        f"warmup={args.warmup}\n"
    )
    sys.stdout.flush()

    records, measured_wall = asyncio.run(
        run_benchmark(
            corpus_paths=corpus_paths,
            iterations=args.iterations,
            concurrency=args.concurrency,
            warmup=args.warmup,
        )
    )

    report = build_report(
        args_dict={
            "corpus": str(args.corpus) if args.corpus else None,
            "iterations": args.iterations,
            "concurrency": args.concurrency,
            "warmup": args.warmup,
            "output": str(output_path),
        },
        records=records,
        measured_wall_seconds=measured_wall,
        settings_snapshot=settings_snapshot,
        git_sha=git_sha,
    )

    _write_report(output_path, report)
    sys.stdout.write(format_summary(report) + "\n")
    sys.stdout.write(f"wrote report: {output_path}\n")
    sys.stdout.flush()

    # Non-zero exit if every document failed — useful for CI gating.
    if report["aggregate"]["doc_count"] > 0 and report["aggregate"]["success_count"] == 0:
        return 1
    return 0


if __name__ == "__main__":
    # Ensure the repo's ``src`` directory is importable when run as a
    # plain script from a checkout that hasn't ``pip install``-ed the
    # package. Mirrors the pytest.ini ``pythonpath = src`` setting.
    _SRC = _REPO_ROOT / "src"
    if _SRC.exists() and str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    raise SystemExit(main())
