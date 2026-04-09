#!/usr/bin/env python
"""Text-model regression check harness for the Zubot Ingestion Service.

Runs a corpus of PDFs through :class:`ExtractionOrchestrator` with a
baseline text model, then re-runs it with each candidate in a fallback
ladder to measure how closely the candidate's structured outputs match
the baseline. The first candidate whose corpus-mean similarity meets
the ``--tolerance`` threshold is declared the winner.

This exists because the Tesla T4's 16 GB VRAM cannot hold both
``qwen2.5vl:7b`` and ``qwen2.5:7b`` at reasonable quantization with
room for KV cache and parallel slots. The operator plan therefore
downgrades the text model to a 3B-class model while keeping the
vision model at 7B — but the downgrade must be proven safe on real
extraction output, not just accepted on faith. This script is that
proof.

Fallback ladder (order matters — first pass wins)::

    qwen2.5:3b  ->  llama3.2:3b-instruct  ->  phi3.5:3.8b
                                    (if all fail)
                                           v
                            CPU-resident qwen2.5:7b
                            (operator action documented
                             in docs/PERFORMANCE.md)

Usage::

    python scripts/regression_check.py                          # auto-discover corpus
    python scripts/regression_check.py --corpus path/to/pdfs
    python scripts/regression_check.py --tolerance 0.90
    python scripts/regression_check.py --output /tmp/reg.json
    python scripts/regression_check.py \\
        --baseline-model qwen2.5:7b \\
        --candidates qwen2.5:3b,llama3.2:3b-instruct,phi3.5:3.8b \\
        --cpu-fallback qwen2.5:7b

Model selection is driven entirely by the ``OLLAMA_TEXT_MODEL``
environment variable, which is set BEFORE :func:`build_orchestrator`
is invoked for each run. The harness never patches
:class:`zubot_ingestion.config.Settings` at import time and never
touches :mod:`zubot_ingestion.services.orchestrator`, the Celery app,
or the Ollama client — it is purely additive.

JSON report schema (see ``docs/PERFORMANCE.md`` "Regression check"
section for prose docs)::

    {
      "schema_version": "1.0",
      "timestamp": "...",
      "git_sha": "...",
      "args": {...},
      "tolerance": 0.85,
      "baseline": {
        "model": "qwen2.5:7b",
        "corpus_size": int,
        "mean_latency_seconds": float,
        "docs": [{"path": ..., "drawing_number": ..., "title": ...,
                  "document_type": ..., "latency_seconds": ...}]
      },
      "candidates": [
        {
          "model": "qwen2.5:3b",
          "mean_similarity": 0.91,
          "latency_delta_pct": -42.3,
          "passed": true,
          "per_doc": [
            {
              "path": ...,
              "baseline": {"drawing_number": ..., "title": ..., "document_type": ...},
              "candidate": {"drawing_number": ..., "title": ..., "document_type": ...},
              "field_scores": {"drawing_number": 1.0, "title": 0.94, "document_type": 1.0},
              "overall_score": 0.98,
              "latency_seconds": 6.2
            }
          ]
        }
      ],
      "winner": "qwen2.5:3b" | null,
      "recommendation": "adopt qwen2.5:3b" | "fallback to CPU text-7B" | ...
    }
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import importlib.util
import json
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

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1.0"

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Default output directory for JSON reports. Mirrors scripts/bench.py.
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "bench_results"

# The environment variable the harness toggles between runs. Setting
# it BEFORE ``build_orchestrator()`` is invoked causes every
# downstream extractor (drawing number / title / document type) to
# pick up the new model name at construction time. The harness never
# patches ``Settings`` at import time.
OLLAMA_TEXT_MODEL_ENV = "OLLAMA_TEXT_MODEL"

# Default fallback ladder. Order matters: the first candidate whose
# mean similarity meets the tolerance threshold is the winner. The
# CPU fallback is separate from the candidate list because adopting
# it is an operator action (reconfigure Ollama to run the text model
# on CPU), not a runtime switch we can flip from this script.
DEFAULT_BASELINE_MODEL = "qwen2.5:7b"
DEFAULT_CANDIDATES: tuple[str, ...] = (
    "qwen2.5:3b",
    "llama3.2:3b-instruct",
    "phi3.5:3.8b",
)
DEFAULT_CPU_FALLBACK = "qwen2.5:7b"

# Similarity tolerance for pass/fail gating. Mean of the three field
# scores (drawing_number, title, document_type) across the entire
# corpus must be >= this value for a candidate to pass.
DEFAULT_TOLERANCE = 0.85

# The three fields we compare between baseline and candidate. Keep
# this tuple ordered so tests and stdout tables are deterministic.
COMPARED_FIELDS: tuple[str, ...] = (
    "drawing_number",
    "title",
    "document_type",
)


# ---------------------------------------------------------------------------
# discover_corpus: delegate to scripts/bench.py
# ---------------------------------------------------------------------------
#
# The task spec explicitly forbids duplicating the auto-discover logic
# that already lives in :mod:`scripts.bench`. ``scripts/`` is not on
# ``sys.path`` by default (pytest config only adds ``src``), so we
# load bench.py via ``importlib.util.spec_from_file_location`` and
# pull the helper off the resulting module. This keeps both scripts
# as stand-alone executables while still sharing one corpus-discovery
# implementation.


def _load_bench_module() -> Any:
    """Import ``scripts/bench.py`` as a module, regardless of CWD.

    Returns the loaded module object. Raises ``ModuleNotFoundError``
    if the file is missing — a clear signal that task-2 has not
    landed yet and the operator needs to investigate.
    """
    bench_path = _REPO_ROOT / "scripts" / "bench.py"
    if not bench_path.exists():
        raise ModuleNotFoundError(
            f"scripts/bench.py not found at {bench_path}. "
            "regression_check.py depends on bench.py's discover_corpus "
            "helper (task-2). Ensure task-2 has landed in the worktree."
        )
    spec = importlib.util.spec_from_file_location(
        "zubot_bench_for_regression_check", str(bench_path)
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"Could not build import spec for {bench_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("zubot_bench_for_regression_check", module)
    spec.loader.exec_module(module)
    return module


def discover_corpus(corpus_arg: str | None) -> list[Path]:
    """Resolve the regression-check corpus via bench.py's helper.

    Thin wrapper around :func:`scripts.bench.discover_corpus` so the
    caller does not have to know about the lazy import dance.
    """
    bench = _load_bench_module()
    return list(bench.discover_corpus(corpus_arg))


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------


@dataclass
class DocOutputs:
    """Captured structured output for one document under one model."""

    path: str
    filename: str
    drawing_number: str | None
    title: str | None
    document_type: str | None
    latency_seconds: float
    ok: bool = True
    error: str | None = None

    def as_fields(self) -> dict[str, str | None]:
        """Return the three compared fields as a plain dict."""
        return {
            "drawing_number": self.drawing_number,
            "title": self.title,
            "document_type": self.document_type,
        }


@dataclass
class DocComparison:
    """Per-doc comparison of a candidate against the baseline."""

    path: str
    filename: str
    baseline: dict[str, str | None]
    candidate: dict[str, str | None]
    field_scores: dict[str, float]
    overall_score: float
    latency_seconds: float
    candidate_ok: bool
    candidate_error: str | None


@dataclass
class CandidateReport:
    """Aggregate report for a single candidate model."""

    model: str
    mean_similarity: float
    mean_latency_seconds: float
    latency_delta_pct: float
    passed: bool
    per_doc: list[DocComparison] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Similarity computation
# ---------------------------------------------------------------------------


def normalize_field(value: str | None) -> str:
    """Normalize a field value for similarity comparison.

    * ``None`` collapses to the empty string so ``None vs None`` is a
      perfect match and ``None vs "foo"`` is a total miss.
    * Whitespace is collapsed and the value is lower-cased so trivial
      casing / spacing differences do not tank the score.
    """
    if value is None:
        return ""
    return " ".join(str(value).strip().split()).lower()


def field_similarity(baseline: str | None, candidate: str | None) -> float:
    """Compute a normalized similarity score in ``[0.0, 1.0]``.

    Uses :func:`difflib.SequenceMatcher.ratio` after normalization.
    Two empty-normalized values (both ``None`` or both blank) return
    ``1.0`` — this is the correct semantic for "the baseline model
    also returned nothing, so the candidate did not regress".
    """
    a = normalize_field(baseline)
    b = normalize_field(candidate)
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(a=a, b=b).ratio()


def document_score(
    baseline_fields: dict[str, str | None],
    candidate_fields: dict[str, str | None],
) -> tuple[float, dict[str, float]]:
    """Compute per-field scores and their mean for one document.

    Returns ``(overall_mean, {field: score})``. The mean is computed
    across :data:`COMPARED_FIELDS` in order. If the candidate failed
    entirely (all fields empty because the orchestrator raised) the
    scores will be whatever ``field_similarity`` decides; that's fine
    because an all-empty candidate vs a populated baseline naturally
    scores very low.
    """
    scores: dict[str, float] = {}
    for field_name in COMPARED_FIELDS:
        scores[field_name] = field_similarity(
            baseline_fields.get(field_name),
            candidate_fields.get(field_name),
        )
    overall = statistics.fmean(scores[f] for f in COMPARED_FIELDS)
    return overall, scores


def corpus_mean_similarity(comparisons: Sequence[DocComparison]) -> float:
    """Mean of ``overall_score`` across every document comparison.

    Returns ``0.0`` on empty input to keep the stdout table and JSON
    report shape stable.
    """
    if not comparisons:
        return 0.0
    return statistics.fmean(c.overall_score for c in comparisons)


def passes_tolerance(mean_similarity: float, tolerance: float) -> bool:
    """Return True when a candidate's mean similarity clears the bar."""
    return mean_similarity >= tolerance


def compare_runs(
    baseline_outputs: Sequence[DocOutputs],
    candidate_outputs: Sequence[DocOutputs],
) -> list[DocComparison]:
    """Diff two runs of the same corpus and produce per-doc comparisons.

    Matching is by document path. If one side is missing a path the
    other has, that document is skipped (cannot compare). Baseline
    failures (candidate succeeded but baseline did not) are also
    skipped with a warning on stderr — we cannot score a candidate
    against a missing reference.
    """
    baseline_by_path = {d.path: d for d in baseline_outputs}
    candidate_by_path = {d.path: d for d in candidate_outputs}

    comparisons: list[DocComparison] = []
    for path, baseline_doc in baseline_by_path.items():
        candidate_doc = candidate_by_path.get(path)
        if candidate_doc is None:
            sys.stderr.write(
                f"regression_check: skipping {path} — candidate run "
                "has no matching document\n"
            )
            continue
        if not baseline_doc.ok:
            sys.stderr.write(
                f"regression_check: skipping {path} — baseline failed "
                f"({baseline_doc.error})\n"
            )
            continue
        overall, field_scores = document_score(
            baseline_doc.as_fields(), candidate_doc.as_fields()
        )
        comparisons.append(
            DocComparison(
                path=path,
                filename=baseline_doc.filename,
                baseline=baseline_doc.as_fields(),
                candidate=candidate_doc.as_fields(),
                field_scores=field_scores,
                overall_score=overall,
                latency_seconds=candidate_doc.latency_seconds,
                candidate_ok=candidate_doc.ok,
                candidate_error=candidate_doc.error,
            )
        )
    return comparisons


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_candidate_list(raw: str) -> list[str]:
    """Split a comma-separated candidate list, trimming whitespace.

    Empty segments are silently dropped so ``"foo,,bar"`` parses to
    ``["foo", "bar"]`` — convenient when assembling the flag in shell
    scripts.
    """
    return [part.strip() for part in raw.split(",") if part.strip()]


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser.

    Exposed as a module-level function so unit tests can exercise it
    without running :func:`main`.
    """
    parser = argparse.ArgumentParser(
        prog="regression_check.py",
        description=(
            "Run a text-model regression check across a corpus of PDFs, "
            "comparing structured extraction outputs against a baseline "
            "model and selecting the first candidate whose mean "
            "similarity clears --tolerance."
        ),
    )
    parser.add_argument(
        "--corpus",
        type=str,
        default=None,
        help=(
            "Path to a directory of PDFs or a single PDF file. If "
            "omitted, the harness auto-discovers fixtures under the "
            "repo's tests tree via scripts.bench.discover_corpus."
        ),
    )
    parser.add_argument(
        "--baseline-model",
        dest="baseline_model",
        type=str,
        default=DEFAULT_BASELINE_MODEL,
        help=(
            f"Text model to use as the reference baseline. Default "
            f"{DEFAULT_BASELINE_MODEL}. Must be a valid Ollama tag."
        ),
    )
    parser.add_argument(
        "--candidates",
        type=_parse_candidate_list,
        default=list(DEFAULT_CANDIDATES),
        help=(
            "Comma-separated candidate text models to try in order. "
            f"Default {','.join(DEFAULT_CANDIDATES)}. The first "
            "candidate whose corpus mean similarity >= --tolerance "
            "is the winner."
        ),
    )
    parser.add_argument(
        "--cpu-fallback",
        dest="cpu_fallback",
        type=str,
        default=DEFAULT_CPU_FALLBACK,
        help=(
            f"Text model to recommend as a CPU-resident fallback when "
            f"every GPU candidate fails. Default {DEFAULT_CPU_FALLBACK}. "
            "The harness only emits a recommendation — it does NOT "
            "reconfigure Ollama (that is an operator action documented "
            "in docs/PERFORMANCE.md)."
        ),
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help=(
            "Path to the JSON report output file. Default "
            "scripts/bench_results/regression_{timestamp}.json."
        ),
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help=(
            f"Pass threshold for corpus mean similarity. Default "
            f"{DEFAULT_TOLERANCE}. Values must be in [0.0, 1.0]."
        ),
    )
    return parser


def default_output_path(now: datetime | None = None) -> Path:
    """Compute the default JSON report path under scripts/bench_results/."""
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_DIR / f"regression_{ts}.json"


# ---------------------------------------------------------------------------
# Git SHA helper (mirrors scripts/bench.py for consistency across reports)
# ---------------------------------------------------------------------------


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
# Orchestrator invocation
# ---------------------------------------------------------------------------
#
# The regression check invokes ``ExtractionOrchestrator.run_pipeline``
# for each document in the corpus, just like scripts/bench.py does.
# Unlike bench.py, we do NOT care about per-stage durations — only the
# three structured output fields (drawing_number, title, document_type)
# and wall-clock latency.
#
# The orchestrator factory is injectable so unit tests can pass a stub
# factory that returns a canned result per (model, path) combination.


def _make_synthetic_job(pdf_path: Path) -> Any:
    """Construct a minimal in-memory ``Job`` for the pipeline.

    Mirrors scripts/bench.py._make_synthetic_job but is duplicated here
    so regression_check.py can be run as a standalone script without
    importing bench module state.
    """
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


def _extract_doc_outputs(
    pdf_path: Path,
    result: Any,
    latency_seconds: float,
) -> DocOutputs:
    """Pull the three compared fields out of a ``PipelineResult``.

    The orchestrator returns ``PipelineResult.extraction_result`` which
    is an :class:`ExtractionResult` dataclass with
    ``drawing_number``, ``title``, and ``document_type`` attributes.
    ``document_type`` is a :class:`DocumentType` enum; we serialize via
    ``.value`` or ``str()`` as a fallback.
    """
    extraction = getattr(result, "extraction_result", None)

    def _field(name: str) -> str | None:
        if extraction is None:
            return None
        value = getattr(extraction, name, None)
        if value is None:
            return None
        # DocumentType is an enum; take its value if present.
        if hasattr(value, "value"):
            return str(value.value)
        return str(value)

    return DocOutputs(
        path=str(pdf_path),
        filename=pdf_path.name,
        drawing_number=_field("drawing_number"),
        title=_field("title"),
        document_type=_field("document_type"),
        latency_seconds=latency_seconds,
        ok=True,
        error=None,
    )


async def _run_single_doc(
    orchestrator: Any,
    pdf_path: Path,
) -> DocOutputs:
    """Invoke ``run_pipeline`` for one document and capture its outputs.

    Exceptions raised by the orchestrator are captured on the returned
    :class:`DocOutputs`; the regression run continues. A failed doc
    has ``ok=False`` and empty structured fields, which ``field_similarity``
    will correctly score as a total miss against the baseline.
    """
    started = time.perf_counter()
    try:
        job = _make_synthetic_job(pdf_path)
        pdf_bytes = pdf_path.read_bytes()
        result = await orchestrator.run_pipeline(job, pdf_bytes)
    except Exception as exc:  # noqa: BLE001 - keep going
        elapsed = time.perf_counter() - started
        return DocOutputs(
            path=str(pdf_path),
            filename=pdf_path.name,
            drawing_number=None,
            title=None,
            document_type=None,
            latency_seconds=elapsed,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    elapsed = time.perf_counter() - started
    return _extract_doc_outputs(pdf_path, result, elapsed)


async def _run_corpus(
    orchestrator: Any,
    corpus_paths: Sequence[Path],
) -> list[DocOutputs]:
    """Run every document in the corpus sequentially.

    We run sequentially (no ``asyncio.gather``) because the regression
    check compares structured outputs, not throughput — parallelism
    would only add variance. The bench harness at scripts/bench.py
    exists for throughput measurement.
    """
    results: list[DocOutputs] = []
    for pdf_path in corpus_paths:
        results.append(await _run_single_doc(orchestrator, pdf_path))
    return results


async def run_with_model(
    model: str,
    corpus_paths: Sequence[Path],
    *,
    orchestrator_factory: Callable[[], Awaitable[Any] | Any] | None = None,
    env: dict[str, str] | None = None,
) -> list[DocOutputs]:
    """Execute a single-model run against the corpus.

    Sets ``OLLAMA_TEXT_MODEL=<model>`` in the environment BEFORE
    constructing the orchestrator. The previous env value (if any) is
    restored on exit so repeated calls do not leak state.

    ``orchestrator_factory`` is injectable so unit tests can pass a
    fake that observes the env var without loading any real
    infrastructure. ``env`` defaults to :data:`os.environ`.
    """
    target_env = env if env is not None else os.environ
    previous: str | None = target_env.get(OLLAMA_TEXT_MODEL_ENV)
    target_env[OLLAMA_TEXT_MODEL_ENV] = model

    if orchestrator_factory is None:

        def _default_factory() -> Any:
            from zubot_ingestion.services import build_orchestrator

            return build_orchestrator()

        orchestrator_factory = _default_factory

    try:
        maybe = orchestrator_factory()
        if asyncio.iscoroutine(maybe):
            orchestrator = await maybe
        else:
            orchestrator = maybe
        return await _run_corpus(orchestrator, corpus_paths)
    finally:
        if previous is None:
            target_env.pop(OLLAMA_TEXT_MODEL_ENV, None)
        else:
            target_env[OLLAMA_TEXT_MODEL_ENV] = previous


# ---------------------------------------------------------------------------
# Top-level regression flow
# ---------------------------------------------------------------------------


@dataclass
class RegressionResult:
    """Final state of a regression run: baseline + all candidates + winner."""

    baseline_model: str
    baseline_outputs: list[DocOutputs]
    candidate_reports: list[CandidateReport]
    winner: str | None
    recommendation: str
    tolerance: float
    cpu_fallback: str


def _build_candidate_report(
    model: str,
    comparisons: Sequence[DocComparison],
    candidate_outputs: Sequence[DocOutputs],
    baseline_mean_latency: float,
    tolerance: float,
) -> CandidateReport:
    """Assemble a :class:`CandidateReport` from comparison rows."""
    mean_sim = corpus_mean_similarity(comparisons)
    candidate_latencies = [
        c.latency_seconds for c in candidate_outputs if c.ok
    ]
    mean_latency = (
        statistics.fmean(candidate_latencies) if candidate_latencies else 0.0
    )
    if baseline_mean_latency > 0:
        delta_pct = (
            (mean_latency - baseline_mean_latency) / baseline_mean_latency
        ) * 100.0
    else:
        delta_pct = 0.0
    return CandidateReport(
        model=model,
        mean_similarity=mean_sim,
        mean_latency_seconds=mean_latency,
        latency_delta_pct=delta_pct,
        passed=passes_tolerance(mean_sim, tolerance),
        per_doc=list(comparisons),
    )


def _build_recommendation(
    winner: str | None,
    candidate_reports: Sequence[CandidateReport],
    cpu_fallback: str,
) -> str:
    """Produce a one-line human-readable recommendation string."""
    if winner is not None:
        return f"adopt {winner}"
    tried = ", ".join(cr.model for cr in candidate_reports)
    return (
        f"fallback to CPU text-{cpu_fallback}: every GPU candidate "
        f"({tried}) failed the similarity tolerance. See "
        "docs/PERFORMANCE.md 'Regression check' section for how to "
        "reconfigure Ollama."
    )


async def run_regression_check(
    *,
    corpus_paths: Sequence[Path],
    baseline_model: str,
    candidates: Sequence[str],
    cpu_fallback: str,
    tolerance: float,
    orchestrator_factory: Callable[[], Awaitable[Any] | Any] | None = None,
    env: dict[str, str] | None = None,
) -> RegressionResult:
    """Execute the full baseline + ladder regression flow.

    Returns a :class:`RegressionResult` describing the winner (or
    ``None`` if every candidate failed) and the per-candidate
    aggregate reports. Callers are responsible for rendering the
    result to stdout and/or JSON.
    """
    # Baseline pass.
    baseline_outputs = await run_with_model(
        baseline_model,
        corpus_paths,
        orchestrator_factory=orchestrator_factory,
        env=env,
    )
    baseline_latencies = [b.latency_seconds for b in baseline_outputs if b.ok]
    baseline_mean_latency = (
        statistics.fmean(baseline_latencies) if baseline_latencies else 0.0
    )

    # Candidate passes. Stop iterating as soon as one clears the bar
    # so we do not waste Ollama time on models we already know we do
    # not need.
    candidate_reports: list[CandidateReport] = []
    winner: str | None = None
    for candidate in candidates:
        candidate_outputs = await run_with_model(
            candidate,
            corpus_paths,
            orchestrator_factory=orchestrator_factory,
            env=env,
        )
        comparisons = compare_runs(baseline_outputs, candidate_outputs)
        report = _build_candidate_report(
            candidate,
            comparisons,
            candidate_outputs,
            baseline_mean_latency,
            tolerance,
        )
        candidate_reports.append(report)
        if report.passed:
            winner = candidate
            break

    recommendation = _build_recommendation(winner, candidate_reports, cpu_fallback)

    return RegressionResult(
        baseline_model=baseline_model,
        baseline_outputs=list(baseline_outputs),
        candidate_reports=candidate_reports,
        winner=winner,
        recommendation=recommendation,
        tolerance=tolerance,
        cpu_fallback=cpu_fallback,
    )


# ---------------------------------------------------------------------------
# Reporting: JSON + stdout table
# ---------------------------------------------------------------------------


def _doc_outputs_to_json(doc: DocOutputs) -> dict[str, Any]:
    """Serialize a :class:`DocOutputs` to a plain JSON-ready dict."""
    return {
        "path": doc.path,
        "filename": doc.filename,
        "drawing_number": doc.drawing_number,
        "title": doc.title,
        "document_type": doc.document_type,
        "latency_seconds": doc.latency_seconds,
        "ok": doc.ok,
        "error": doc.error,
    }


def _candidate_report_to_json(report: CandidateReport) -> dict[str, Any]:
    """Serialize a :class:`CandidateReport` to a JSON-ready dict."""
    return {
        "model": report.model,
        "mean_similarity": report.mean_similarity,
        "mean_latency_seconds": report.mean_latency_seconds,
        "latency_delta_pct": report.latency_delta_pct,
        "passed": report.passed,
        "per_doc": [asdict(c) for c in report.per_doc],
    }


def build_json_report(
    result: RegressionResult,
    *,
    args_dict: dict[str, Any],
    git_sha: str,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the full JSON report from a :class:`RegressionResult`."""
    ts = (timestamp or datetime.now(timezone.utc)).isoformat()
    baseline_latencies = [
        b.latency_seconds for b in result.baseline_outputs if b.ok
    ]
    baseline_mean_latency = (
        statistics.fmean(baseline_latencies) if baseline_latencies else 0.0
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": ts,
        "git_sha": git_sha,
        "args": args_dict,
        "tolerance": result.tolerance,
        "cpu_fallback": result.cpu_fallback,
        "baseline": {
            "model": result.baseline_model,
            "corpus_size": len(result.baseline_outputs),
            "mean_latency_seconds": baseline_mean_latency,
            "docs": [_doc_outputs_to_json(d) for d in result.baseline_outputs],
        },
        "candidates": [
            _candidate_report_to_json(r) for r in result.candidate_reports
        ],
        "winner": result.winner,
        "recommendation": result.recommendation,
    }


def format_summary_table(result: RegressionResult) -> str:
    """Render a human-readable summary table of the regression result.

    Columns: candidate | mean_similarity | pass/fail | latency_delta_pct.
    Followed by the winner / recommendation line.
    """
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("Zubot Ingestion Text-Model Regression Check")
    lines.append("=" * 72)
    lines.append(f"baseline model:        {result.baseline_model}")
    lines.append(f"tolerance:             {result.tolerance:.3f}")
    lines.append(f"corpus size:           {len(result.baseline_outputs)}")
    lines.append("")
    header = f"{'candidate':30s} {'mean_sim':>10s}  {'pass':>6s}  {'lat_delta_pct':>15s}"
    lines.append(header)
    lines.append("-" * len(header))
    for report in result.candidate_reports:
        verdict = "pass" if report.passed else "fail"
        lines.append(
            f"{report.model:30s} "
            f"{report.mean_similarity:10.4f}  "
            f"{verdict:>6s}  "
            f"{report.latency_delta_pct:+14.1f}%"
        )
    lines.append("")
    if result.winner is not None:
        lines.append(f"winner:                {result.winner}")
    else:
        lines.append("winner:                (none — every candidate failed)")
    lines.append(f"recommendation:        {result.recommendation}")
    lines.append("=" * 72)
    return "\n".join(lines)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    """Persist the JSON report, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def main(argv: Iterable[str] | None = None) -> int:
    """CLI entrypoint. Returns a process exit code.

    Exit codes:

    * ``0`` — at least one candidate passed the tolerance check.
    * ``1`` — every candidate failed; the report recommends CPU fallback.
    * ``2`` — usage error (no corpus, invalid tolerance, etc.).
    """
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not 0.0 <= args.tolerance <= 1.0:
        sys.stderr.write("regression_check: --tolerance must be in [0.0, 1.0]\n")
        return 2

    if not args.candidates:
        sys.stderr.write(
            "regression_check: --candidates must contain at least one model\n"
        )
        return 2

    corpus_paths = discover_corpus(args.corpus)
    if not corpus_paths:
        sys.stderr.write(
            "regression_check: no PDFs found.\n"
            "  auto-discovery searched the same roots as scripts/bench.py.\n"
            "  pass --corpus PATH to point at a directory or single PDF.\n"
        )
        return 2

    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else default_output_path()
    )
    git_sha = get_git_sha()

    sys.stdout.write(
        f"regression_check: corpus={len(corpus_paths)} docs  "
        f"baseline={args.baseline_model}  "
        f"candidates={','.join(args.candidates)}  "
        f"tolerance={args.tolerance:.3f}\n"
    )
    sys.stdout.flush()

    result = asyncio.run(
        run_regression_check(
            corpus_paths=corpus_paths,
            baseline_model=args.baseline_model,
            candidates=args.candidates,
            cpu_fallback=args.cpu_fallback,
            tolerance=args.tolerance,
        )
    )

    report = build_json_report(
        result,
        args_dict={
            "corpus": str(args.corpus) if args.corpus else None,
            "baseline_model": args.baseline_model,
            "candidates": list(args.candidates),
            "cpu_fallback": args.cpu_fallback,
            "tolerance": args.tolerance,
            "output": str(output_path),
        },
        git_sha=git_sha,
    )
    _write_report(output_path, report)

    sys.stdout.write(format_summary_table(result) + "\n")
    sys.stdout.write(f"wrote report: {output_path}\n")
    sys.stdout.flush()

    # Exit 0 on success (at least one candidate cleared the bar),
    # exit 1 on full fallback (operator needs to intervene).
    return 0 if result.winner is not None else 1


if __name__ == "__main__":
    # Ensure the repo's ``src`` directory is importable when run as a
    # plain script from a checkout that hasn't ``pip install``-ed the
    # package. Mirrors the pytest.ini ``pythonpath = src`` setting.
    _SRC = _REPO_ROOT / "src"
    if _SRC.exists() and str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    raise SystemExit(main())
