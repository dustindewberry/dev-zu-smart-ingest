"""Stage 1 title extractor (CAP-014).

Extracts the human-readable drawing title from a PDF using two sources:

1.  **Vision** — render page 0 and send the JPEG to ``qwen2.5vl:7b`` with
    ``TITLE_VISION_PROMPT_V1``. The vision model reads the title block.
2.  **Text**   — analyse the first AND last page of extracted text with
    ``TITLE_TEXT_PROMPT_V1``. We pass both pages because the title may
    appear in either the cover sheet or the trailing title block.

The two answers are reconciled with a simple priority + confidence rule:

* If both sources agree (case-insensitive, whitespace-collapsed): keep the
  vision string and report ``confidence = max(vision_conf, text_conf, 0.9)``
  to reward agreement.
* If only one source produced a value: keep that value and use its
  reported confidence (clamped to ``[0.0, 1.0]``).
* If they disagree: prefer vision (it sees the title block layout
  directly) and use the average of the two reported confidences,
  multiplied by 0.7 to penalise disagreement.

Multi-line title strings returned by the model are concatenated into a
single line with single spaces.

This module lives in the *domain* layer. It depends only on the domain
protocols and on :class:`JsonResponseParser`. It MUST NOT instantiate any
infrastructure adapter directly.
"""

from __future__ import annotations

import logging
import re
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
    "TitleExtractor",
    "TITLE_VISION_PROMPT_V1",
    "TITLE_TEXT_PROMPT_V1",
]

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt templates (kept in sync with infrastructure/ollama/prompts.py)
# ---------------------------------------------------------------------------

TITLE_VISION_PROMPT_V1: str = """\
You are an expert at reading construction drawings.

Look at this image of a construction drawing page. Find the DRAWING TITLE \
inside the title block. The title is the human-readable description of \
what the drawing depicts (e.g., "GROUND FLOOR PLAN — BUILDING A", \
"FOUNDATION DETAILS", "ELECTRICAL RISER DIAGRAM").

Return STRICT JSON only — no markdown, no commentary, no code fences. \
Use this exact schema:

{
  "title": "<string or null>",
  "confidence": <float between 0.0 and 1.0>
}

Rules:
- Concatenate multi-line titles into a single string with single spaces.
- Strip leading/trailing whitespace.
- If no title is visible, return null for title and 0.0 for confidence.
- Do not invent values. Do not include any text outside the JSON object.
- This call is made with temperature=0; respond deterministically.
"""

TITLE_TEXT_PROMPT_V1: str = """\
You are an expert at parsing construction drawing text.

Below is the extracted text content of a construction drawing PDF. Find \
the DRAWING TITLE — the human-readable description such as "GROUND FLOOR \
PLAN", "FOUNDATION DETAILS", or "ELECTRICAL RISER DIAGRAM". It is usually \
adjacent to a label like "TITLE", "DRAWING TITLE", or "SHEET TITLE".

Return STRICT JSON only — no markdown, no commentary, no code fences. \
Use this exact schema:

{
  "title": "<string or null>",
  "confidence": <float between 0.0 and 1.0>
}

Rules:
- Concatenate multi-line titles into a single string with single spaces.
- Strip leading/trailing whitespace.
- If no title is found, return null for title and 0.0 for confidence.
- Do not invent values. Do not include any text outside the JSON object.
- This call is made with temperature=0; respond deterministically.
"""


_TITLE_SCHEMA: dict[str, Any] = {
    "title": None,
    "confidence": 0.0,
}

_WHITESPACE_RE: re.Pattern[str] = re.compile(r"\s+")


def _flatten_title(value: str | None) -> str | None:
    """Collapse multi-line whitespace into single spaces, strip ends.

    Returns ``None`` if the input is empty after flattening.
    """
    if value is None:
        return None
    flattened = _WHITESPACE_RE.sub(" ", value).strip()
    return flattened or None


def _normalise_for_compare(value: str | None) -> str | None:
    """Lowercase + collapse whitespace, used only for equality checks."""
    flat = _flatten_title(value)
    if flat is None:
        return None
    return flat.lower()


def _coerce_confidence(value: Any) -> float:
    """Coerce a parser-returned ``confidence`` field into ``[0.0, 1.0]``."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _fuse(
    vision_title: str | None,
    vision_conf: float,
    text_title: str | None,
    text_conf: float,
) -> tuple[str | None, float]:
    """Combine the two source answers into a single ``(title, confidence)``.

    See module docstring for the rules.
    """
    v = _flatten_title(vision_title)
    t = _flatten_title(text_title)
    v_norm = _normalise_for_compare(v)
    t_norm = _normalise_for_compare(t)

    if v is None and t is None:
        return None, 0.0
    if v is not None and t is None:
        return v, vision_conf
    if v is None and t is not None:
        return t, text_conf

    # Both present
    assert v is not None and t is not None
    if v_norm == t_norm:
        return v, max(vision_conf, text_conf, 0.9)

    # Disagreement: prefer vision, penalise.
    return v, ((vision_conf + text_conf) / 2.0) * 0.7


class TitleExtractor:
    """Concrete title extractor implementing :class:`IExtractor`.

    Constructor injection (same shape as DrawingNumberExtractor for
    consistent orchestrator wiring):
        pdf_processor:    IPDFProcessor implementation
        ollama_client:    IOllamaClient implementation
        response_parser:  JsonResponseParser instance
        filename_parser:  IFilenameParser implementation (unused for title
                          extraction itself, but kept in the constructor
                          for uniform DI wiring)
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
        self._filename = filename_parser  # not used directly, kept for DI uniformity
        self._vision_model = vision_model
        self._text_model = text_model
        self._log = logger or _LOG

    # ------------------------------------------------------------------
    # Source extractors
    # ------------------------------------------------------------------

    async def _from_vision(
        self, context: PipelineContext
    ) -> tuple[str | None, float]:
        try:
            page = self._get_or_render_first_page(context)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("title vision: render failed: %s", exc)
            return None, 0.0
        try:
            response = await self._ollama.generate_vision(
                image_base64=page.base64_jpeg,
                prompt=TITLE_VISION_PROMPT_V1,
                model=self._vision_model,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("title vision: ollama failed: %s", exc)
            return None, 0.0
        parsed = await self._parser.parse(response.response_text, _TITLE_SCHEMA)
        return parsed.get("title"), _coerce_confidence(parsed.get("confidence"))

    async def _from_text(
        self, context: PipelineContext
    ) -> tuple[str | None, float]:
        first_last = self._first_and_last_page_text(context)
        if not first_last:
            return None, 0.0
        try:
            response = await self._ollama.generate_text(
                text=first_last,
                prompt=TITLE_TEXT_PROMPT_V1,
                model=self._text_model,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("title text: ollama failed: %s", exc)
            return None, 0.0
        parsed = await self._parser.parse(response.response_text, _TITLE_SCHEMA)
        return parsed.get("title"), _coerce_confidence(parsed.get("confidence"))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_or_render_first_page(self, context: PipelineContext) -> RenderedPage:
        for page in context.rendered_pages:
            if page.page_number == 0:
                return page
        page = self._pdf.render_page(context.pdf_bytes, page_number=0)
        context.rendered_pages.append(page)
        return page

    def _first_and_last_page_text(self, context: PipelineContext) -> str:
        """Return text from page 0 + the last page (or full text if short).

        We rely on the page-separator markers emitted by
        :class:`PyMuPDFProcessor.extract_text`. If the separator is missing
        we fall back to the full extracted text.
        """
        if context.extracted_text is None:
            try:
                context.extracted_text = self._pdf.extract_text(context.pdf_bytes)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("title text: extract_text failed: %s", exc)
                context.extracted_text = ""

        text = context.extracted_text
        if not text:
            return ""

        # Page separator from infrastructure/pdf/processor.py is
        # "\n\n---PAGE {n}---\n\n". We split on the literal "---PAGE "
        # token which is robust to the page-number value.
        parts = text.split("---PAGE ")
        # parts[0] is the prefix before page 1; parts[1:] each start with
        # "<n>---\n\n<body>"
        if len(parts) <= 2:
            return text  # one or zero pages, return everything
        # First page body
        first_body = parts[1]
        if "---\n\n" in first_body:
            first_body = first_body.split("---\n\n", 1)[1]
        # Last page body
        last_body = parts[-1]
        if "---\n\n" in last_body:
            last_body = last_body.split("---\n\n", 1)[1]
        return f"{first_body}\n\n{last_body}"

    # ------------------------------------------------------------------
    # IExtractor.extract
    # ------------------------------------------------------------------

    async def extract(self, context: PipelineContext) -> ExtractionResult:
        """Run vision + text extraction in parallel and fuse them."""
        import asyncio

        vision_task = asyncio.create_task(self._from_vision(context))
        text_task = asyncio.create_task(self._from_text(context))
        (v_title, v_conf), (t_title, t_conf) = await asyncio.gather(
            vision_task, text_task
        )

        title, confidence = _fuse(v_title, v_conf, t_title, t_conf)

        sources_used: list[str] = []
        if v_title is not None:
            sources_used.append("vision")
        if t_title is not None:
            sources_used.append("text")

        return ExtractionResult(
            drawing_number=None,
            drawing_number_confidence=0.0,
            title=title,
            title_confidence=confidence,
            document_type=None,
            document_type_confidence=0.0,
            sources_used=sources_used,
        )
