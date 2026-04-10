"""Unit tests for the benchmark harness at ``scripts/bench.py``.

These tests exercise the harness without requiring a live Ollama
server or any real PDFs on disk:

* Corpus auto-discovery is tested with a ``monkeypatch``ed glob list
  pointing into a ``tmp_path`` fixture, so the filesystem layout is
  deterministic.
* The argparse definition is tested by round-tripping every flag.
* The JSON report schema is tested by running ``run_benchmark``
  against a stub orchestrator that returns a canned ``PipelineResult``,
  then asserting the shape of the output dict (and the dict written
  to disk by ``main``).

The tests do NOT touch ``build_orchestrator`` or any infrastructure
adapter. The harness is designed to accept an ``orchestrator_factory``
injection point exactly so the test suite can stay hermetic.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest


# ---------------------------------------------------------------------------
# scripts/bench.py module loader
# ---------------------------------------------------------------------------
#
# ``scripts/`` is not on ``sys.path`` by default (the pytest config only
# adds ``src``), so we load the module explicitly via
# ``importlib.util.spec_from_file_location``. This keeps the tests
# runnable from any CWD and avoids polluting ``sys.path`` for the rest
# of the test session.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BENCH_PATH = _REPO_ROOT / "scripts" / "bench.py"


def _load_bench_module() -> Any:
    spec = importlib.util.spec_from_file_location("bench_harness_under_test", _BENCH_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bench_harness_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


bench = _load_bench_module()


# ---------------------------------------------------------------------------
# Stub PipelineResult and Job entities
# ---------------------------------------------------------------------------
#
# The real ``PipelineResult`` is a frozen dataclass with a handful of
# nested types (ExtractionResult, CompanionResult, SidecarDocument,
# ConfidenceAssessment, PipelineError). The harness only looks at
# ``pipeline_trace``, ``confidence_assessment.tier``, and (as a
# fallback) ``extraction_result.confidence_tier``. We therefore stub
# just those attributes with a pair of ``@dataclass``es.


@dataclass
class _StubTier:
    value: str


@dataclass
class _StubAssessment:
    tier: _StubTier | None


@dataclass
class _StubPipelineResult:
    confidence_assessment: _StubAssessment | None
    pipeline_trace: dict[str, Any] = field(default_factory=dict)
    extraction_result: Any = None


def _make_canned_pipeline_result(tier: str = "auto") -> _StubPipelineResult:
    """Return a stub PipelineResult with realistic stage durations."""
    return _StubPipelineResult(
        confidence_assessment=_StubAssessment(tier=_StubTier(value=tier)),
        pipeline_trace={
            "stages": {
                "pdf_load": {"duration_ms": 20, "ok": True},
                "stage1": {"duration_ms": 4500, "ok": True},
                "stage2": {"duration_ms": 5100, "ok": True},
                "stage3": {"duration_ms": 15, "ok": True},
                "metadata_write": {"duration_ms": 100, "ok": True},
                "search_index": {"duration_ms": 50, "ok": True},
                "callback": {"duration_ms": 30, "ok": True},
            },
            "errors": [],
        },
    )


class _StubOrchestrator:
    """Fake orchestrator whose ``run_pipeline`` returns a canned result."""

    def __init__(self, result: _StubPipelineResult, *, raise_on: set[str] | None = None) -> None:
        self._result = result
        self._raise_on = raise_on or set()
        self.calls: list[tuple[str, int]] = []

    async def run_pipeline(
        self,
        job: Any,
        pdf_bytes: bytes,
        *,
        deployment_id: int | None = None,
        node_id: int | None = None,
        callback_url: str | None = None,
        api_key: str = "",
    ) -> _StubPipelineResult:
        name = getattr(job, "filename", "<no-filename>")
        self.calls.append((name, len(pdf_bytes)))
        if name in self._raise_on:
            raise RuntimeError(f"stub failure for {name}")
        return self._result


def _make_stub_orchestrator_factory(**kwargs: Any) -> Any:
    orch = _StubOrchestrator(_make_canned_pipeline_result(**kwargs))

    def _factory() -> _StubOrchestrator:
        return orch

    _factory.orchestrator = orch  # type: ignore[attr-defined]
    return _factory


def _make_stub_job(tmp_path: Path, name: str = "fake.pdf") -> Any:
    """Create a dummy job object compatible with ``_run_single``.

    The harness reads ``job.filename`` (for logging) and calls
    ``pdf_path.read_bytes()`` directly, so we can bypass the real
    ``Job`` dataclass entirely and just return a simple namespace.
    We DO need ``_make_synthetic_job`` to succeed though, so we
    monkeypatch it in the tests that drive ``_run_single``.
    """

    @dataclass
    class _FakeJob:
        filename: str
        job_id: Any
        batch_id: Any
        file_hash: str
        file_path: str

    return _FakeJob(
        filename=name,
        job_id=uuid4(),
        batch_id=uuid4(),
        file_hash="0" * 64,
        file_path=str(tmp_path / name),
    )


# ---------------------------------------------------------------------------
# (a) Corpus auto-discovery
# ---------------------------------------------------------------------------


def test_discover_corpus_explicit_file_returns_single_pdf(tmp_path: Path) -> None:
    pdf = tmp_path / "one.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    result = bench.discover_corpus(str(pdf))
    assert result == [pdf.resolve()]


def test_discover_corpus_explicit_directory_returns_recursive_pdfs(
    tmp_path: Path,
) -> None:
    (tmp_path / "sub").mkdir()
    a = tmp_path / "a.pdf"
    b = tmp_path / "sub" / "b.pdf"
    c = tmp_path / "notes.txt"  # non-PDF should be ignored
    for p in (a, b):
        p.write_bytes(b"%PDF-1.4")
    c.write_text("ignored")

    result = bench.discover_corpus(str(tmp_path))
    assert set(result) == {a.resolve(), b.resolve()}


def test_discover_corpus_missing_path_returns_empty(tmp_path: Path) -> None:
    ghost = tmp_path / "does-not-exist"
    assert bench.discover_corpus(str(ghost)) == []


def test_discover_corpus_auto_discovery_with_mocked_fs(tmp_path: Path) -> None:
    """Auto-discovery should find PDFs under the mocked globs."""
    fixtures = tmp_path / "tests" / "fixtures"
    data_dir = tmp_path / "tests" / "data"
    other = tmp_path / "tests" / "other"
    for d in (fixtures, data_dir, other):
        d.mkdir(parents=True)
    pdf_a = fixtures / "a.pdf"
    pdf_b = data_dir / "nested" / "b.pdf"
    pdf_c = other / "c.pdf"
    pdf_b.parent.mkdir(parents=True, exist_ok=True)
    for p in (pdf_a, pdf_b, pdf_c):
        p.write_bytes(b"%PDF-1.4")

    custom_globs = (
        (fixtures, "**/*.pdf"),
        (data_dir, "**/*.pdf"),
        (tmp_path / "tests", "**/*.pdf"),
    )
    result = bench.discover_corpus(None, discovery_globs=custom_globs)
    assert set(result) == {pdf_a.resolve(), pdf_b.resolve(), pdf_c.resolve()}


def test_discover_corpus_auto_discovery_empty_when_nothing_found(
    tmp_path: Path,
) -> None:
    """Auto-discovery falls back to empty when no PDFs exist."""
    empty_root = tmp_path / "tests" / "fixtures"
    empty_root.mkdir(parents=True)
    custom_globs = ((empty_root, "**/*.pdf"),)
    assert bench.discover_corpus(None, discovery_globs=custom_globs) == []


def test_main_exits_nonzero_when_no_corpus_and_none_discovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI: main() must exit 2 with a clear message on empty corpus."""
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    # Override the module-level discovery globs with a pointer at an
    # empty directory so auto-discovery yields nothing.
    monkeypatch.setattr(bench, "_DISCOVERY_GLOBS", ((empty_root, "**/*.pdf"),))
    rc = bench.main([])
    assert rc == 2
    captured = capsys.readouterr()
    assert "no PDFs found" in captured.err
    assert "--corpus" in captured.err


# ---------------------------------------------------------------------------
# (b) CLI argparse
# ---------------------------------------------------------------------------


def test_build_arg_parser_accepts_all_flags() -> None:
    parser = bench.build_arg_parser()
    args = parser.parse_args(
        [
            "--corpus",
            "/tmp/corpus",
            "--iterations",
            "5",
            "--concurrency",
            "3",
            "--warmup",
            "2",
            "--output",
            "/tmp/out.json",
        ]
    )
    assert args.corpus == "/tmp/corpus"
    assert args.iterations == 5
    assert args.concurrency == 3
    assert args.warmup == 2
    assert args.output == "/tmp/out.json"


def test_build_arg_parser_defaults() -> None:
    parser = bench.build_arg_parser()
    args = parser.parse_args([])
    assert args.corpus is None
    assert args.iterations == 1
    assert args.concurrency == 1
    assert args.warmup == 0
    assert args.output is None


def test_main_rejects_negative_iterations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Give auto-discovery a non-empty result so the iterations check is
    # the first thing to fail.
    pdf = tmp_path / "corpus" / "a.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(
        bench, "_DISCOVERY_GLOBS", ((tmp_path / "corpus", "**/*.pdf"),)
    )
    rc = bench.main(["--iterations", "0"])
    assert rc == 2
    assert "iterations" in capsys.readouterr().err


def test_main_rejects_negative_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pdf = tmp_path / "corpus" / "a.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(
        bench, "_DISCOVERY_GLOBS", ((tmp_path / "corpus", "**/*.pdf"),)
    )
    rc = bench.main(["--concurrency", "0"])
    assert rc == 2
    assert "concurrency" in capsys.readouterr().err


def test_main_rejects_negative_warmup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pdf = tmp_path / "corpus" / "a.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(
        bench, "_DISCOVERY_GLOBS", ((tmp_path / "corpus", "**/*.pdf"),)
    )
    rc = bench.main(["--warmup", "-1"])
    assert rc == 2
    assert "warmup" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# (c) JSON report schema (via run_benchmark + main with mocked factory)
# ---------------------------------------------------------------------------


def _patch_synthetic_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Replace ``_make_synthetic_job`` with a trivial fake.

    The real implementation imports ``zubot_ingestion.domain.entities`` and
    wraps a UUID, which requires the full domain module graph at import
    time. For the hermetic harness tests we substitute a tiny fake that
    returns a namespace with just the fields the rest of the harness
    reads.
    """

    def _fake(pdf_path: Path) -> Any:
        return _make_stub_job(tmp_path, pdf_path.name)

    monkeypatch.setattr(bench, "_make_synthetic_job", _fake)


async def test_run_benchmark_produces_expected_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_a = tmp_path / "a.pdf"
    pdf_b = tmp_path / "b.pdf"
    pdf_a.write_bytes(b"%PDF-1.4 a")
    pdf_b.write_bytes(b"%PDF-1.4 b")
    _patch_synthetic_job(monkeypatch, tmp_path)

    factory = _make_stub_orchestrator_factory(tier="auto")
    records, measured_wall = await bench.run_benchmark(
        corpus_paths=[pdf_a, pdf_b],
        iterations=2,
        concurrency=1,
        warmup=0,
        orchestrator_factory=factory,
    )

    # 2 docs x 2 iterations = 4 records
    assert len(records) == 4
    assert measured_wall >= 0.0
    for r in records:
        assert r.ok is True
        assert r.confidence_tier == "auto"
        assert r.error is None
        assert r.total_seconds >= 0.0
        # Every canned stage should be present in the per-doc stages
        # dict with a float seconds value.
        for key in ("pdf_load", "stage1", "stage2", "stage3", "metadata_write", "search_index", "callback"):
            assert key in r.stages
            assert isinstance(r.stages[key], float)
            assert r.stages[key] >= 0.0
    # Stub orchestrator received both files for both iterations.
    orchestrator = factory.orchestrator  # type: ignore[attr-defined]
    assert sorted(c[0] for c in orchestrator.calls) == [
        "a.pdf",
        "a.pdf",
        "b.pdf",
        "b.pdf",
    ]


async def test_run_benchmark_warmup_runs_are_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_a = tmp_path / "a.pdf"
    pdf_a.write_bytes(b"%PDF-1.4 a")
    _patch_synthetic_job(monkeypatch, tmp_path)

    factory = _make_stub_orchestrator_factory()
    records, _wall = await bench.run_benchmark(
        corpus_paths=[pdf_a],
        iterations=1,
        concurrency=1,
        warmup=3,
        orchestrator_factory=factory,
    )

    # Only the single measured iteration is in the records.
    assert len(records) == 1
    # But the orchestrator should have been called 3 warmup + 1 measured = 4 times.
    orchestrator = factory.orchestrator  # type: ignore[attr-defined]
    assert len(orchestrator.calls) == 4


async def test_run_benchmark_captures_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_ok = tmp_path / "ok.pdf"
    pdf_bad = tmp_path / "bad.pdf"
    pdf_ok.write_bytes(b"%PDF-1.4 ok")
    pdf_bad.write_bytes(b"%PDF-1.4 bad")
    _patch_synthetic_job(monkeypatch, tmp_path)

    orch = _StubOrchestrator(
        _make_canned_pipeline_result(), raise_on={"bad.pdf"}
    )

    def _factory() -> _StubOrchestrator:
        return orch

    records, _wall = await bench.run_benchmark(
        corpus_paths=[pdf_ok, pdf_bad],
        iterations=1,
        concurrency=2,
        warmup=0,
        orchestrator_factory=_factory,
    )
    rec_by_name = {r.filename: r for r in records}
    assert rec_by_name["ok.pdf"].ok is True
    assert rec_by_name["bad.pdf"].ok is False
    assert rec_by_name["bad.pdf"].error is not None
    assert "stub failure" in rec_by_name["bad.pdf"].error


def test_aggregate_records_computes_percentiles_and_stage_stats() -> None:
    # Three successful records with distinct latencies.
    records = [
        bench.PerDocRecord(
            path="/x/1.pdf",
            filename="1.pdf",
            iteration=0,
            ok=True,
            error=None,
            total_seconds=1.0,
            confidence_tier="auto",
            stages={"stage1": 0.5, "stage2": 0.4},
        ),
        bench.PerDocRecord(
            path="/x/2.pdf",
            filename="2.pdf",
            iteration=0,
            ok=True,
            error=None,
            total_seconds=2.0,
            confidence_tier="spot",
            stages={"stage1": 1.0, "stage2": 0.8},
        ),
        bench.PerDocRecord(
            path="/x/3.pdf",
            filename="3.pdf",
            iteration=0,
            ok=True,
            error=None,
            total_seconds=4.0,
            confidence_tier="review",
            stages={"stage1": 2.0, "stage2": 1.6},
        ),
    ]
    agg = bench.aggregate_records(records, measured_wall_seconds=6.0)
    assert agg["doc_count"] == 3
    assert agg["success_count"] == 3
    assert agg["failure_count"] == 0
    assert agg["failure_rate"] == 0.0
    # 3 docs in 6 seconds = 30 docs/min.
    assert agg["throughput_docs_per_minute"] == pytest.approx(30.0)
    latency = agg["latency_seconds"]
    assert latency["min"] == 1.0
    assert latency["max"] == 4.0
    assert latency["p99"] == 4.0
    # Nearest-rank p50 of [1, 2, 4] picks index 1 -> 2.0.
    assert latency["p50"] == 2.0
    assert latency["mean"] == pytest.approx((1.0 + 2.0 + 4.0) / 3.0)
    # Stage aggregation: stage1 mean should be (0.5 + 1.0 + 2.0) / 3.
    stage1 = agg["stage_seconds"]["stage1"]
    assert stage1["mean"] == pytest.approx((0.5 + 1.0 + 2.0) / 3.0)
    assert stage1["max"] == 2.0
    assert stage1["count"] == 3


def test_aggregate_records_handles_empty_input() -> None:
    agg = bench.aggregate_records([], measured_wall_seconds=0.0)
    assert agg["doc_count"] == 0
    assert agg["success_count"] == 0
    assert agg["failure_count"] == 0
    assert agg["failure_rate"] == 0.0
    assert agg["throughput_docs_per_minute"] == 0.0
    assert agg["latency_seconds"]["p50"] == 0.0
    assert agg["stage_seconds"] == {}


def test_aggregate_records_counts_failures() -> None:
    records = [
        bench.PerDocRecord(
            path="/x/a.pdf",
            filename="a.pdf",
            iteration=0,
            ok=True,
            error=None,
            total_seconds=1.5,
            confidence_tier="auto",
            stages={"stage1": 1.0},
        ),
        bench.PerDocRecord(
            path="/x/b.pdf",
            filename="b.pdf",
            iteration=0,
            ok=False,
            error="boom",
            total_seconds=0.1,
            confidence_tier=None,
        ),
    ]
    agg = bench.aggregate_records(records, measured_wall_seconds=2.0)
    assert agg["doc_count"] == 2
    assert agg["success_count"] == 1
    assert agg["failure_count"] == 1
    assert agg["failure_rate"] == 0.5


def test_build_report_includes_required_top_level_keys() -> None:
    now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
    report = bench.build_report(
        args_dict={"corpus": "/tmp", "iterations": 1, "concurrency": 1, "warmup": 0, "output": "/tmp/out.json"},
        records=[],
        measured_wall_seconds=0.0,
        settings_snapshot={
            "OLLAMA_NUM_PARALLEL": 2,
            "OLLAMA_TEXT_MODEL": "qwen2.5:3b",
            "OLLAMA_VISION_MODEL": "qwen2.5vl:7b",
            "CELERY_WORKER_CONCURRENCY": 2,
            "COMPANION_SKIP_ENABLED": True,
            "COMPANION_SKIP_MIN_WORDS": 40,
        },
        git_sha="abc1234",
        timestamp=now,
    )
    assert report["schema_version"] == bench.SCHEMA_VERSION
    assert report["timestamp"] == now.isoformat()
    assert report["git_sha"] == "abc1234"
    assert set(report["settings"].keys()) == {
        "OLLAMA_NUM_PARALLEL",
        "OLLAMA_TEXT_MODEL",
        "OLLAMA_VISION_MODEL",
        "CELERY_WORKER_CONCURRENCY",
        "COMPANION_SKIP_ENABLED",
        "COMPANION_SKIP_MIN_WORDS",
    }
    assert report["settings"]["OLLAMA_NUM_PARALLEL"] == 2
    assert "per_doc" in report
    assert "aggregate" in report
    assert "args" in report


def test_snapshot_settings_returns_expected_keys() -> None:
    snap = bench.snapshot_settings()
    # OLLAMA_NUM_PARALLEL is intentionally NOT part of the snapshot —
    # it's an Ollama server-side env var set on the upstream Ollama
    # container, not a zubot Settings field. See the docstring in
    # ``shared/constants.py`` next to ``PERF_OLLAMA_KEEP_ALIVE``.
    assert set(snap.keys()) == {
        "OLLAMA_TEXT_MODEL",
        "OLLAMA_VISION_MODEL",
        "CELERY_WORKER_CONCURRENCY",
        "COMPANION_SKIP_ENABLED",
        "COMPANION_SKIP_MIN_WORDS",
    }


def test_get_git_sha_returns_string() -> None:
    sha = bench.get_git_sha()
    assert isinstance(sha, str)
    assert sha  # either a real SHA or 'unknown'


def test_default_output_path_has_timestamp_suffix() -> None:
    now = datetime(2026, 4, 9, 12, 34, 56, tzinfo=timezone.utc)
    path = bench.default_output_path(now)
    assert path.name == "bench_20260409T123456Z.json"
    assert path.parent == bench.DEFAULT_OUTPUT_DIR


def test_format_summary_renders_key_fields() -> None:
    now = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
    report = bench.build_report(
        args_dict={"corpus": None, "iterations": 1, "concurrency": 1, "warmup": 0, "output": "/x.json"},
        records=[
            bench.PerDocRecord(
                path="/x/a.pdf",
                filename="a.pdf",
                iteration=0,
                ok=True,
                error=None,
                total_seconds=1.25,
                confidence_tier="auto",
                stages={"stage1": 0.8, "stage2": 0.3},
            )
        ],
        measured_wall_seconds=1.5,
        settings_snapshot=bench.snapshot_settings(),
        git_sha="deadbeef",
        timestamp=now,
    )
    text = bench.format_summary(report)
    assert "Zubot Ingestion Benchmark Summary" in text
    assert "deadbeef" in text
    assert "documents" in text
    assert "latency (seconds)" in text
    assert "stage1" in text


def test_main_writes_json_report_with_mocked_orchestrator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: main() should write a JSON report with the expected shape.

    We point ``--corpus`` at a tmpdir with two fake PDFs, patch the
    synthetic-job helper so it doesn't hit the real domain entities,
    patch ``run_benchmark``'s default factory by passing our stub via
    monkeypatching the ``zubot_ingestion.services`` import hook the
    default factory would otherwise use, and assert the JSON file on
    disk matches the schema.
    """
    pdf_a = tmp_path / "a.pdf"
    pdf_b = tmp_path / "b.pdf"
    pdf_a.write_bytes(b"%PDF-1.4 a")
    pdf_b.write_bytes(b"%PDF-1.4 b")

    _patch_synthetic_job(monkeypatch, tmp_path)

    # Inject a fake ``zubot_ingestion.services`` module with a
    # ``build_orchestrator`` that returns our stub. This is the cleanest
    # way to intercept the default factory without passing an explicit
    # injection down through main().
    stub_services = type(sys)("zubot_ingestion.services")
    canned_orch = _StubOrchestrator(_make_canned_pipeline_result(tier="spot"))
    stub_services.build_orchestrator = lambda: canned_orch  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zubot_ingestion.services", stub_services)

    # Stub snapshot_settings so we don't need the real pydantic Settings
    # (which would require env vars).
    monkeypatch.setattr(
        bench,
        "snapshot_settings",
        lambda: {
            "OLLAMA_NUM_PARALLEL": 2,
            "OLLAMA_TEXT_MODEL": "qwen2.5:3b",
            "OLLAMA_VISION_MODEL": "qwen2.5vl:7b",
            "CELERY_WORKER_CONCURRENCY": 2,
            "COMPANION_SKIP_ENABLED": True,
            "COMPANION_SKIP_MIN_WORDS": 40,
        },
    )
    monkeypatch.setattr(bench, "get_git_sha", lambda: "testsha")

    output_path = tmp_path / "out" / "report.json"
    rc = bench.main(
        [
            "--corpus",
            str(tmp_path),
            "--iterations",
            "1",
            "--concurrency",
            "1",
            "--warmup",
            "0",
            "--output",
            str(output_path),
        ]
    )
    assert rc == 0
    assert output_path.exists()

    report = json.loads(output_path.read_text(encoding="utf-8"))

    # Top-level keys.
    assert report["schema_version"] == "1.0"
    assert report["git_sha"] == "testsha"
    assert "timestamp" in report
    # args block preserved.
    assert report["args"]["iterations"] == 1
    assert report["args"]["concurrency"] == 1
    assert report["args"]["warmup"] == 0
    assert report["args"]["output"] == str(output_path)

    # Settings snapshot contains every required key.
    assert report["settings"] == {
        "OLLAMA_NUM_PARALLEL": 2,
        "OLLAMA_TEXT_MODEL": "qwen2.5:3b",
        "OLLAMA_VISION_MODEL": "qwen2.5vl:7b",
        "CELERY_WORKER_CONCURRENCY": 2,
        "COMPANION_SKIP_ENABLED": True,
        "COMPANION_SKIP_MIN_WORDS": 40,
    }

    # per_doc records: one per PDF.
    assert len(report["per_doc"]) == 2
    for rec in report["per_doc"]:
        assert rec["ok"] is True
        assert rec["confidence_tier"] == "spot"
        assert rec["error"] is None
        assert isinstance(rec["total_seconds"], float)
        # Canned stages present.
        for k in (
            "pdf_load",
            "stage1",
            "stage2",
            "stage3",
            "metadata_write",
            "search_index",
            "callback",
        ):
            assert k in rec["stages"]
            assert isinstance(rec["stages"][k], float)

    # Aggregate block.
    agg = report["aggregate"]
    assert agg["doc_count"] == 2
    assert agg["success_count"] == 2
    assert agg["failure_count"] == 0
    assert agg["failure_rate"] == 0.0
    assert "latency_seconds" in agg
    assert "stage_seconds" in agg
    assert set(agg["latency_seconds"].keys()) == {
        "p50",
        "p95",
        "p99",
        "mean",
        "min",
        "max",
    }
    assert "stage1" in agg["stage_seconds"]

    # Human-readable summary was printed to stdout.
    captured = capsys.readouterr()
    assert "Zubot Ingestion Benchmark Summary" in captured.out
    assert "testsha" in captured.out
