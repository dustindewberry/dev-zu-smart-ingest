"""Stage 1 document-type extractor (CAP-015).

Classifies the PDF into one of the 21 values in
:class:`~zubot_ingestion.domain.enums.DocumentType` using the text model.
The prompt enumerates every legal enum value and instructs the model to
choose exactly one. The response is parsed and validated against the
enum: any value not in the enum maps to ``DocumentType.DOCUMENT`` (the
fallback) with reduced confidence.

This module lives in the *domain* layer. It depends only on the domain
protocols, the canonical :class:`DocumentType` enum, and on
:class:`JsonResponseParser`. It MUST NOT instantiate any infrastructure
adapter directly.
"""

from __future__ import annotations

import logging
from typing import Any

from zubot_ingestion.domain.entities import ExtractionResult, PipelineContext
from zubot_ingestion.domain.enums import DocumentType
from zubot_ingestion.domain.pipeline.json_parser import JsonResponseParser
from zubot_ingestion.domain.protocols import (
    IFilenameParser,
    IOllamaClient,
    IPDFProcessor,
)

__all__ = [
    "DocumentTypeExtractor",
    "DOCUMENT_TYPE_TEXT_PROMPT_V1",
]

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt template — built from the canonical enum values at import time
# ---------------------------------------------------------------------------
#
# The enum is the single source of truth for the controlled vocabulary
# (see boundary-contracts.md §5 + reference-architecture.md "Controlled
# Vocabularies"). Building the prompt from the enum guarantees the prompt
# never drifts out of sync with the enum on enum changes.

_ENUM_VALUES: tuple[str, ...] = tuple(member.value for member in DocumentType)
_ENUM_BULLET_LIST: str = "\n".join(f"- {value}" for value in _ENUM_VALUES)

DOCUMENT_TYPE_TEXT_PROMPT_V1: str = f"""\
You are an expert at classifying construction project documents.

Below is the extracted text content of a PDF. Classify the document into \
EXACTLY ONE of the following {len(_ENUM_VALUES)} controlled-vocabulary types:

{_ENUM_BULLET_LIST}

Return STRICT JSON only — no markdown, no commentary, no code fences. \
Use this exact schema:

{{
  "document_type": "<one of the {len(_ENUM_VALUES)} enum values above>",
  "confidence": <float between 0.0 and 1.0>,
  "reasoning": "<one short sentence>"
}}

Rules:
- The document_type field MUST be one of the {len(_ENUM_VALUES)} values \
listed above, verbatim and lowercase. If you are unsure, return "document".
- Do not invent new types. Do not include any text outside the JSON object.
- This call is made with temperature=0; respond deterministically.
"""


_DOCUMENT_TYPE_SCHEMA: dict[str, Any] = {
    "document_type": None,
    "confidence": 0.0,
}

# Penalty applied when the model returns a value that is not in the enum;
# we still keep DocumentType.DOCUMENT as the result but at reduced confidence.
_FALLBACK_CONFIDENCE: float = 0.30


def _coerce_confidence(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _coerce_document_type(value: Any) -> tuple[DocumentType | None, bool]:
    """Map a model-returned string to a :class:`DocumentType` enum value.

    Returns ``(enum_value, was_in_vocabulary)``. ``enum_value`` is ``None``
    only when ``value`` is empty/None; otherwise it falls back to
    :attr:`DocumentType.DOCUMENT` and ``was_in_vocabulary=False``.
    """
    if value is None:
        return None, False
    if not isinstance(value, str):
        value = str(value)
    candidate = value.strip().lower()
    if not candidate:
        return None, False
    try:
        return DocumentType(candidate), True
    except ValueError:
        return DocumentType.DOCUMENT, False


class DocumentTypeExtractor:
    """Concrete document-type extractor implementing :class:`IExtractor`.

    Constructor injection (uniform shape with the other Stage 1 extractors):
        pdf_processor:    IPDFProcessor implementation
        ollama_client:    IOllamaClient implementation
        response_parser:  JsonResponseParser instance
        filename_parser:  IFilenameParser implementation (kept for DI uniformity)
    """

    def __init__(
        self,
        pdf_processor: IPDFProcessor,
        ollama_client: IOllamaClient,
        response_parser: JsonResponseParser,
        filename_parser: IFilenameParser,
        *,
        text_model: str = "qwen2.5:7b",
        logger: logging.Logger | None = None,
    ) -> None:
        self._pdf = pdf_processor
        self._ollama = ollama_client
        self._parser = response_parser
        self._filename = filename_parser
        self._text_model = text_model
        self._log = logger or _LOG

    async def extract(self, context: PipelineContext) -> ExtractionResult:
        """Classify the document into a :class:`DocumentType` value."""
        text = self._get_or_extract_text(context)
        if not text:
            return ExtractionResult(
                drawing_number=None,
                drawing_number_confidence=0.0,
                title=None,
                title_confidence=0.0,
                document_type=None,
                document_type_confidence=0.0,
                sources_used=[],
            )

        try:
            response = await self._ollama.generate_text(
                text=text,
                prompt=DOCUMENT_TYPE_TEXT_PROMPT_V1,
                model=self._text_model,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("document_type: ollama failed: %s", exc)
            return ExtractionResult(
                drawing_number=None,
                drawing_number_confidence=0.0,
                title=None,
                title_confidence=0.0,
                document_type=None,
                document_type_confidence=0.0,
                sources_used=[],
            )

        parsed = await self._parser.parse(
            response.response_text, _DOCUMENT_TYPE_SCHEMA
        )
        enum_value, was_in_vocab = _coerce_document_type(parsed.get("document_type"))
        reported_confidence = _coerce_confidence(parsed.get("confidence"))

        if enum_value is None:
            confidence = 0.0
        elif was_in_vocab:
            confidence = reported_confidence
        else:
            # Model emitted an unknown vocabulary value. Keep the fallback
            # but cap confidence at the lesser of the model's reported value
            # and our fallback ceiling so we don't trust an out-of-vocab guess.
            confidence = min(reported_confidence, _FALLBACK_CONFIDENCE)

        return ExtractionResult(
            drawing_number=None,
            drawing_number_confidence=0.0,
            title=None,
            title_confidence=0.0,
            document_type=enum_value,
            document_type_confidence=confidence,
            sources_used=["text"] if enum_value is not None else [],
            raw_text_response=response.response_text,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_or_extract_text(self, context: PipelineContext) -> str:
        if context.extracted_text is None:
            try:
                context.extracted_text = self._pdf.extract_text(context.pdf_bytes)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("document_type: extract_text failed: %s", exc)
                context.extracted_text = ""
        return context.extracted_text
