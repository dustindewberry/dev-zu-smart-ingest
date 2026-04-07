"""Versioned prompt templates for the Ollama extraction pipeline.

Each constant in this module is a frozen, versioned prompt string that
the extraction pipeline sends to the Ollama vision or text model. The
``_V1`` suffix is part of the public name so future revisions can be
added side-by-side (``..._V2``) without breaking call sites that pin a
specific version.

Conventions
-----------
* Every prompt instructs the model to return strict JSON only — no
  markdown fences, no commentary, no leading/trailing prose.
* Every prompt explicitly states a ``temperature=0`` expectation so
  reviewers can verify the call site is using deterministic sampling.
* The exact JSON schema the model is expected to emit is documented
  inline so the response parser (IResponseParser) can validate it.

These templates are imported by Stage 1 extractors and the Stage 2
companion generator. They have no runtime dependencies on other
zubot_ingestion modules.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Stage 1 — drawing number
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Stage 1 — title
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


# ---------------------------------------------------------------------------
# Stage 1 — document type classification
# ---------------------------------------------------------------------------

DOCUMENT_TYPE_TEXT_PROMPT_V1: str = """\
You are an expert at classifying construction project documents.

Below is the extracted text content of a PDF. Classify the document into \
EXACTLY ONE of the following 21 controlled-vocabulary types:

- DRAWING
- SPECIFICATION
- SCHEDULE
- REPORT
- RFI
- SUBMITTAL
- CHANGE_ORDER
- TRANSMITTAL
- MEETING_MINUTES
- CORRESPONDENCE
- INVOICE
- PURCHASE_ORDER
- CONTRACT
- PERMIT
- INSPECTION_REPORT
- TEST_REPORT
- WARRANTY
- MANUAL
- PHOTO
- SAFETY_DOCUMENT
- OTHER

Return STRICT JSON only — no markdown, no commentary, no code fences. \
Use this exact schema:

{
  "document_type": "<one of the 21 enum values above>",
  "confidence": <float between 0.0 and 1.0>,
  "reasoning": "<one short sentence>"
}

Rules:
- The document_type field MUST be one of the 21 values listed above, \
verbatim and uppercase. If you are unsure, return "OTHER".
- Do not invent new types. Do not include any text outside the JSON object.
- This call is made with temperature=0; respond deterministically.
"""


# ---------------------------------------------------------------------------
# Stage 2 — companion description
# ---------------------------------------------------------------------------

COMPANION_DESCRIPTION_PROMPT_V1: str = """\
You are an expert at describing construction drawings for downstream \
search and retrieval.

Look at this image of one page from a construction document. Produce a \
structured visual description that another system can index for full-text \
search.

Return STRICT JSON only — no markdown, no commentary, no code fences. \
Use this exact schema:

{
  "visual_description": "<2-4 sentence factual description of what is depicted>",
  "technical_details": "<key technical features, dimensions, callouts, or symbols>",
  "page_metadata": {
    "drawing_number_visible": "<string or null>",
    "title_visible": "<string or null>",
    "scale_visible": "<string or null>"
  }
}

Rules:
- Be factual and concise. Do not speculate about content not visible.
- If a field is not visible on this page, use null (for strings) or an \
empty string for the description fields.
- Do not invent values. Do not include any text outside the JSON object.
- This call is made with temperature=0; respond deterministically.
"""


__all__ = [
    "DRAWING_NUMBER_VISION_PROMPT_V1",
    "DRAWING_NUMBER_TEXT_PROMPT_V1",
    "TITLE_VISION_PROMPT_V1",
    "TITLE_TEXT_PROMPT_V1",
    "DOCUMENT_TYPE_TEXT_PROMPT_V1",
    "COMPANION_DESCRIPTION_PROMPT_V1",
]
