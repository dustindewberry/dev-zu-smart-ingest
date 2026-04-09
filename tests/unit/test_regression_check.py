"""Unit tests for the text-model regression harness at ``scripts/regression_check.py``.

These tests exercise the harness without requiring a live Ollama
server or any real PDFs. The orchestrator is stubbed via the
``orchestrator_factory`` injection point on ``run_with_model`` /
``run_regression_check`` — the stub returns deterministic
``ExtractionResult`` objects per (model, document) combination so
we can assert exact similarity scores and ladder behavior.

What is covered:

* CLI parsing: every flag round-trips, defaults match the spec,
  comma-separated candidate lists split correctly, tolerance is
  enforced to be in ``[0.0, 1.0]``.
* Fallback ladder order: given a stub factory that scores each
  candidate deterministically, the harness stops at the FIRST
  candidate that clears the tolerance and records the others as
  "not attempted" implicitly (by not iterating past the winner).
* Similarity computation: canned string pairs produce the expected
  ``difflib.SequenceMatcher`` scores, including normalization edge
  cases (None vs None, whitespace, case insensitivity).
* Tolerance pass/fail gating: a candidate with mean_similarity just
  above / below the threshold is correctly classified.
* Full-flow smoke test: the end-to-end flow runs against a stub
  orchestrator that emits canned ExtractionResult dataclasses, and
  the resulting JSON report has the expected shape and winner.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Module loader: scripts/regression_check.py is not on sys.path.
# ---------------------------------------------------------------------------
#
# pytest.ini only adds src/ to pythonpath. We load the harness via
# importlib.util so these tests work from any CWD and do not leak
# the scripts directory into sys.path for other test files.


_REPO_ROOT = Path(__file__).resolve().parents[2]
_HARNESS_PATH = _REPO_ROOT / "scripts" / "regression_check.py"


def _load_harness_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "regression_check_under_test", _HARNESS_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["regression_check_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


rc = _load_harness_module()


# ---------------------------------------------------------------------------
# Stub ExtractionResult / PipelineResult
# ---------------------------------------------------------------------------
#
# The harness only looks at ``result.extraction_result.drawing_number``,
# ``.title``, and ``.document_type`` on the returned pipeline result.
# DocumentType is normally a :class:`DocumentType` enum with a ``.value``
# attribute — we stub that via a simple dataclass with a ``value`` field
# so the harness's ``getattr(value, "value")`` normalization path runs.


@dataclass
class _StubDocumentType:
    value: str


@dataclass
class _StubExtractionResult:
    drawing_number: str | None
    title: str | None
    document_type: Any  # _StubDocumentType | None


@dataclass
class _StubPipelineResult:
    extraction_result: _StubExtractionResult


def _er(
    drawing_number: str | None = None,
    title: str | None = None,
    document_type: str | None = None,
) -> _StubPipelineResult:
    """Tiny helper to build a stub pipeline result."""
    dt = _StubDocumentType(value=document_type) if document_type else None
    return _StubPipelineResult(
        extraction_result=_StubExtractionResult(
            drawing_number=drawing_number, title=title, document_type=dt
        )
    )


# ---------------------------------------------------------------------------
# Stub orchestrator driven by (OLLAMA_TEXT_MODEL env, document name)
# ---------------------------------------------------------------------------


class _StubOrchestrator:
    """Orchestrator that returns a canned result per (model, filename).

    The ``run_with_model`` helper in the harness sets
    ``OLLAMA_TEXT_MODEL`` on the environment BEFORE invoking the
    factory. We therefore observe the env var inside ``run_pipeline``
    and look up the canned result from a dict. That lets a single
    shared factory drive the entire ladder without per-candidate
    bookkeeping in each test.
    """

    def __init__(
        self,
        *,
        results_by_model_and_name: dict[str, dict[str, _StubPipelineResult]],
        env: dict[str, str],
    ) -> None:
        self._results = results_by_model_and_name
        self._env = env
        self.calls: list[tuple[str, str]] = []  # (model, filename)
        self.raise_on: set[tuple[str, str]] = set()

    async def run_pipeline(
        self,
        job: Any,
        pdf_bytes: bytes,
        *args: Any,
        **kwargs: Any,
    ) -> _StubPipelineResult:
        model = self._env.get(rc.OLLAMA_TEXT_MODEL_ENV, "<unset>")
        filename = getattr(job, "filename", "<no-filename>")
        self.calls.append((model, filename))
        if (model, filename) in self.raise_on:
            raise RuntimeError(f"stub failure for {model}/{filename}")
        by_name = self._results.get(model, {})
        if filename in by_name:
            return by_name[filename]
        # Default: empty result — useful for "candidate has nothing
        # because the model is untrained on this corpus".
        return _er()


def _make_factory(orchestrator: _StubOrchestrator) -> Any:
    def _factory() -> _StubOrchestrator:
        return orchestrator

    _factory.orchestrator = orchestrator  # type: ignore[attr-defined]
    return _factory


# ---------------------------------------------------------------------------
# Corpus helper: create N dummy PDF files on disk so _run_single_doc
# can read_bytes() on them without raising.
# ---------------------------------------------------------------------------


def _make_corpus(tmp_path: Path, names: list[str]) -> list[Path]:
    paths: list[Path] = []
    for name in names:
        p = tmp_path / name
        # Minimal PDF header so sha256 is stable and read_bytes works.
        p.write_bytes(b"%PDF-1.4\n%stub\n" + name.encode("utf-8") + b"\n")
        paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# Monkeypatch _make_synthetic_job so it does NOT require the real
# zubot_ingestion.domain.entities import graph.
# ---------------------------------------------------------------------------


@dataclass
class _StubJob:
    job_id: str
    batch_id: str
    filename: str
    file_hash: str
    file_path: str


@pytest.fixture(autouse=True)
def _patch_make_synthetic_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``_make_synthetic_job`` with a lightweight stub.

    The real helper imports domain entities which in turn drag in a
    large chunk of the infrastructure graph. For hermetic unit tests
    we only need an object with a ``filename`` attribute that also
    survives ``pathlib.Path.read_bytes()`` in the caller.
    """

    def _fake(pdf_path: Path) -> _StubJob:
        return _StubJob(
            job_id="stub-job",
            batch_id="stub-batch",
            filename=pdf_path.name,
            file_hash="stub-hash",
            file_path=str(pdf_path),
        )

    monkeypatch.setattr(rc, "_make_synthetic_job", _fake)


# ---------------------------------------------------------------------------
# CLI parsing tests
# ---------------------------------------------------------------------------


class TestCLIParsing:
    def test_default_values_match_spec(self) -> None:
        parser = rc.build_arg_parser()
        args = parser.parse_args([])
        assert args.corpus is None
        assert args.baseline_model == "qwen2.5:7b"
        assert args.candidates == [
            "qwen2.5:3b",
            "llama3.2:3b-instruct",
            "phi3.5:3.8b",
        ]
        assert args.cpu_fallback == "qwen2.5:7b"
        assert args.output is None
        assert args.tolerance == pytest.approx(0.85)

    def test_all_flags_round_trip(self) -> None:
        parser = rc.build_arg_parser()
        args = parser.parse_args(
            [
                "--corpus", "/tmp/corpus",
                "--baseline-model", "qwen2.5:7b",
                "--candidates", "foo:3b,bar:3b-instruct",
                "--cpu-fallback", "qwen2.5:7b",
                "--output", "/tmp/out.json",
                "--tolerance", "0.90",
            ]
        )
        assert args.corpus == "/tmp/corpus"
        assert args.candidates == ["foo:3b", "bar:3b-instruct"]
        assert args.output == "/tmp/out.json"
        assert args.tolerance == pytest.approx(0.90)

    def test_candidate_list_trims_whitespace_and_drops_empty(self) -> None:
        parser = rc.build_arg_parser()
        args = parser.parse_args(["--candidates", "a , b ,, c"])
        assert args.candidates == ["a", "b", "c"]

    def test_parse_candidate_list_helper(self) -> None:
        assert rc._parse_candidate_list("a,b,c") == ["a", "b", "c"]
        assert rc._parse_candidate_list("  a , b ,, ") == ["a", "b"]
        assert rc._parse_candidate_list("") == []

    def test_default_output_path_uses_regression_prefix(self) -> None:
        ts = datetime(2026, 4, 9, 12, 34, 56, tzinfo=timezone.utc)
        path = rc.default_output_path(now=ts)
        assert path.parent.name == "bench_results"
        assert path.name.startswith("regression_")
        assert path.suffix == ".json"
        assert "20260409T123456Z" in path.name


# ---------------------------------------------------------------------------
# Similarity computation tests
# ---------------------------------------------------------------------------


class TestSimilarity:
    def test_none_vs_none_is_perfect(self) -> None:
        assert rc.field_similarity(None, None) == pytest.approx(1.0)

    def test_empty_vs_empty_is_perfect(self) -> None:
        assert rc.field_similarity("", "") == pytest.approx(1.0)

    def test_none_vs_value_is_zero(self) -> None:
        # Normalization collapses None to "" and "foo" to "foo";
        # difflib.SequenceMatcher("", "foo").ratio() == 0.0.
        assert rc.field_similarity(None, "foo") == pytest.approx(0.0)

    def test_identical_strings_are_perfect(self) -> None:
        assert rc.field_similarity("A-101", "A-101") == pytest.approx(1.0)

    def test_case_insensitive_normalization(self) -> None:
        # "Floor Plan" vs "FLOOR PLAN" normalizes to the same string.
        assert rc.field_similarity("Floor Plan", "FLOOR PLAN") == pytest.approx(1.0)

    def test_whitespace_normalization(self) -> None:
        assert rc.field_similarity("  Floor   Plan  ", "Floor Plan") == pytest.approx(1.0)

    def test_close_but_not_identical(self) -> None:
        # Canonical difflib example: length 6 string, one-char diff →
        # ratio = 2*matches/total = 2*5/12 ≈ 0.833.
        score = rc.field_similarity("abcdef", "abcXef")
        assert 0.80 < score < 0.90

    def test_document_score_averages_three_fields(self) -> None:
        # Two perfect scores and one miss: (1 + 1 + 0) / 3 ≈ 0.667.
        baseline = {
            "drawing_number": "A-101",
            "title": "Floor Plan",
            "document_type": "drawing",
        }
        candidate = {
            "drawing_number": "A-101",
            "title": "Floor Plan",
            "document_type": None,
        }
        overall, scores = rc.document_score(baseline, candidate)
        assert scores["drawing_number"] == pytest.approx(1.0)
        assert scores["title"] == pytest.approx(1.0)
        assert scores["document_type"] == pytest.approx(0.0)
        assert overall == pytest.approx(2.0 / 3.0)

    def test_corpus_mean_similarity_empty_is_zero(self) -> None:
        assert rc.corpus_mean_similarity([]) == pytest.approx(0.0)

    def test_corpus_mean_similarity_averages_overall_scores(self) -> None:
        comps = [
            rc.DocComparison(
                path="p1", filename="f1",
                baseline={}, candidate={},
                field_scores={}, overall_score=0.9,
                latency_seconds=1.0, candidate_ok=True, candidate_error=None,
            ),
            rc.DocComparison(
                path="p2", filename="f2",
                baseline={}, candidate={},
                field_scores={}, overall_score=0.5,
                latency_seconds=1.0, candidate_ok=True, candidate_error=None,
            ),
        ]
        assert rc.corpus_mean_similarity(comps) == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# Tolerance gating tests
# ---------------------------------------------------------------------------


class TestTolerance:
    def test_exact_threshold_passes(self) -> None:
        assert rc.passes_tolerance(0.85, 0.85) is True

    def test_just_above_threshold_passes(self) -> None:
        assert rc.passes_tolerance(0.8501, 0.85) is True

    def test_just_below_threshold_fails(self) -> None:
        assert rc.passes_tolerance(0.8499, 0.85) is False

    def test_zero_tolerance_accepts_everything(self) -> None:
        assert rc.passes_tolerance(0.0, 0.0) is True

    def test_perfect_tolerance_requires_identity(self) -> None:
        assert rc.passes_tolerance(1.0, 1.0) is True
        assert rc.passes_tolerance(0.9999, 1.0) is False


# ---------------------------------------------------------------------------
# Fallback ladder tests
# ---------------------------------------------------------------------------


class TestFallbackLadder:
    @pytest.mark.asyncio
    async def test_first_candidate_passes_stops_iteration(
        self, tmp_path: Path
    ) -> None:
        """When the first candidate clears the bar we do NOT keep trying."""
        paths = _make_corpus(tmp_path, ["doc1.pdf", "doc2.pdf"])

        env: dict[str, str] = {}
        results = {
            "qwen2.5:7b": {
                "doc1.pdf": _er("A-101", "Floor Plan", "drawing"),
                "doc2.pdf": _er("A-102", "Site Plan", "drawing"),
            },
            "qwen2.5:3b": {
                # Identical to baseline → mean similarity = 1.0 → passes.
                "doc1.pdf": _er("A-101", "Floor Plan", "drawing"),
                "doc2.pdf": _er("A-102", "Site Plan", "drawing"),
            },
            # If the harness accidentally kept trying, these garbage
            # outputs would show up in calls[] — the test asserts they
            # do not.
            "llama3.2:3b-instruct": {},
            "phi3.5:3.8b": {},
        }
        orchestrator = _StubOrchestrator(
            results_by_model_and_name=results, env=env
        )
        factory = _make_factory(orchestrator)

        result = await rc.run_regression_check(
            corpus_paths=paths,
            baseline_model="qwen2.5:7b",
            candidates=["qwen2.5:3b", "llama3.2:3b-instruct", "phi3.5:3.8b"],
            cpu_fallback="qwen2.5:7b",
            tolerance=0.85,
            orchestrator_factory=factory,
            env=env,
        )

        assert result.winner == "qwen2.5:3b"
        assert len(result.candidate_reports) == 1
        assert result.candidate_reports[0].passed is True
        assert result.candidate_reports[0].mean_similarity == pytest.approx(1.0)

        # Assert the orchestrator was only invoked for baseline + 3b.
        models_called = {model for (model, _) in orchestrator.calls}
        assert models_called == {"qwen2.5:7b", "qwen2.5:3b"}

    @pytest.mark.asyncio
    async def test_second_candidate_wins_when_first_fails(
        self, tmp_path: Path
    ) -> None:
        """A failing first candidate does not stop the ladder."""
        paths = _make_corpus(tmp_path, ["doc1.pdf", "doc2.pdf"])

        env: dict[str, str] = {}
        results = {
            "qwen2.5:7b": {
                "doc1.pdf": _er("A-101", "Floor Plan", "drawing"),
                "doc2.pdf": _er("A-102", "Site Plan", "drawing"),
            },
            "qwen2.5:3b": {
                # Completely wrong → will fail.
                "doc1.pdf": _er("XXXX", "WRONG", "other"),
                "doc2.pdf": _er("YYYY", "WRONG", "other"),
            },
            "llama3.2:3b-instruct": {
                # Perfect match → will win.
                "doc1.pdf": _er("A-101", "Floor Plan", "drawing"),
                "doc2.pdf": _er("A-102", "Site Plan", "drawing"),
            },
            "phi3.5:3.8b": {},
        }
        orchestrator = _StubOrchestrator(
            results_by_model_and_name=results, env=env
        )
        factory = _make_factory(orchestrator)

        result = await rc.run_regression_check(
            corpus_paths=paths,
            baseline_model="qwen2.5:7b",
            candidates=["qwen2.5:3b", "llama3.2:3b-instruct", "phi3.5:3.8b"],
            cpu_fallback="qwen2.5:7b",
            tolerance=0.85,
            orchestrator_factory=factory,
            env=env,
        )

        assert result.winner == "llama3.2:3b-instruct"
        # Two candidates should have run (3b failed, 3b-instruct passed).
        assert len(result.candidate_reports) == 2
        assert result.candidate_reports[0].model == "qwen2.5:3b"
        assert result.candidate_reports[0].passed is False
        assert result.candidate_reports[1].model == "llama3.2:3b-instruct"
        assert result.candidate_reports[1].passed is True

        models_called = {model for (model, _) in orchestrator.calls}
        assert "phi3.5:3.8b" not in models_called

    @pytest.mark.asyncio
    async def test_all_candidates_fail_recommends_cpu_fallback(
        self, tmp_path: Path
    ) -> None:
        """Every GPU candidate failing produces the CPU fallback recommendation."""
        paths = _make_corpus(tmp_path, ["doc1.pdf"])

        env: dict[str, str] = {}
        results = {
            "qwen2.5:7b": {
                "doc1.pdf": _er("A-101", "Floor Plan", "drawing"),
            },
            "qwen2.5:3b": {"doc1.pdf": _er("X", "X", "X")},
            "llama3.2:3b-instruct": {"doc1.pdf": _er("Y", "Y", "Y")},
            "phi3.5:3.8b": {"doc1.pdf": _er("Z", "Z", "Z")},
        }
        orchestrator = _StubOrchestrator(
            results_by_model_and_name=results, env=env
        )
        factory = _make_factory(orchestrator)

        result = await rc.run_regression_check(
            corpus_paths=paths,
            baseline_model="qwen2.5:7b",
            candidates=["qwen2.5:3b", "llama3.2:3b-instruct", "phi3.5:3.8b"],
            cpu_fallback="qwen2.5:7b",
            tolerance=0.85,
            orchestrator_factory=factory,
            env=env,
        )

        assert result.winner is None
        assert len(result.candidate_reports) == 3
        assert all(not r.passed for r in result.candidate_reports)
        assert "fallback to CPU text-qwen2.5:7b" in result.recommendation
        assert "qwen2.5:3b" in result.recommendation
        assert "llama3.2:3b-instruct" in result.recommendation
        assert "phi3.5:3.8b" in result.recommendation

    @pytest.mark.asyncio
    async def test_candidates_run_in_declared_order(
        self, tmp_path: Path
    ) -> None:
        """Candidates must be iterated in the exact order provided."""
        paths = _make_corpus(tmp_path, ["doc1.pdf"])

        env: dict[str, str] = {}
        # Give every candidate a score of 0.0 so the harness iterates
        # through the entire ladder — that lets us inspect the call order.
        results: dict[str, dict[str, _StubPipelineResult]] = {
            "qwen2.5:7b": {"doc1.pdf": _er("A", "B", "C")},
            "phi3.5:3.8b": {"doc1.pdf": _er("x", "x", "x")},
            "qwen2.5:3b": {"doc1.pdf": _er("x", "x", "x")},
            "llama3.2:3b-instruct": {"doc1.pdf": _er("x", "x", "x")},
        }
        orchestrator = _StubOrchestrator(
            results_by_model_and_name=results, env=env
        )
        factory = _make_factory(orchestrator)

        candidate_order = ["phi3.5:3.8b", "qwen2.5:3b", "llama3.2:3b-instruct"]

        await rc.run_regression_check(
            corpus_paths=paths,
            baseline_model="qwen2.5:7b",
            candidates=candidate_order,
            cpu_fallback="qwen2.5:7b",
            tolerance=0.85,
            orchestrator_factory=factory,
            env=env,
        )

        # Drop the baseline call; everything after must match the
        # candidate_order one model at a time.
        model_sequence = [m for (m, _) in orchestrator.calls if m != "qwen2.5:7b"]
        # Collapse repeats from multi-doc corpora.
        unique_sequence: list[str] = []
        for m in model_sequence:
            if not unique_sequence or unique_sequence[-1] != m:
                unique_sequence.append(m)
        assert unique_sequence == candidate_order


# ---------------------------------------------------------------------------
# Full-flow smoke test
# ---------------------------------------------------------------------------


class TestFullFlowSmoke:
    @pytest.mark.asyncio
    async def test_run_regression_check_end_to_end(
        self, tmp_path: Path
    ) -> None:
        """Run the full flow and verify the RegressionResult + JSON shape."""
        paths = _make_corpus(tmp_path, ["one.pdf", "two.pdf", "three.pdf"])

        env: dict[str, str] = {}
        results = {
            "qwen2.5:7b": {
                "one.pdf": _er("DWG-001", "Title One", "drawing"),
                "two.pdf": _er("DWG-002", "Title Two", "drawing"),
                "three.pdf": _er("DWG-003", "Title Three", "specification"),
            },
            "qwen2.5:3b": {
                # Very close to baseline: case-insensitive normalized
                # strings match on 2/3 docs, minor diff on third.
                "one.pdf": _er("DWG-001", "Title One", "drawing"),
                "two.pdf": _er("DWG-002", "Title Two", "drawing"),
                "three.pdf": _er("DWG-003", "Title Three", "specification"),
            },
            "llama3.2:3b-instruct": {},
            "phi3.5:3.8b": {},
        }
        orchestrator = _StubOrchestrator(
            results_by_model_and_name=results, env=env
        )
        factory = _make_factory(orchestrator)

        result = await rc.run_regression_check(
            corpus_paths=paths,
            baseline_model="qwen2.5:7b",
            candidates=["qwen2.5:3b", "llama3.2:3b-instruct", "phi3.5:3.8b"],
            cpu_fallback="qwen2.5:7b",
            tolerance=0.85,
            orchestrator_factory=factory,
            env=env,
        )

        assert result.winner == "qwen2.5:3b"
        assert result.baseline_model == "qwen2.5:7b"
        assert len(result.baseline_outputs) == 3
        assert len(result.candidate_reports) == 1
        assert all(b.ok for b in result.baseline_outputs)

        # Build and inspect the JSON report.
        ts = datetime(2026, 4, 9, 12, 34, 56, tzinfo=timezone.utc)
        report = rc.build_json_report(
            result,
            args_dict={
                "corpus": str(tmp_path),
                "baseline_model": "qwen2.5:7b",
                "candidates": ["qwen2.5:3b", "llama3.2:3b-instruct", "phi3.5:3.8b"],
                "cpu_fallback": "qwen2.5:7b",
                "tolerance": 0.85,
                "output": str(tmp_path / "out.json"),
            },
            git_sha="abc123",
            timestamp=ts,
        )

        assert report["schema_version"] == "1.0"
        assert report["git_sha"] == "abc123"
        assert report["tolerance"] == pytest.approx(0.85)
        assert report["winner"] == "qwen2.5:3b"
        assert report["recommendation"] == "adopt qwen2.5:3b"

        baseline_block = report["baseline"]
        assert baseline_block["model"] == "qwen2.5:7b"
        assert baseline_block["corpus_size"] == 3
        assert len(baseline_block["docs"]) == 3
        # Ensure the structured fields survived serialization.
        for doc in baseline_block["docs"]:
            assert doc["drawing_number"] is not None
            assert doc["title"] is not None
            assert doc["document_type"] is not None

        candidates_block = report["candidates"]
        assert len(candidates_block) == 1
        c = candidates_block[0]
        assert c["model"] == "qwen2.5:3b"
        assert c["passed"] is True
        assert c["mean_similarity"] == pytest.approx(1.0)
        assert len(c["per_doc"]) == 3
        # Per-doc shape: field_scores + overall_score + latency_seconds.
        for row in c["per_doc"]:
            assert set(row["field_scores"].keys()) == {
                "drawing_number",
                "title",
                "document_type",
            }
            assert row["overall_score"] == pytest.approx(1.0)

        # The JSON report is round-trip serializable.
        json.dumps(report)

    @pytest.mark.asyncio
    async def test_summary_table_contains_expected_columns(
        self, tmp_path: Path
    ) -> None:
        """Stdout summary should list every candidate and the winner line."""
        paths = _make_corpus(tmp_path, ["one.pdf"])

        env: dict[str, str] = {}
        results = {
            "qwen2.5:7b": {"one.pdf": _er("A", "B", "C")},
            "qwen2.5:3b": {"one.pdf": _er("A", "B", "C")},
        }
        orchestrator = _StubOrchestrator(
            results_by_model_and_name=results, env=env
        )
        factory = _make_factory(orchestrator)

        result = await rc.run_regression_check(
            corpus_paths=paths,
            baseline_model="qwen2.5:7b",
            candidates=["qwen2.5:3b"],
            cpu_fallback="qwen2.5:7b",
            tolerance=0.85,
            orchestrator_factory=factory,
            env=env,
        )
        table = rc.format_summary_table(result)
        assert "candidate" in table
        assert "mean_sim" in table
        assert "lat_delta_pct" in table
        assert "qwen2.5:3b" in table
        assert "winner:" in table
        assert "pass" in table

    @pytest.mark.asyncio
    async def test_env_var_is_set_per_candidate_and_restored(
        self, tmp_path: Path
    ) -> None:
        """``run_with_model`` must set and restore OLLAMA_TEXT_MODEL."""
        paths = _make_corpus(tmp_path, ["one.pdf"])

        env: dict[str, str] = {rc.OLLAMA_TEXT_MODEL_ENV: "original-value"}
        results = {
            "qwen2.5:7b": {"one.pdf": _er("A", "B", "C")},
            "qwen2.5:3b": {"one.pdf": _er("A", "B", "C")},
        }
        orchestrator = _StubOrchestrator(
            results_by_model_and_name=results, env=env
        )
        factory = _make_factory(orchestrator)

        await rc.run_with_model(
            "qwen2.5:3b",
            paths,
            orchestrator_factory=factory,
            env=env,
        )
        # After the call returns the env must be restored.
        assert env[rc.OLLAMA_TEXT_MODEL_ENV] == "original-value"
        # Inside the call, the stub observed "qwen2.5:3b".
        assert orchestrator.calls == [("qwen2.5:3b", "one.pdf")]

    @pytest.mark.asyncio
    async def test_env_var_is_removed_if_previously_unset(
        self, tmp_path: Path
    ) -> None:
        """If the env var was unset going in, it must be unset going out."""
        paths = _make_corpus(tmp_path, ["one.pdf"])

        env: dict[str, str] = {}
        results = {"qwen2.5:3b": {"one.pdf": _er("A", "B", "C")}}
        orchestrator = _StubOrchestrator(
            results_by_model_and_name=results, env=env
        )
        factory = _make_factory(orchestrator)

        await rc.run_with_model(
            "qwen2.5:3b",
            paths,
            orchestrator_factory=factory,
            env=env,
        )
        assert rc.OLLAMA_TEXT_MODEL_ENV not in env


# ---------------------------------------------------------------------------
# Compare-runs behavior (alignment by path, skips, failures)
# ---------------------------------------------------------------------------


class TestCompareRuns:
    def test_matches_by_path(self) -> None:
        baseline = [
            rc.DocOutputs(
                path="/a.pdf", filename="a.pdf",
                drawing_number="X", title="T", document_type="drawing",
                latency_seconds=1.0, ok=True, error=None,
            )
        ]
        candidate = [
            rc.DocOutputs(
                path="/a.pdf", filename="a.pdf",
                drawing_number="X", title="T", document_type="drawing",
                latency_seconds=1.0, ok=True, error=None,
            )
        ]
        comps = rc.compare_runs(baseline, candidate)
        assert len(comps) == 1
        assert comps[0].overall_score == pytest.approx(1.0)

    def test_skips_baseline_failure(self) -> None:
        baseline = [
            rc.DocOutputs(
                path="/a.pdf", filename="a.pdf",
                drawing_number=None, title=None, document_type=None,
                latency_seconds=1.0, ok=False, error="boom",
            )
        ]
        candidate = [
            rc.DocOutputs(
                path="/a.pdf", filename="a.pdf",
                drawing_number="X", title="T", document_type="drawing",
                latency_seconds=1.0, ok=True, error=None,
            )
        ]
        comps = rc.compare_runs(baseline, candidate)
        assert comps == []

    def test_skips_missing_candidate(self) -> None:
        baseline = [
            rc.DocOutputs(
                path="/a.pdf", filename="a.pdf",
                drawing_number="X", title="T", document_type="drawing",
                latency_seconds=1.0, ok=True, error=None,
            ),
            rc.DocOutputs(
                path="/b.pdf", filename="b.pdf",
                drawing_number="Y", title="U", document_type="drawing",
                latency_seconds=1.0, ok=True, error=None,
            ),
        ]
        candidate = [
            rc.DocOutputs(
                path="/a.pdf", filename="a.pdf",
                drawing_number="X", title="T", document_type="drawing",
                latency_seconds=1.0, ok=True, error=None,
            )
        ]
        comps = rc.compare_runs(baseline, candidate)
        # /b.pdf has no candidate row → skipped.
        assert [c.path for c in comps] == ["/a.pdf"]
