"""Stage 1 drawing-number extractor (CAP-013).

Multi-source extraction of the drawing number from a single PDF page:

1.  **Vision** — render page 0 and send the JPEG to ``qwen2.5vl:7b`` with
    ``DRAWING_NUMBER_VISION_PROMPT_V1``.
2.  **Text**   — send the extracted text to the text model with
    ``DRAWING_NUMBER_TEXT_PROMPT_V1``.
3.  **Filename** — apply :class:`IFilenameParser` to ``job.filename``.

The three sources run in parallel via :func:`asyncio.gather`. Their answers
are then fused with a simple weighted-voting algorithm:

* All 3 sources agree (case-insensitive, whitespace-normalised) → 0.95
* Exactly 2 of 3 agree                                          → 0.75
* Only 1 source produced a value (or the 3 produced 3 different values) → 0.50
* Total disagreement (>= 2 distinct non-null values, none in majority) → 0.30

The chosen drawing number is the majority value when there is one; otherwise
it is the value from the highest-priority source (vision > text > filename).

This module lives in the *domain* layer. It depends only on the domain
protocols (:class:`IPDFProcessor`, :class:`IOllamaClient`,
:class:`IFilenameParser`) and on :class:`JsonResponseParser`. It MUST NOT
instantiate any infrastructure adapter directly — all dependencies arrive
via the constructor.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import Any

from zubot_ingestion.domain.entities import (
    ExtractionResult,
    PipelineContext,
    RenderedPage,
)
from zubot_ingestion.domain.pipeline.json_parser import JsonResponseParser
from zubot_ingestion.domain.protocols import (
    IFilenameParser,
    IOllamaClient,
    IPDFProcessor,
)

__all__ = [
    "DrawingNumberExtractor",
    "DRAWING_NUMBER_VISION_PROMPT_V1",
    "DRAWING_NUMBER_TEXT_PROMPT_V1",
]

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt templates (kept in sync with infrastructure/ollama/prompts.py)
# ---------------------------------------------------------------------------
#
# These constants intentionally mirror the strings in
# ``zubot_ingestion.infrastructure.ollama.prompts`` byte-for-byte. They are
# duplicated here so the domain layer does not import from infrastructure
# (per the dependency rules in boundary-contracts.md §3). Any future change
# to one MUST be applied to the other.

DRAWING_NUMBER_VISION_PROMPT_V1: str = """\
You are an expert at reading construction drawings.

Look at this image of a construction drawing page. Find the DRAWING NUMBER \
inside the title block (typically the lower-right corner). The drawing \
number is a short alphanumeric identifier such as "KXC-B6-001-Y-27-1905-301", \
"170154-L-001", or "A-101".

Return STRICT JSON only — no markdown, no commentary, no code fences. \
Use this exact schema:

{
  "drawing_number": "<string or null>",
  "confidence": <float between 0.0 and 1.0>,
  "source_location": "<short description of where you saw it>"
}

Rules:
- If no drawing number is visible, return null for drawing_number and 0.0 \
for confidence.
- Do not invent values. Do not include any text outside the JSON object.
- This call is made with temperature=0; respond deterministically.
"""

DRAWING_NUMBER_TEXT_PROMPT_V1: str = """\
You are an expert at parsing construction drawing text.

Below is the extracted text content of a construction drawing PDF. Find \
the DRAWING NUMBER, which is typically a short alphanumeric identifier \
such as "KXC-B6-001-Y-27-1905-301", "170154-L-001", or "A-101". It is \
usually adjacent to a label like "DWG NO", "DRAWING NUMBER", or "DRAWING NO".

Return STRICT JSON only — no markdown, no commentary, no code fences. \
Use this exact schema:

{
  "drawing_number": "<string or null>",
  "confidence": <float between 0.0 and 1.0>,
  "source_snippet": "<short snippet of nearby text or null>"
}

Rules:
- If no drawing number is found, return null for drawing_number and 0.0 \
for confidence.
- Do not invent values. Do not include any text outside the JSON object.
- This call is made with temperature=0; respond deterministically.
"""


# Schema dict the parser uses to fill missing keys with sensible defaults.
_DRAWING_NUMBER_SCHEMA: dict[str, Any] = {
    "drawing_number": None,
    "confidence": 0.0,
}


# Confidence levels assigned by the cross-check vote (see module docstring).
CONFIDENCE_ALL_AGREE: float = 0.95
CONFIDENCE_TWO_OF_THREE: float = 0.75
CONFIDENCE_SINGLE_SOURCE: float = 0.50
CONFIDENCE_DISAGREEMENT: float = 0.30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise(value: str | None) -> str | None:
    """Normalise a drawing number for cross-source equality checks.

    Trims whitespace, uppercases, and collapses internal whitespace. Empty
    or ``None`` inputs become ``None`` so the vote ignores them.
    """
    if value is None:
        return None
    s = " ".join(value.split()).strip().upper()
    return s or None


def _fuse(
    vision: str | None,
    text: str | None,
    filename: str | None,
) -> tuple[str | None, float]:
    """Run the weighted vote across the three sources.

    Returns ``(chosen_value, confidence)``.

    The chosen value is:

    * The majority winner when one exists.
    * Otherwise the highest-priority non-null value (vision > text > filename).
    * ``None`` if all three sources are null.
    """
    norm_vision = _normalise(vision)
    norm_text = _normalise(text)
    norm_filename = _normalise(filename)

    candidates = [
        v for v in (norm_vision, norm_text, norm_filename) if v is not None
    ]
    if not candidates:
        return None, 0.0

    counts = Counter(candidates)
    most_common_value, most_common_count = counts.most_common(1)[0]

    if len(candidates) == 3 and most_common_count == 3:
        return most_common_value, CONFIDENCE_ALL_AGREE

    if most_common_count >= 2:
        return most_common_value, CONFIDENCE_TWO_OF_THREE

    if len(candidates) == 1:
        # Only one source contributed; use its value at "single source" confidence.
        return candidates[0], CONFIDENCE_SINGLE_SOURCE

    # 2 or 3 different non-null values, none in majority → disagreement.
    # Pick by source priority: vision > text > filename.
    chosen = norm_vision or norm_text or norm_filename
    return chosen, CONFIDENCE_DISAGREEMENT


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class DrawingNumberExtractor:
    """Concrete drawing-number extractor implementing :class:`IExtractor`.

    Constructor injection:
        pdf_processor:    IPDFProcessor implementation
        ollama_client:    IOllamaClient implementation
        response_parser:  JsonResponseParser instance
        filename_parser:  IFilenameParser implementation
        vision_model:     Optional override for the vision model name
        text_model:       Optional override for the text model name
        logger:           Optional logger (defaults to module logger)
    """

    def __init__(
        self,
        pdf_processor: IPDFProcessor,
        ollama_client: IOllamaClient,
        response_parser: JsonResponseParser,
        filename_parser: IFilenameParser,
        *,
        vision_model: str = "qwen2.5vl:7b",
        text_model: str = "qwen2.5:7b",
        logger: logging.Logger | None = None,
    ) -> None:
        self._pdf = pdf_processor
        self._ollama = ollama_client
        self._parser = response_parser
        self._filename = filename_parser
        self._vision_model = vision_model
        self._text_model = text_model
        self._log = logger or _LOG

    # ------------------------------------------------------------------
    # Source extractors (one coroutine per source)
    # ------------------------------------------------------------------

    async def _from_vision(self, context: PipelineContext) -> tuple[str | None, str]:
        """Extract drawing number from a rendered page-0 image.

        Returns ``(value, raw_response_text)``. ``value`` is ``None`` on any
        failure (the parser never raises, so failures are limited to the PDF
        rendering or the Ollama transport).
        """
        try:
            page = self._get_or_render_first_page(context)
        except Exception as exc:  # noqa: BLE001 - we want to swallow render errors
            self._log.warning("vision: failed to render page 0: %s", exc)
            return None, ""
        try:
            response = await self._ollama.generate_vision(
                image_base64=page.base64_jpeg,
                prompt=DRAWING_NUMBER_VISION_PROMPT_V1,
                model=self._vision_model,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - swallow transport errors
            self._log.warning("vision: ollama generate_vision failed: %s", exc)
            return None, ""

        parsed = await self._parser.parse(
            response.response_text, _DRAWING_NUMBER_SCHEMA
        )
        value = parsed.get("drawing_number")
        return value if isinstance(value, str) else None, response.response_text

    async def _from_text(self, context: PipelineContext) -> tuple[str | None, str]:
        """Extract drawing number from the PDF text content."""
        text = self._get_or_extract_text(context)
        if not text:
            return None, ""
        try:
            response = await self._ollama.generate_text(
                text=text,
                prompt=DRAWING_NUMBER_TEXT_PROMPT_V1,
                model=self._text_model,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("text: ollama generate_text failed: %s", exc)
            return None, ""

        parsed = await self._parser.parse(
            response.response_text, _DRAWING_NUMBER_SCHEMA
        )
        value = parsed.get("drawing_number")
        return value if isinstance(value, str) else None, response.response_text

    async def _from_filename(self, context: PipelineContext) -> str | None:
        """Extract drawing number from the filename via :class:`IFilenameParser`.

        Cached on the pipeline context so the title and document-type
        extractors can reuse it without reparsing.
        """
        if context.filename_hints is None:
            try:
                context.filename_hints = self._filename.parse(context.job.filename)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("filename: parser raised: %s", exc)
                return None
        return context.filename_hints.drawing_number_hint

    # ------------------------------------------------------------------
    # PipelineContext memoisation helpers
    # ------------------------------------------------------------------

    def _get_or_render_first_page(self, context: PipelineContext) -> RenderedPage:
        """Return the cached page-0 render or call the PDF processor once."""
        for page in context.rendered_pages:
            if page.page_number == 0:
                return page
        page = self._pdf.render_page(context.pdf_bytes, page_number=0)
        context.rendered_pages.append(page)
        return page

    def _get_or_extract_text(self, context: PipelineContext) -> str:
        """Return the cached extracted text or call the PDF processor once."""
        if context.extracted_text is None:
            try:
                context.extracted_text = self._pdf.extract_text(context.pdf_bytes)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("text: pdf extract_text failed: %s", exc)
                context.extracted_text = ""
        return context.extracted_text

    # ------------------------------------------------------------------
    # IExtractor.extract
    # ------------------------------------------------------------------

    async def extract(self, context: PipelineContext) -> ExtractionResult:
        """Run the three sources in parallel and fuse their answers."""
        vision_task = asyncio.create_task(self._from_vision(context))
        text_task = asyncio.create_task(self._from_text(context))
        filename_task = asyncio.create_task(self._from_filename(context))

        (vision_value, raw_vision), (text_value, raw_text), filename_value = (
            await asyncio.gather(vision_task, text_task, filename_task)
        )

        chosen, confidence = _fuse(vision_value, text_value, filename_value)

        sources_used: list[str] = []
        if vision_value is not None:
            sources_used.append("vision")
        if text_value is not None:
            sources_used.append("text")
        if filename_value is not None:
            sources_used.append("filename")

        return ExtractionResult(
            drawing_number=chosen,
            drawing_number_confidence=confidence,
            title=None,
            title_confidence=0.0,
            document_type=None,
            document_type_confidence=0.0,
            sources_used=sources_used,
            raw_vision_response=raw_vision or None,
            raw_text_response=raw_text or None,
        )
