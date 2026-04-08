"""Filename hint extraction.

Implements :class:`~zubot_ingestion.domain.protocols.IFilenameParser`.
Pure-Python regex matching against common construction-drawing filename
patterns. No I/O, no infrastructure dependencies.

Recognised patterns (in priority order):

1.  ``\\d{6}-[A-Z]-\\d{3,4}``  e.g. ``170154-L-001``
2.  ``[A-Z]\\d{2,3}-\\d{2,4}``  e.g. ``A101-02``, ``M50-1234``
3.  ``DWG-\\d+``               e.g. ``DWG-12345``

A revision hint is extracted from the substring matching ``Rev\\s*([A-Z]?\\d{2})``.

A discipline is inferred from the path segments using a simple
keyword-to-:class:`~zubot_ingestion.domain.enums.Discipline` mapping.

This module may only import from stdlib, ``zubot_ingestion.domain.entities``,
and ``zubot_ingestion.domain.enums`` per the dependency rules in
boundary-contracts.md §3.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath

from zubot_ingestion.domain.entities import FilenameHints
from zubot_ingestion.domain.enums import Discipline

__all__ = ["FilenameParser"]


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# (pattern, name, base_confidence) — order matters; first match wins.
_DRAWING_NUMBER_PATTERNS: tuple[tuple[re.Pattern[str], str, float], ...] = (
    (re.compile(r"(\d{6}-[A-Z]-\d{3,4})"), "six_digit_disc_seq", 0.90),
    (re.compile(r"([A-Z]\d{2,3}-\d{2,4})"), "alpha_num_dash_num", 0.75),
    (re.compile(r"(DWG-\d+)"), "dwg_prefix", 0.70),
)

_REVISION_PATTERN: re.Pattern[str] = re.compile(r"Rev[\s_]*([A-Z]?\d{2})", re.IGNORECASE)

# Discipline keyword → enum value. The check is case-insensitive and runs
# against each path segment AND the filename stem.
_DISCIPLINE_KEYWORDS: tuple[tuple[str, Discipline], ...] = (
    ("architectural", Discipline.ARCHITECTURAL),
    ("architecture", Discipline.ARCHITECTURAL),
    ("electrical", Discipline.ELECTRICAL),
    ("mechanical", Discipline.MECHANICAL),
    ("plumbing", Discipline.PLUMBING),
    ("fire", Discipline.FIRE),
    ("structural", Discipline.STRUCTURAL),
    ("structure", Discipline.STRUCTURAL),
)


def _split_path_segments(filename: str) -> list[str]:
    """Split a filename or path into its components in an OS-agnostic way.

    The input may be a bare filename (``foo.pdf``), a POSIX path
    (``/proj/elec/foo.pdf``), or a Windows path
    (``C:\\proj\\elec\\foo.pdf``). All variants are normalised to a list of
    string segments.
    """
    if not filename:
        return []
    # Try POSIX first; PureWindowsPath understands both separators but
    # PurePosixPath does not handle backslashes, so we pick the parser
    # whose split yields more segments.
    posix_parts = list(PurePosixPath(filename).parts)
    win_parts = list(PureWindowsPath(filename).parts)
    return win_parts if len(win_parts) > len(posix_parts) else posix_parts


def _stem(filename: str) -> str:
    """Return the basename without extension, OS-agnostically."""
    parts = _split_path_segments(filename)
    if not parts:
        return ""
    base = parts[-1]
    # Strip a single trailing extension; we don't care which.
    if "." in base:
        return base.rsplit(".", 1)[0]
    return base


def _match_drawing_number(text: str) -> tuple[str | None, str | None, float]:
    """Apply :data:`_DRAWING_NUMBER_PATTERNS` in order; return the first hit.

    Returns ``(value, pattern_name, confidence)``. If nothing matches, returns
    ``(None, None, 0.0)``.
    """
    for pattern, name, confidence in _DRAWING_NUMBER_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1), name, confidence
    return None, None, 0.0


def _match_revision(text: str) -> str | None:
    """Extract a revision hint like ``Rev P02`` → ``P02``."""
    match = _REVISION_PATTERN.search(text)
    if match:
        return match.group(1)
    return None


def _infer_discipline(filename: str) -> Discipline | None:
    """Infer discipline from path segments OR the filename stem.

    Matches in either direction — a discipline keyword either containing
    a path segment (e.g. segment ``"elec"`` → keyword ``"electrical"``)
    or being contained in a segment (e.g. segment ``"electrical"``
    anywhere) counts as a hit. The bidirectional check lets operators
    abbreviate folder names without sacrificing discipline detection.
    """
    segments_lower = [seg.lower() for seg in _split_path_segments(filename)]
    stem_lower = _stem(filename).lower()
    haystacks = [*segments_lower, stem_lower]
    for keyword, discipline in _DISCIPLINE_KEYWORDS:
        for h in haystacks:
            if not h:
                continue
            if keyword in h:
                return discipline
            # Allow a short path segment (>=3 chars) to prefix-match a
            # longer discipline keyword so ``elec/`` resolves to
            # ``electrical``. The minimum length prevents single-letter
            # false positives.
            if len(h) >= 3 and keyword.startswith(h):
                return discipline
    return None


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class FilenameParser:
    """Concrete :class:`IFilenameParser` implementation.

    This class is stateless and side-effect-free. Construct once and reuse.
    """

    def parse(self, filename: str) -> FilenameHints:
        """Extract drawing-number and revision hints from a filename.

        Args:
            filename: The original filename (with or without extension; may
                also be a full path on POSIX or Windows).

        Returns:
            A :class:`FilenameHints` value. ``confidence`` is the strength
            of the drawing-number match (or 0.0 if nothing matched), and
            ``matched_pattern`` names the regex that fired (for debugging).
        """
        if filename is None:  # type: ignore[redundant-expr]
            return FilenameHints(
                drawing_number_hint=None,
                revision_hint=None,
                confidence=0.0,
                matched_pattern=None,
            )

        # We match against the stem, not the full path, to avoid spurious
        # matches inside parent-directory names.
        stem = _stem(filename)

        drawing_number, pattern_name, confidence = _match_drawing_number(stem)
        revision = _match_revision(stem)

        # Discipline is recorded only as a side observation in the
        # ``matched_pattern`` field for now — FilenameHints itself does not
        # have a discipline column. The orchestrator pulls discipline from
        # the broader extraction result.
        discipline = _infer_discipline(filename)
        if discipline is not None and pattern_name is not None:
            pattern_name = f"{pattern_name}+{discipline.value}"
        elif discipline is not None:
            pattern_name = f"discipline:{discipline.value}"

        return FilenameHints(
            drawing_number_hint=drawing_number,
            revision_hint=revision,
            confidence=confidence,
            matched_pattern=pattern_name,
        )

    def infer_discipline(self, filename: str) -> Discipline | None:
        """Public helper exposing discipline inference for downstream callers.

        Not part of the :class:`IFilenameParser` Protocol — extractors that
        need the discipline can call this directly.
        """
        return _infer_discipline(filename)
