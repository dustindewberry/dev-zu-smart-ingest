"""Unit tests for :mod:`zubot_ingestion.infrastructure.pdf.processor`.

All test PDFs are synthesized at runtime via PyMuPDF so the tests run without
any external fixtures.
"""

from __future__ import annotations

import base64
import hashlib

import fitz
import pytest

from zubot_ingestion.domain.entities import PDFData, RenderedPage
from zubot_ingestion.domain.protocols import IPDFProcessor
from zubot_ingestion.infrastructure.pdf.processor import (
    PDFCorruptedError,
    PDFEncryptedError,
    PageNotFoundError,
    PyMuPDFProcessor,
)


# ---------------------------------------------------------------------------
# Synthetic PDF fixtures
# ---------------------------------------------------------------------------


def _make_pdf(
    pages: list[str],
    *,
    title: str = "Synthetic Title",
    author: str = "PyTest Author",
    subject: str = "Synthetic Subject",
    creator: str = "zubot-ingestion tests",
    producer: str = "PyMuPDF",
    encrypt: bool = False,
) -> bytes:
    """Return a PDF byte string with the given page texts and metadata."""
    doc = fitz.open()
    for body in pages:
        page = doc.new_page()
        # Place text at (72, 72); empty strings still produce a valid blank page.
        if body:
            page.insert_text((72, 72), body, fontsize=12)
    doc.set_metadata(
        {
            "title": title,
            "author": author,
            "subject": subject,
            "creator": creator,
            "producer": producer,
            "keywords": "",
            "creationDate": "",
            "modDate": "",
            "trapped": "",
            "format": "PDF 1.7",
            "encryption": None,
        }
    )
    if encrypt:
        out = doc.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="owner-secret",
            user_pw="user-secret",
            permissions=fitz.PDF_PERM_ACCESSIBILITY,
        )
    else:
        out = doc.tobytes()
    doc.close()
    return out


@pytest.fixture(scope="module")
def sample_pdf_bytes() -> bytes:
    return _make_pdf(
        pages=[
            "Hello from page one.",
            "Page two has different content.",
            "Final page with closing remarks.",
        ]
    )


@pytest.fixture(scope="module")
def single_page_pdf_bytes() -> bytes:
    return _make_pdf(pages=["only one page here"])


@pytest.fixture(scope="module")
def empty_page_pdf_bytes() -> bytes:
    # One populated page, one deliberately blank page.
    return _make_pdf(pages=["content", ""])


@pytest.fixture(scope="module")
def encrypted_pdf_bytes() -> bytes:
    return _make_pdf(pages=["secret"], encrypt=True)


@pytest.fixture
def processor() -> PyMuPDFProcessor:
    return PyMuPDFProcessor()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_processor_satisfies_protocol(processor: PyMuPDFProcessor) -> None:
    assert isinstance(processor, IPDFProcessor)


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_returns_pdf_data_with_hash_and_metadata(
    processor: PyMuPDFProcessor, sample_pdf_bytes: bytes
) -> None:
    data = processor.load(sample_pdf_bytes)

    assert isinstance(data, PDFData)
    assert data.page_count == 3
    assert data.is_encrypted is False
    assert data.has_text_layer is True
    # SHA-256 of the raw bytes must match hashlib exactly.
    assert data.file_hash == hashlib.sha256(sample_pdf_bytes).hexdigest()
    assert len(data.file_hash) == 64

    # Curated metadata keys are always present.
    for key in ("title", "author", "subject", "creator", "producer", "creationDate", "modDate"):
        assert key in data.metadata
    assert data.metadata["title"] == "Synthetic Title"
    assert data.metadata["author"] == "PyTest Author"
    assert data.metadata["subject"] == "Synthetic Subject"
    assert data.metadata["creator"] == "zubot-ingestion tests"
    assert data.metadata["producer"] == "PyMuPDF"


def test_load_normalizes_empty_metadata_strings_to_none(
    processor: PyMuPDFProcessor,
) -> None:
    pdf = _make_pdf(pages=["x"], title="", author="", subject="", creator="", producer="")
    data = processor.load(pdf)
    # Empty strings must be normalized to None.
    assert data.metadata["title"] is None
    assert data.metadata["author"] is None
    assert data.metadata["subject"] is None
    assert data.metadata["creator"] is None
    assert data.metadata["producer"] is None


def test_load_detects_no_text_layer(processor: PyMuPDFProcessor) -> None:
    # A page with no inserted text has no text layer.
    pdf = _make_pdf(pages=[""])
    data = processor.load(pdf)
    assert data.has_text_layer is False


def test_load_raises_on_corrupted_pdf(processor: PyMuPDFProcessor) -> None:
    with pytest.raises(PDFCorruptedError):
        processor.load(b"this is definitely not a pdf")


def test_load_raises_on_empty_bytes(processor: PyMuPDFProcessor) -> None:
    with pytest.raises(PDFCorruptedError):
        processor.load(b"")


def test_load_raises_on_encrypted_pdf(
    processor: PyMuPDFProcessor, encrypted_pdf_bytes: bytes
) -> None:
    with pytest.raises(PDFEncryptedError):
        processor.load(encrypted_pdf_bytes)


# ---------------------------------------------------------------------------
# extract_text()
# ---------------------------------------------------------------------------


def test_extract_text_includes_all_pages_with_1_indexed_separators(
    processor: PyMuPDFProcessor, sample_pdf_bytes: bytes
) -> None:
    text = processor.extract_text(sample_pdf_bytes)

    assert "Hello from page one." in text
    assert "Page two has different content." in text
    assert "Final page with closing remarks." in text
    # Separators are 1-indexed.
    assert "---PAGE 1---" in text
    assert "---PAGE 2---" in text
    assert "---PAGE 3---" in text
    # And there should not be a PAGE 0 or PAGE 4 in a 3-page doc.
    assert "---PAGE 0---" not in text
    assert "---PAGE 4---" not in text
    # Page 1 must appear before page 2 before page 3.
    assert text.index("---PAGE 1---") < text.index("---PAGE 2---") < text.index("---PAGE 3---")


def test_extract_text_handles_empty_pages_with_separator(
    processor: PyMuPDFProcessor, empty_page_pdf_bytes: bytes
) -> None:
    text = processor.extract_text(empty_page_pdf_bytes)
    # Both separators must still be emitted even though page 2 is blank.
    assert "---PAGE 1---" in text
    assert "---PAGE 2---" in text


def test_extract_text_raises_on_corrupted_pdf(processor: PyMuPDFProcessor) -> None:
    with pytest.raises(PDFCorruptedError):
        processor.extract_text(b"\x00\x01\x02 not a pdf")


# ---------------------------------------------------------------------------
# render_page()
# ---------------------------------------------------------------------------


def _assert_jpeg(data: bytes) -> None:
    # JPEG SOI marker
    assert data[:2] == b"\xff\xd8", "expected JPEG SOI marker"
    # JPEG EOI marker
    assert data[-2:] == b"\xff\xd9", "expected JPEG EOI marker"


def test_render_page_returns_jpeg_rendered_page(
    processor: PyMuPDFProcessor, sample_pdf_bytes: bytes
) -> None:
    rendered = processor.render_page(sample_pdf_bytes, page_number=0, dpi=150)

    assert isinstance(rendered, RenderedPage)
    assert rendered.page_number == 0
    assert rendered.dpi == 150
    assert rendered.width_px > 0
    assert rendered.height_px > 0
    _assert_jpeg(rendered.jpeg_bytes)

    # base64 should round-trip back to the same bytes.
    assert base64.b64decode(rendered.base64_jpeg) == rendered.jpeg_bytes


def test_render_page_negative_index_returns_last_page(
    processor: PyMuPDFProcessor, sample_pdf_bytes: bytes
) -> None:
    rendered = processor.render_page(sample_pdf_bytes, page_number=-1)
    assert rendered.page_number == 2  # 3-page doc, -1 -> index 2


def test_render_page_out_of_range_raises(
    processor: PyMuPDFProcessor, sample_pdf_bytes: bytes
) -> None:
    with pytest.raises(PageNotFoundError):
        processor.render_page(sample_pdf_bytes, page_number=99)
    with pytest.raises(PageNotFoundError):
        processor.render_page(sample_pdf_bytes, page_number=-99)


def test_render_page_dpi_scales_pixmap_size(
    processor: PyMuPDFProcessor, single_page_pdf_bytes: bytes
) -> None:
    low = processor.render_page(single_page_pdf_bytes, page_number=0, dpi=72)
    high = processor.render_page(single_page_pdf_bytes, page_number=0, dpi=144)
    # Doubling DPI should roughly double pixel dimensions.
    assert high.width_px >= low.width_px * 2 - 2
    assert high.height_px >= low.height_px * 2 - 2


def test_render_page_respects_explicit_scale(
    processor: PyMuPDFProcessor, single_page_pdf_bytes: bytes
) -> None:
    base = processor.render_page(
        single_page_pdf_bytes, page_number=0, dpi=72, scale=1.0
    )
    doubled = processor.render_page(
        single_page_pdf_bytes, page_number=0, dpi=72, scale=2.0
    )
    assert doubled.width_px >= base.width_px * 2 - 2
    assert doubled.height_px >= base.height_px * 2 - 2


# ---------------------------------------------------------------------------
# render_pages()
# ---------------------------------------------------------------------------


def test_render_pages_preserves_input_order(
    processor: PyMuPDFProcessor, sample_pdf_bytes: bytes
) -> None:
    rendered = processor.render_pages(sample_pdf_bytes, page_numbers=[2, 0, 1])
    assert [r.page_number for r in rendered] == [2, 0, 1]
    assert all(isinstance(r, RenderedPage) for r in rendered)
    for r in rendered:
        _assert_jpeg(r.jpeg_bytes)


def test_render_pages_resolves_negative_indices(
    processor: PyMuPDFProcessor, sample_pdf_bytes: bytes
) -> None:
    rendered = processor.render_pages(sample_pdf_bytes, page_numbers=[-1, -2, 0])
    assert [r.page_number for r in rendered] == [2, 1, 0]


def test_render_pages_empty_list_returns_empty(
    processor: PyMuPDFProcessor, sample_pdf_bytes: bytes
) -> None:
    assert processor.render_pages(sample_pdf_bytes, page_numbers=[]) == []


def test_render_pages_out_of_range_raises(
    processor: PyMuPDFProcessor, sample_pdf_bytes: bytes
) -> None:
    with pytest.raises(PageNotFoundError):
        processor.render_pages(sample_pdf_bytes, page_numbers=[0, 42])


def test_render_pages_raises_on_corrupted_pdf(processor: PyMuPDFProcessor) -> None:
    with pytest.raises(PDFCorruptedError):
        processor.render_pages(b"garbage", page_numbers=[0])
