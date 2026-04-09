"""Stage 2 companion document generation — implements ICompanionGenerator (CAP-017).

The :class:`CompanionGenerator` produces a structured markdown "companion
document" that describes the visual content of a PDF in human-readable form.
The companion is later indexed in Elasticsearch (CAP-024) and used by the
review queue UI to give human reviewers a quick visual summary of a drawing
without having to open the PDF itself.

Pipeline:

1.  Determine which pages to render. We always describe the first two pages
    and the last two pages — the title block and the revision/notes block on
    the back of a typical construction drawing — capped at
    :data:`MAX_COMPANION_PAGES` (4) total. Single-page documents render only
    page 0; two/three-page documents render pages 0 and 1; four-or-more page
    documents render pages 0, 1, -2, and -1 (with deduplication if -2/-1
    happen to overlap pages 0/1).
2.  Render those pages via :class:`IPDFProcessor.render_pages`.
3.  For each rendered page, send the JPEG to the vision model with
    :data:`COMPANION_DESCRIPTION_PROMPT_V1` and parse the response with
    :class:`IResponseParser`.
4.  Concatenate the per-page ``visual_description`` and ``technical_details``
    fields into the structured markdown body and append a small ``METADATA``
    block sourced from :class:`ExtractionResult`.

Skip rules (returns ``CompanionResult(skipped=True, ...)``):

* PDF has zero pages (degenerate / empty PDF).
* Text-only PDF detection: ``len(extracted_text) > TEXT_ONLY_THRESHOLD_CHARS``
  AND ``page_count > TEXT_ONLY_THRESHOLD_PAGES``. Text-heavy documents (specs,
  RFI letters, meeting minutes) get nothing useful from a visual companion
  pass and burn vision-model time, so we short-circuit them.

Failure tolerance:

The generator never raises during normal operation. If individual page
descriptions fail (vision model timeout, JSON parse failure) the affected
page is silently dropped from the combined description rather than failing
the whole companion stage. If ALL pages fail, the generator returns a
``CompanionResult`` with empty text and ``companion_generated=False`` so the
orchestrator can record the failure in the pipeline trace and proceed.

Layering: this module lives in the domain.pipeline layer. It imports only
from :mod:`zubot_ingestion.shared`, :mod:`zubot_ingestion.domain.entities`,
:mod:`zubot_ingestion.domain.protocols`, and stdlib. It MUST NOT import from
:mod:`zubot_ingestion.infrastructure` or :mod:`zubot_ingestion.api`.
"""

from __future__ import annotations

import logging
from typing import Any

from zubot_ingestion.domain.entities import (
    CompanionResult,
    ExtractionResult,
    PipelineContext,
    RenderedPage,
)
from zubot_ingestion.domain.protocols import (
    ICompanionGenerator,
    IOllamaClient,
    IPDFProcessor,
    IResponseParser,
)
from zubot_ingestion.shared.constants import (
    COMPANION_DESCRIPTION_PROMPT_V1,
    MAX_COMPANION_PAGES,
    OLLAMA_MODEL_VISION,
    OLLAMA_TEMPERATURE_DETERMINISTIC,
    TEXT_ONLY_THRESHOLD_CHARS,
    TEXT_ONLY_THRESHOLD_PAGES,
)

__all__ = ["CompanionGenerator"]

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema used when parsing per-page vision responses
# ---------------------------------------------------------------------------

# The response parser fills in any missing keys with the default values from
# this dict. Both fields default to None so a totally unparseable response
# yields a per-page record with no visual_description / technical_details
# rather than raising.
_COMPANION_PAGE_SCHEMA: dict[str, Any] = {
    "visual_description": None,
    "technical_details": None,
}


# ---------------------------------------------------------------------------
# Page selection
# ---------------------------------------------------------------------------


def _select_pages_to_render(page_count: int) -> list[int]:
    """Pick the page indices to send to the vision model for description.

    Selection rules (per task spec):

    * First two pages: ``[0, 1]`` if ``page_count >= 2`` else ``[0]``.
    * Last two pages: ``[-2, -1]`` only if ``page_count >= 4``.
    * Deduplicate while preserving order so a 2- or 3-page document does
      not double-render the same page just because the "last two" rule
      happened to overlap.
    * Cap at :data:`MAX_COMPANION_PAGES` (4) entries total. With the
      current rules the natural cap is exactly four, but the explicit
      slice keeps the function safe if the rules are extended later.

    For ``page_count == 0`` returns an empty list — callers should already
    have short-circuited via the skip rule.

    Examples:
        >>> _select_pages_to_render(0)
        []
        >>> _select_pages_to_render(1)
        [0]
        >>> _select_pages_to_render(2)
        [0, 1]
        >>> _select_pages_to_render(3)
        [0, 1]
        >>> _select_pages_to_render(4)
        [0, 1, -2, -1]
        >>> _select_pages_to_render(50)
        [0, 1, -2, -1]
    """
    if page_count <= 0:
        return []
    selected: list[int] = []
    if page_count >= 2:
        selected.extend((0, 1))
    else:
        selected.append(0)
    if page_count >= 4:
        selected.extend((-2, -1))
    # Deduplicate while preserving order. We don't normalize negative
    # indices to positive because the page renderer (IPDFProcessor) accepts
    # negative indices natively, and the index list is part of the
    # CompanionResult contract: callers expect [0, 1, -2, -1] for a long
    # document, not [0, 1, 48, 49].
    deduped: list[int] = []
    for page in selected:
        if page not in deduped:
            deduped.append(page)
    return deduped[:MAX_COMPANION_PAGES]


# ---------------------------------------------------------------------------
# Markdown assembly
# ---------------------------------------------------------------------------


def _format_metadata_value(value: Any) -> str:
    """Render an extraction-result field as a human-readable string.

    Returns ``"N/A"`` for ``None`` so the metadata block is always
    structurally consistent. Enum values are rendered as their ``.value``
    attribute (e.g. ``DocumentType.TECHNICAL_DRAWING -> 'technical_drawing'``).
    """
    if value is None:
        return "N/A"
    if hasattr(value, "value"):
        # Enum-like — use the string value to keep the markdown stable
        return str(value.value)
    return str(value)


def _assemble_companion_markdown(
    visual_descriptions: list[str],
    technical_details: list[str],
    extraction_result: ExtractionResult,
) -> str:
    """Assemble the final markdown body for the companion document.

    Per the CAP-017 task spec:

        # VISUAL DESCRIPTION
        {combined_visual_descriptions}

        # TECHNICAL DETAILS
        {combined_technical_details}

        # METADATA
        Drawing Number: {drawing_number}
        Title: {title}
        Document Type: {document_type}
    """
    visual_block = "\n\n".join(d.strip() for d in visual_descriptions if d and d.strip())
    technical_block = "\n\n".join(d.strip() for d in technical_details if d and d.strip())

    metadata_lines = [
        f"Drawing Number: {_format_metadata_value(extraction_result.drawing_number)}",
        f"Title: {_format_metadata_value(extraction_result.title)}",
        f"Document Type: {_format_metadata_value(extraction_result.document_type)}",
    ]

    return (
        "# VISUAL DESCRIPTION\n"
        f"{visual_block}\n"
        "\n"
        "# TECHNICAL DETAILS\n"
        f"{technical_block}\n"
        "\n"
        "# METADATA\n"
        + "\n".join(metadata_lines)
    )


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class CompanionGenerator(ICompanionGenerator):
    """Generate visual description companion documents for PDFs (CAP-017).

    This class is stateless apart from its injected collaborators. Construct
    once at composition root time and reuse across pipeline runs.
    """

    def __init__(
        self,
        pdf_processor: IPDFProcessor,
        ollama_client: IOllamaClient,
        response_parser: IResponseParser,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._pdf_processor = pdf_processor
        self._ollama_client = ollama_client
        self._response_parser = response_parser
        self._log = logger or _LOG

    async def generate(
        self,
        context: PipelineContext,
        extraction_result: ExtractionResult,
    ) -> CompanionResult:
        """Generate a companion document for the PDF in ``context``.

        Args:
            context: The shared pipeline context. Must have ``pdf_data``
                populated by an earlier stage (the orchestrator's ``pdf_load``
                stage). ``pdf_bytes`` is read directly from the context.
            extraction_result: The Stage 1 extraction result, used to
                populate the metadata footer of the companion markdown.

        Returns:
            A :class:`CompanionResult`. ``companion_generated`` is True if the
            generator produced any text at all; False if the generator was
            skipped (empty PDF, text-only heuristic) or if every page failed
            to produce a description.
        """
        # ------------------------------------------------------------------
        # Skip rule 1: empty / missing PDF data
        # ------------------------------------------------------------------
        pdf_data = context.pdf_data
        if pdf_data is None or pdf_data.page_count == 0:
            self._log.info(
                "companion_skipped_empty_pdf",
                extra={"job_id": str(context.job.job_id)},
            )
            return CompanionResult(
                companion_text="",
                pages_described=0,
                companion_generated=False,
                validation_passed=False,
                quality_score=None,
            )

        # ------------------------------------------------------------------
        # Skip rule 2: text-only heuristic
        # ------------------------------------------------------------------
        if self._is_text_only(context, pdf_data.page_count):
            self._log.info(
                "companion_skipped_text_only",
                extra={
                    "job_id": str(context.job.job_id),
                    "page_count": pdf_data.page_count,
                    "extracted_text_len": (
                        len(context.extracted_text) if context.extracted_text else 0
                    ),
                },
            )
            return CompanionResult(
                companion_text="",
                pages_described=0,
                companion_generated=False,
                validation_passed=False,
                quality_score=None,
            )

        # ------------------------------------------------------------------
        # Render selected pages
        # ------------------------------------------------------------------
        page_numbers = _select_pages_to_render(pdf_data.page_count)
        if not page_numbers:
            # Defensive: page_count > 0 but selection returned nothing.
            return CompanionResult(
                companion_text="",
                pages_described=0,
                companion_generated=False,
                validation_passed=False,
                quality_score=None,
            )

        try:
            rendered_pages: list[RenderedPage] = self._pdf_processor.render_pages(
                context.pdf_bytes, page_numbers
            )
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            self._log.warning(
                "companion_render_failed",
                extra={
                    "job_id": str(context.job.job_id),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return CompanionResult(
                companion_text="",
                pages_described=0,
                companion_generated=False,
                validation_passed=False,
                quality_score=None,
            )

        # ------------------------------------------------------------------
        # Per-page vision inference + parsing
        # ------------------------------------------------------------------
        visual_descriptions: list[str] = []
        technical_details: list[str] = []
        successful_pages = 0

        for rendered in rendered_pages:
            visual, technical = await self._describe_one_page(
                rendered, job_id=str(context.job.job_id)
            )
            if visual or technical:
                successful_pages += 1
                if visual:
                    visual_descriptions.append(visual)
                if technical:
                    technical_details.append(technical)

        if successful_pages == 0:
            self._log.warning(
                "companion_all_pages_failed",
                extra={
                    "job_id": str(context.job.job_id),
                    "attempted_pages": len(rendered_pages),
                },
            )
            return CompanionResult(
                companion_text="",
                pages_described=0,
                companion_generated=False,
                validation_passed=False,
                quality_score=None,
            )

        companion_text = _assemble_companion_markdown(
            visual_descriptions=visual_descriptions,
            technical_details=technical_details,
            extraction_result=extraction_result,
        )

        return CompanionResult(
            companion_text=companion_text,
            pages_described=successful_pages,
            companion_generated=True,
            # Validation is run by ICompanionValidator in the orchestrator
            # AFTER this call returns. The companion generator never sets
            # this field to True; the orchestrator wires a separate
            # ValidationResult into the ConfidenceCalculator.
            validation_passed=False,
            quality_score=None,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_text_only(self, context: PipelineContext, page_count: int) -> bool:
        """Return True if the PDF should be treated as text-only.

        Heuristic: ``len(extracted_text) > TEXT_ONLY_THRESHOLD_CHARS`` AND
        ``page_count > TEXT_ONLY_THRESHOLD_PAGES``. Both conditions must be
        true to skip — a 50-page PDF with 200 chars of text is still worth
        rendering, and so is a 5-page PDF with 50 KB of text.
        """
        extracted_text = context.extracted_text
        if extracted_text is None:
            return False
        if len(extracted_text) <= TEXT_ONLY_THRESHOLD_CHARS:
            return False
        if page_count <= TEXT_ONLY_THRESHOLD_PAGES:
            return False
        return True

    async def _describe_one_page(
        self,
        rendered: RenderedPage,
        *,
        job_id: str,
    ) -> tuple[str | None, str | None]:
        """Run vision inference + parsing for a single rendered page.

        Returns ``(visual_description, technical_details)``. Either field
        may be ``None`` if the parser couldn't extract it. Both will be
        ``None`` on a hard failure (vision model raised, response parser
        produced an empty dict, etc.) and the page will be silently dropped
        from the combined output.
        """
        try:
            response = await self._ollama_client.generate_vision(
                image_base64=rendered.base64_jpeg,
                prompt=COMPANION_DESCRIPTION_PROMPT_V1,
                model=OLLAMA_MODEL_VISION,
                temperature=OLLAMA_TEMPERATURE_DETERMINISTIC,
            )
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            self._log.warning(
                "companion_vision_failed",
                extra={
                    "job_id": job_id,
                    "page_number": rendered.page_number,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return None, None

        try:
            parsed = await self._response_parser.parse(
                response.response_text,
                expected_schema=_COMPANION_PAGE_SCHEMA,
            )
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            self._log.warning(
                "companion_parse_failed",
                extra={
                    "job_id": job_id,
                    "page_number": rendered.page_number,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return None, None

        visual = _coerce_to_str(parsed.get("visual_description"))
        technical = _coerce_to_str(parsed.get("technical_details"))
        return visual, technical


def _coerce_to_str(value: Any) -> str | None:
    """Coerce a parsed JSON value to a non-empty string or ``None``.

    The vision-model JSON parser returns whatever shape the model emits.
    Most of the time we get a clean string, but defensively handle the
    cases where the model returns a number, a list of bullet points, or
    an empty string.
    """
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, list):
        # Some models return bullet points as a list of strings; join them
        # so the markdown body remains a single readable paragraph.
        joined = "\n".join(str(item).strip() for item in value if str(item).strip())
        return joined or None
    return str(value)
