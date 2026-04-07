"""Unit tests for :mod:`zubot_ingestion.domain.pipeline.extractors.filename_parser`."""

from __future__ import annotations

from zubot_ingestion.domain.entities import FilenameHints
from zubot_ingestion.domain.enums import Discipline
from zubot_ingestion.domain.pipeline.extractors.filename_parser import FilenameParser


# ---------------------------------------------------------------------------
# Drawing-number pattern matching
# ---------------------------------------------------------------------------


def test_six_digit_disc_seq_match() -> None:
    parser = FilenameParser()
    hints = parser.parse("170154-L-001.pdf")
    assert hints.drawing_number_hint == "170154-L-001"
    assert hints.confidence == 0.90
    assert hints.matched_pattern is not None
    assert "six_digit_disc_seq" in hints.matched_pattern


def test_six_digit_disc_seq_with_4digit_seq() -> None:
    parser = FilenameParser()
    hints = parser.parse("170154-L-1234_RevA01.pdf")
    assert hints.drawing_number_hint == "170154-L-1234"
    assert hints.revision_hint == "A01"


def test_alpha_num_dash_num_match() -> None:
    parser = FilenameParser()
    hints = parser.parse("A101-02.pdf")
    assert hints.drawing_number_hint == "A101-02"
    assert hints.confidence == 0.75
    assert hints.matched_pattern is not None
    assert "alpha_num_dash_num" in hints.matched_pattern


def test_dwg_prefix_match() -> None:
    parser = FilenameParser()
    hints = parser.parse("DWG-12345.pdf")
    assert hints.drawing_number_hint == "DWG-12345"
    assert hints.confidence == 0.70
    assert hints.matched_pattern is not None
    assert "dwg_prefix" in hints.matched_pattern


def test_no_match_returns_none() -> None:
    parser = FilenameParser()
    hints = parser.parse("random_file.pdf")
    assert hints.drawing_number_hint is None
    assert hints.revision_hint is None
    assert hints.confidence == 0.0


def test_pattern_priority_six_digit_wins_over_alpha() -> None:
    """If both 6-digit and alpha-num patterns could match, 6-digit wins."""
    parser = FilenameParser()
    # Contains both "170154-L-001" (six_digit) and "A101-02"
    hints = parser.parse("170154-L-001_A101-02.pdf")
    assert hints.drawing_number_hint == "170154-L-001"
    assert hints.confidence == 0.90


def test_pattern_priority_alpha_wins_over_dwg() -> None:
    parser = FilenameParser()
    # Contains both "A101-02" and "DWG-555"
    hints = parser.parse("A101-02_DWG-555.pdf")
    assert hints.drawing_number_hint == "A101-02"


# ---------------------------------------------------------------------------
# Revision extraction
# ---------------------------------------------------------------------------


def test_revision_with_letter_prefix() -> None:
    parser = FilenameParser()
    hints = parser.parse("Some_Drawing_Rev_P02.pdf")
    assert hints.revision_hint == "P02"


def test_revision_without_letter_prefix() -> None:
    parser = FilenameParser()
    hints = parser.parse("Some_Drawing_Rev01.pdf")
    assert hints.revision_hint == "01"


def test_revision_case_insensitive() -> None:
    parser = FilenameParser()
    hints = parser.parse("foo_REV_A02.pdf")
    assert hints.revision_hint == "A02"


def test_revision_missing() -> None:
    parser = FilenameParser()
    hints = parser.parse("170154-L-001.pdf")
    assert hints.revision_hint is None


# ---------------------------------------------------------------------------
# Discipline inference
# ---------------------------------------------------------------------------


def test_discipline_from_path_segment_electrical() -> None:
    parser = FilenameParser()
    discipline = parser.infer_discipline("/projects/site/electrical/dwg.pdf")
    assert discipline is Discipline.ELECTRICAL


def test_discipline_from_path_segment_mechanical() -> None:
    parser = FilenameParser()
    discipline = parser.infer_discipline("/projects/site/mechanical/dwg.pdf")
    assert discipline is Discipline.MECHANICAL


def test_discipline_from_path_segment_architectural() -> None:
    parser = FilenameParser()
    discipline = parser.infer_discipline("/projects/site/architectural/dwg.pdf")
    assert discipline is Discipline.ARCHITECTURAL


def test_discipline_from_filename_stem() -> None:
    parser = FilenameParser()
    discipline = parser.infer_discipline("electrical_riser.pdf")
    assert discipline is Discipline.ELECTRICAL


def test_discipline_unknown_returns_none() -> None:
    parser = FilenameParser()
    discipline = parser.infer_discipline("foo/bar/baz.pdf")
    assert discipline is None


def test_discipline_appears_in_matched_pattern() -> None:
    parser = FilenameParser()
    hints = parser.parse("/proj/electrical/170154-L-001.pdf")
    assert hints.matched_pattern is not None
    assert "electrical" in hints.matched_pattern


# ---------------------------------------------------------------------------
# Path/extension robustness
# ---------------------------------------------------------------------------


def test_handles_windows_path() -> None:
    parser = FilenameParser()
    hints = parser.parse(r"C:\projects\elec\170154-L-001.pdf")
    assert hints.drawing_number_hint == "170154-L-001"
    discipline = parser.infer_discipline(r"C:\projects\elec\170154-L-001.pdf")
    # "elec" is a substring of "electrical" → matches
    assert discipline is Discipline.ELECTRICAL


def test_handles_no_extension() -> None:
    parser = FilenameParser()
    hints = parser.parse("170154-L-001")
    assert hints.drawing_number_hint == "170154-L-001"


def test_handles_empty_string() -> None:
    parser = FilenameParser()
    hints = parser.parse("")
    assert hints.drawing_number_hint is None
    assert hints.confidence == 0.0


def test_returns_filename_hints_dataclass() -> None:
    parser = FilenameParser()
    hints = parser.parse("foo.pdf")
    assert isinstance(hints, FilenameHints)
