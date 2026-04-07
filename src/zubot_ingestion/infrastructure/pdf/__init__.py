"""PDF processing adapter (PyMuPDF)."""

from zubot_ingestion.infrastructure.pdf.processor import (
    PDFCorruptedError,
    PDFEncryptedError,
    PDFProcessorError,
    PageNotFoundError,
    PyMuPDFProcessor,
)

__all__ = [
    "PyMuPDFProcessor",
    "PDFProcessorError",
    "PDFEncryptedError",
    "PDFCorruptedError",
    "PageNotFoundError",
]
