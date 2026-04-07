"""Shared test helpers for the Stage 1 extractor tests.

* :class:`MockOllamaClient` — a programmable :class:`IOllamaClient` stub.
* :func:`make_pdf_bytes` — a synthetic PDF builder using PyMuPDF.
* :func:`make_pipeline_context` — convenience to build a
  :class:`PipelineContext` from synthetic bytes + filename.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import fitz

from zubot_ingestion.domain.entities import (
    Job,
    OllamaResponse,
    PipelineContext,
)
from zubot_ingestion.domain.enums import JobStatus
from zubot_ingestion.shared.types import BatchId, FileHash, JobId


# ---------------------------------------------------------------------------
# Synthetic PDF builder
# ---------------------------------------------------------------------------


def make_pdf_bytes(
    pages: list[str] | None = None,
    *,
    title: str = "Synthetic Title",
) -> bytes:
    """Build a simple multi-page PDF with the given page bodies."""
    pages = pages or ["page one body", "page two body"]
    doc = fitz.open()
    for body in pages:
        page = doc.new_page()
        if body:
            page.insert_text((72, 72), body, fontsize=12)
    doc.set_metadata({"title": title, "format": "PDF 1.7"})
    out = doc.tobytes()
    doc.close()
    return out


def make_job(filename: str = "170154-L-001.pdf") -> Job:
    """Construct a frozen :class:`Job` with sane defaults for tests."""
    now = datetime.now(UTC)
    return Job(
        job_id=JobId(uuid4()),
        batch_id=BatchId(uuid4()),
        filename=filename,
        file_hash=FileHash("a" * 64),
        file_path=f"/tmp/{filename}",
        status=JobStatus.QUEUED,
        result=None,
        error_message=None,
        pipeline_trace=None,
        otel_trace_id=None,
        processing_time_ms=None,
        created_at=now,
        updated_at=now,
    )


def make_pipeline_context(
    *,
    pdf_bytes: bytes | None = None,
    filename: str = "170154-L-001.pdf",
) -> PipelineContext:
    """Build a fresh :class:`PipelineContext` for an extractor under test."""
    return PipelineContext(
        job=make_job(filename),
        pdf_bytes=pdf_bytes if pdf_bytes is not None else make_pdf_bytes(),
    )


# ---------------------------------------------------------------------------
# Programmable mock Ollama client
# ---------------------------------------------------------------------------


@dataclass
class _OllamaCall:
    """A recorded call to the mock Ollama client."""

    method: str  # "vision" or "text"
    model: str
    prompt: str
    payload: str  # image_base64 (vision) or text content (text)
    temperature: float


@dataclass
class MockOllamaClient:
    """An ``IOllamaClient``-shaped test double.

    Programs the responses by setting :attr:`vision_responses` /
    :attr:`text_responses` to a list of strings — each call pops the next
    string off the front of the list. If the list is empty, raises the
    exception in :attr:`raise_on_vision` / :attr:`raise_on_text` (or, by
    default, returns an empty response).

    Recorded calls are accessible via :attr:`calls` for assertions.
    """

    vision_responses: list[str] = field(default_factory=list)
    text_responses: list[str] = field(default_factory=list)
    raise_on_vision: BaseException | None = None
    raise_on_text: BaseException | None = None
    available_models: tuple[str, ...] = ("qwen2.5vl:7b", "qwen2.5:7b")
    calls: list[_OllamaCall] = field(default_factory=list)

    async def generate_vision(
        self,
        image_base64: str,
        prompt: str,
        model: str = "qwen2.5vl:7b",
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
    ) -> OllamaResponse:
        self.calls.append(
            _OllamaCall(
                method="vision",
                model=model,
                prompt=prompt,
                payload=image_base64[:32],
                temperature=temperature,
            )
        )
        if self.raise_on_vision is not None:
            raise self.raise_on_vision
        if self.vision_responses:
            text = self.vision_responses.pop(0)
        else:
            text = ""
        return OllamaResponse(
            response_text=text,
            model=model,
            prompt_eval_count=None,
            eval_count=None,
            total_duration_ns=None,
            raw={},
        )

    async def generate_text(
        self,
        text: str,
        prompt: str,
        model: str = "qwen2.5:7b",
        temperature: float = 0.0,
        timeout_seconds: float = 30.0,
    ) -> OllamaResponse:
        self.calls.append(
            _OllamaCall(
                method="text",
                model=model,
                prompt=prompt,
                payload=text[:32],
                temperature=temperature,
            )
        )
        if self.raise_on_text is not None:
            raise self.raise_on_text
        if self.text_responses:
            response_text = self.text_responses.pop(0)
        else:
            response_text = ""
        return OllamaResponse(
            response_text=response_text,
            model=model,
            prompt_eval_count=None,
            eval_count=None,
            total_duration_ns=None,
            raw={},
        )

    async def check_model_available(self, model: str) -> bool:
        return model in self.available_models


# ---------------------------------------------------------------------------
# Helper to count calls of a given method
# ---------------------------------------------------------------------------


def count_calls(client: MockOllamaClient, method: str) -> int:
    return sum(1 for c in client.calls if c.method == method)


def calls_for(client: MockOllamaClient, method: str) -> list[_OllamaCall]:
    return [c for c in client.calls if c.method == method]


__all__ = [
    "MockOllamaClient",
    "make_job",
    "make_pdf_bytes",
    "make_pipeline_context",
    "count_calls",
    "calls_for",
]
