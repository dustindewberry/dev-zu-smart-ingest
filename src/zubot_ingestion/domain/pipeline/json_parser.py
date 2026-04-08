"""Best-effort JSON response parser for Ollama outputs.

Implements (a superset of) :class:`~zubot_ingestion.domain.protocols.IResponseParser`.
The Stage 1 extractors call this parser to turn an Ollama ``response_text``
into a Python ``dict``. The parser MUST NOT raise — every failure mode falls
back to a partial dict (with ``None`` values for missing keys). This invariant
is what allows the orchestrator to keep going when one of three extraction
sources misbehaves.

Repair strategies, in order:

1.  ``json.loads`` directly on the response text.
2.  ``json.loads`` on the substring between the first ``{`` and the last ``}``
    (so we can strip code-fence noise from the model).
3.  ``json_repair.repair_json`` for malformed-but-recoverable JSON.
4.  Regex-based key-value extraction as a final, lossy best effort.

If ``expected_schema`` (a mapping of field name → default value) is provided,
the result is filled out so every expected key is present. Missing keys take
the value supplied in the schema (typically ``None``). Extra keys returned by
the model are preserved verbatim.

This module may only import from stdlib and the third-party
``json_repair`` library, per the dependency rules in boundary-contracts.md
§3 (the rules permit third-party libs as long as no infrastructure module
is imported).

Note on the canonical Protocol signature
----------------------------------------
:class:`~zubot_ingestion.domain.protocols.IResponseParser` declares
``parse(raw_response: str, expected_schema: type[T]) -> T`` (sync, may raise).
The task spec for step-14 prescribes an async, dict-based interface that never
raises. The two are deliberately reconciled here: the async :meth:`parse`
implements the task spec, and a sync :meth:`parse_with_schema` exposes the
Protocol shape for downstream callers that prefer it. Both methods share the
underlying repair pipeline.
"""

from __future__ import annotations

import json
import re
from typing import Any

from json_repair import repair_json

__all__ = ["JsonResponseParser"]


# ---------------------------------------------------------------------------
# Regex used by the final fallback strategy
# ---------------------------------------------------------------------------

# Matches "key": "value", "key": 0.95, "key": null, "key": true, etc. Tolerates
# both single and double quotes around the key, and extracts string, numeric,
# bool, or null values. NOT a full JSON parser — only intended as a last resort.
_KV_PATTERN: re.Pattern[str] = re.compile(
    r"""
    (?P<key>["'][^"']+["'])      # quoted key
    \s*:\s*
    (?P<value>
        "(?:[^"\\]|\\.)*"        # double-quoted string
        | '(?:[^'\\]|\\.)*'      # single-quoted string
        | -?\d+(?:\.\d+)?        # number
        | true | false | null    # JSON literals
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code-fence wrappers like ```json ... ```."""
    s = text.strip()
    if s.startswith("```"):
        # remove the opening fence (and optional language tag) and the
        # trailing fence, if any
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()


def _slice_outer_braces(text: str) -> str | None:
    """Return the substring between the first ``{`` and the last ``}``."""
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    return text[first : last + 1]


def _coerce_kv_value(raw: str) -> Any:
    """Convert a raw regex-captured value into a Python primitive."""
    raw = raw.strip()
    lowered = raw.lower()
    if lowered == "null":
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        # Strip quotes and unescape simple sequences
        inner = raw[1:-1]
        return inner.encode("utf-8").decode("unicode_escape", errors="replace")
    # Numeric
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _regex_extract(text: str) -> dict[str, Any]:
    """Final fallback: pull ``"key": value`` pairs out of arbitrary text."""
    out: dict[str, Any] = {}
    for match in _KV_PATTERN.finditer(text):
        key_token = match.group("key").strip()
        # Strip surrounding quotes from the key
        key = key_token[1:-1]
        out[key] = _coerce_kv_value(match.group("value"))
    return out


def _attempt_strict_json(text: str) -> dict[str, Any] | None:
    """Try ``json.loads``; return ``None`` on any failure."""
    try:
        result = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(result, dict):
        return result
    return None


def _attempt_repair_json(text: str) -> dict[str, Any] | None:
    """Try ``json_repair.repair_json``; return ``None`` on any failure.

    ``repair_json`` returns a string by default; we re-parse the string with
    ``json.loads``. If the repaired string is empty (the library's signal
    for "irrecoverable") we return ``None``.
    """
    try:
        repaired = repair_json(text)
    except Exception:  # pragma: no cover - json_repair is forgiving
        return None
    if not repaired:
        return None
    return _attempt_strict_json(repaired)


def _apply_schema(
    parsed: dict[str, Any] | None,
    expected_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge ``parsed`` into a new dict that has every key from ``expected_schema``.

    - Missing keys in ``parsed`` take the default value from
      ``expected_schema`` (typically ``None``).
    - Extra keys in ``parsed`` are preserved verbatim.
    - If ``expected_schema`` is ``None``, returns ``parsed`` (or ``{}`` if
      ``parsed`` is also ``None``).
    """
    if expected_schema is None:
        return dict(parsed) if parsed else {}
    out: dict[str, Any] = {key: default for key, default in expected_schema.items()}
    if parsed:
        for key, value in parsed.items():
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class JsonResponseParser:
    """Best-effort JSON parser with multi-stage repair fallbacks.

    Stateless and side-effect-free. Construct once and reuse.
    """

    async def parse(
        self,
        response_text: str,
        expected_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Parse a model response into a dict, never raising.

        Args:
            response_text: The raw text returned by the Ollama model.
            expected_schema: Optional ``{key: default}`` mapping describing
                the dict shape the caller expects. Missing keys are filled
                with the default values; extras from the model are kept.

        Returns:
            A dict. May be empty if the input is unrecoverable AND no
            ``expected_schema`` is provided.
        """
        return self._parse_sync(response_text, expected_schema)

    def parse_with_schema(
        self,
        raw_response: str,
        expected_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Synchronous variant of :meth:`parse` with the same semantics."""
        return self._parse_sync(raw_response, expected_schema)

    # ------------------------------------------------------------------
    # Internal: the actual parsing pipeline
    # ------------------------------------------------------------------

    def _parse_sync(
        self,
        text: str | None,
        expected_schema: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if text is None:
            return _apply_schema(None, expected_schema)
        cleaned = _strip_code_fences(text)
        if not cleaned:
            return _apply_schema(None, expected_schema)

        # Strategy 1: strict JSON on the cleaned text
        parsed = _attempt_strict_json(cleaned)
        if parsed is not None:
            return _apply_schema(parsed, expected_schema)

        # Strategy 2: strict JSON on the {...} slice
        sliced = _slice_outer_braces(cleaned)
        if sliced is not None:
            parsed = _attempt_strict_json(sliced)
            if parsed is not None:
                return _apply_schema(parsed, expected_schema)

            # Strategy 3: json_repair on the slice
            parsed = _attempt_repair_json(sliced)
            if parsed is not None:
                return _apply_schema(parsed, expected_schema)

        # Strategy 3 (whole text): json_repair on everything
        parsed = _attempt_repair_json(cleaned)
        if parsed is not None:
            return _apply_schema(parsed, expected_schema)

        # Strategy 4: regex-based key/value extraction
        parsed = _regex_extract(cleaned)
        return _apply_schema(parsed, expected_schema)
