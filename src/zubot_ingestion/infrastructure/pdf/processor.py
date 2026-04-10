"""PyMuPDF-based implementation of IPDFProcessor.

Implements CAP-006 (PDF text extraction) and CAP-007 (PDF page rendering) using
the PyMuPDF (``fitz``) library. The concrete class :class:`PyMuPDFProcessor`
satisfies the :class:`~zubot_ingestion.domain.protocols.IPDFProcessor` Protocol.

This adapter lives in the infrastructure layer and is the only place in the
codebase that is allowed to import ``fitz``.
"""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Any

import fitz  # PyMuPDF

from zubot_ingestion.domain.entities import PDFData, RenderedPage
from zubot_ingestion.shared.types import FileHash

__all__ = [
    "PyMuPDFProcessor",
    "PDFProcessorError",
    "PDFEncryptedError",
    "PDFCorruptedError",
    "PageNotFoundError",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PDFProcessorError(Exception):
    """Base class for all PDF processor errors."""


class PDFEncryptedError(PDFProcessorError):
    """Raised when a PDF is password-protected / encrypted and cannot be read."""


class PDFCorruptedError(PDFProcessorError):
    """Raised when a PDF stream is unreadable, truncated, or not a valid PDF."""


class PageNotFoundError(PDFProcessorError):
    """Raised when a requested page index is out of range for the PDF."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Keys we surface from PyMuPDF's document metadata dict. PyMuPDF returns a
# flat string-typed dict; we pass these through unchanged (empty strings
# become ``None`` to distinguish "missing" from "empty-but-set").
_METADATA_KEYS = (
    "title",
    "author",
    "subject",
    "creator",
    "producer",
    "creationDate",
    "modDate",
    "keywords",
    "format",
)

# JPEG quality for rendered pages. Chosen to match the task spec.
_JPEG_QUALITY = 85

# Page separator token emitted by :meth:`extract_text` between pages. Pages
# are 1-indexed in this output for human readability.
_PAGE_SEPARATOR = "\n\n---PAGE {n}---\n\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_hex(data: bytes) -> str:
    """Return the SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _normalize_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Project a PyMuPDF metadata dict down to our curated key set.

    Empty strings are converted to ``None`` so downstream code can rely on
    ``metadata.get("title")`` returning a truthy value only when one is set.
    Unknown keys are dropped to keep the entity stable across PyMuPDF versions.
    """
    if not raw:
        return {k: None for k in _METADATA_KEYS}
    out: dict[str, Any] = {}
    for key in _METADATA_KEYS:
        value = raw.get(key)
        if value == "" or value is None:
            out[key] = None
        else:
            out[key] = value
    return out


def _open_document(pdf_bytes: bytes) -> fitz.Document:
    """Open a PDF from bytes, raising our domain errors on failure.

    This is the single choke point where PyMuPDF-specific exceptions
    (``fitz.FileDataError``, ``fitz.EmptyFileError``) are translated into
    :class:`PDFCorruptedError` / :class:`PDFEncryptedError`, so the rest of the
    adapter code can treat an opened document as trusted.
    """
    if not pdf_bytes:
        raise PDFCorruptedError("PDF bytes are empty")
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except fitz.FileDataError as exc:  # corrupted / not a PDF
        raise PDFCorruptedError(f"Failed to open PDF: {exc}") from exc
    except fitz.EmptyFileError as exc:  # zero-length
        raise PDFCorruptedError("PDF stream is empty") from exc
    except RuntimeError as exc:  # generic mupdf failure
        raise PDFCorruptedError(f"Failed to open PDF: {exc}") from exc

    # PyMuPDF flags password-protected PDFs via ``needs_pass`` / ``is_encrypted``
    # but still returns the Document object. We refuse to operate on them.
    if doc.needs_pass or doc.is_encrypted:
        try:
            doc.close()
        except Exception:
            pass
        raise PDFEncryptedError("PDF is encrypted or password-protected")

    return doc


def _resolve_page_index(page_number: int, page_count: int) -> int:
    """Resolve a possibly-negative page index to a concrete ``[0, page_count)`` index."""
    if page_count <= 0:
        raise PageNotFoundError("PDF has no pages")
    idx = page_number
    if idx < 0:
        idx = page_count + idx
    if idx < 0 or idx >= page_count:
        raise PageNotFoundError(
            f"Page {page_number} out of range (page_count={page_count})"
        )
    return idx


def _render_single_page(
    doc: fitz.Document,
    page_number: int,
    dpi: int,
    scale: float | None,
) -> RenderedPage:
    """Render a single page from an already-open document.

    Uses ``scale`` to build the zoom matrix if provided, otherwise derives
    zoom from ``dpi`` (PyMuPDF's default base resolution is 72 dpi).
    """
    idx = _resolve_page_index(page_number, doc.page_count)

    if scale is not None:
        zoom = float(scale)
    else:
        zoom = float(dpi) / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    started = time.perf_counter()
    page = doc.load_page(idx)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    jpeg_bytes = pixmap.tobytes(output="jpeg", jpg_quality=_JPEG_QUALITY)
    render_time_ms = int((time.perf_counter() - started) * 1000)

    return RenderedPage(
        page_number=idx,
        jpeg_bytes=jpeg_bytes,
        base64_jpeg=base64.b64encode(jpeg_bytes).decode("ascii"),
        width_px=int(pixmap.width),
        height_px=int(pixmap.height),
        dpi=int(dpi),
        render_time_ms=render_time_ms,
    )


# ---------------------------------------------------------------------------
# PyMuPDFProcessor
# ---------------------------------------------------------------------------


class PyMuPDFProcessor:
    """Concrete :class:`IPDFProcessor` backed by PyMuPDF (``fitz``)."""

    # -- load -------------------------------------------------------------

    def load(self, pdf_bytes: bytes) -> PDFData:
        """Open ``pdf_bytes`` and return :class:`PDFData`.

        Raises
        ------
        PDFEncryptedError
            If the PDF is password-protected.
        PDFCorruptedError
            If the PDF bytes cannot be parsed as a PDF.
        """
        file_hash = FileHash(_sha256_hex(pdf_bytes))
        with _open_document(pdf_bytes) as doc:
            page_count = int(doc.page_count)
            metadata = _normalize_metadata(doc.metadata)
            # Cheap heuristic: does any page have a text layer?
            has_text_layer = False
            for page in doc:
                if page.get_text("text").strip():
                    has_text_layer = True
                    break

        return PDFData(
            page_count=page_count,
            file_hash=file_hash,
            metadata=metadata,
            is_encrypted=False,  # would have raised above
            has_text_layer=has_text_layer,
        )

    # -- extract_text -----------------------------------------------------

    def extract_text(self, pdf_bytes: bytes) -> str:
        """Extract all page text, joined with 1-indexed page separators."""
        parts: list[str] = []
        with _open_document(pdf_bytes) as doc:
            for i, page in enumerate(doc, start=1):
                parts.append(_PAGE_SEPARATOR.format(n=i))
                try:
                    text = page.get_text("text")
                except Exception:
                    text = ""
                parts.append(text or "")
        return "".join(parts)

    # -- extract_page_text ------------------------------------------------

    def extract_page_text(self, pdf_bytes: bytes, page_number: int) -> str:
        """Extract the text layer for a single page.

        ``page_number`` may be negative (e.g. ``-1`` for the last page).
        Returns an empty string if the page has no text layer or if text
        extraction fails. Unlike :meth:`extract_text`, this method returns
        ONLY the content of the requested page with no separator tokens.

        Performance note: opens and parses ``pdf_bytes`` on every call. If
        the caller already knows the full set of pages it needs, prefer
        :meth:`extract_page_texts` which opens the document exactly once.
        """
        with _open_document(pdf_bytes) as doc:
            idx = _resolve_page_index(page_number, doc.page_count)
            try:
                page = doc.load_page(idx)
                return page.get_text("text") or ""
            except Exception:
                return ""

    # -- extract_page_texts -----------------------------------------------

    def extract_page_texts(
        self, pdf_bytes: bytes, page_numbers: list[int]
    ) -> dict[int, str]:
        """Batch-extract the text layer for multiple pages.

        Opens ``pdf_bytes`` exactly once and returns a dict mapping each
        requested page number (preserved as-is, including negative
        indices) to the extracted text for that page. This avoids the
        per-call ``fitz.open(stream=...)`` re-parse cost that
        :meth:`extract_page_text` incurs when called in a loop.

        Pages whose text extraction raises are recorded as empty strings
        rather than propagating the failure, matching the
        per-page method's degrade-gracefully contract. An empty
        ``page_numbers`` list returns an empty dict without opening the
        document.
        """
        if not page_numbers:
            return {}
        results: dict[int, str] = {}
        with _open_document(pdf_bytes) as doc:
            for pn in page_numbers:
                try:
                    idx = _resolve_page_index(pn, doc.page_count)
                    page = doc.load_page(idx)
                    results[pn] = page.get_text("text") or ""
                except Exception:
                    results[pn] = ""
        return results

    # -- render_page ------------------------------------------------------

    def render_page(
        self,
        pdf_bytes: bytes,
        page_number: int,
        dpi: int = 150,
        scale: float | None = None,
    ) -> RenderedPage:
        """Render a single page to a JPEG-encoded :class:`RenderedPage`.

        ``page_number`` may be negative (e.g. ``-1`` for the last page).
        """
        with _open_document(pdf_bytes) as doc:
            return _render_single_page(doc, page_number, dpi, scale)

    # -- render_pages -----------------------------------------------------

    def render_pages(
        self,
        pdf_bytes: bytes,
        page_numbers: list[int],
        dpi: int = 150,
        scale: float | None = None,
    ) -> list[RenderedPage]:
        """Render multiple pages in a single document context.

        The document is opened **once** and reused for every requested page so
        that PyMuPDF can release its internal caches at the end, preventing
        memory leaks that occur if ``render_page`` is called in a loop.

        The result list preserves the order of ``page_numbers``.
        """
        rendered: list[RenderedPage] = []
        with _open_document(pdf_bytes) as doc:
            for pn in page_numbers:
                rendered.append(_render_single_page(doc, pn, dpi, scale))
        return rendered
