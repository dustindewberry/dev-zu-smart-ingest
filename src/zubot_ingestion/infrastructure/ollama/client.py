"""Ollama client metric instrumentation hooks (CAP-028 instrumentation slice).

This file is a MINIMAL STUB owned by the witty-atlas worktree (task-23 /
step-23 prometheus-metrics). The canonical full Ollama HTTP client that
implements IOllamaClient (generate_vision, generate_text,
check_model_available, retry policy) is owned by task-9 (sage-crane /
iron-finch worktrees). On merge, the canonical task-9 client MUST take
precedence over this stub. The ONLY behavioural contribution this file
makes is the metric-recording helpers below — they MUST be ported into
the canonical client at the locations indicated by the docstrings.

Two integration points are required in the canonical client:

1. **Per-call timing -> ollama_duration histogram**

   Wrap the body of each ``_call_ollama_*`` method (or whatever the
   single private call helper is named) with the ``time_ollama_call``
   context manager so the elapsed wall-clock for the request is
   observed in seconds, labelled by model name. The context manager
   captures the start before the HTTP call and observes elapsed AFTER
   the call returns OR raises (using a try/finally), so it records
   timing for both the success and failure paths.

2. **Status counter -> ollama_requests**

   Inside the same try/except wrapper that decides whether the HTTP
   call succeeded or failed, call ``record_ollama_request_status(model,
   status)`` where status is one of {'success', 'error', 'retry'}. The
   helper accepts any string but the canonical taxonomy is:

       - 'success' on a 200 response with a parseable body
       - 'error'   on a non-retryable HTTP status, transport error, or
                   exhausted retry budget
       - 'retry'   on each retried 503/429/transport error attempt
                   (i.e. counted at the retry decision point, not the
                   final outcome)

   The retry counter is intentionally separate from the final outcome
   counter so dashboards can distinguish "1 successful call after 2
   retries" from "1 failed call".

The helpers below are pure functions that take primitive inputs and
update the metric singletons in
``zubot_ingestion.infrastructure.metrics.prometheus``. They have no
runtime dependency on the canonical client's internals beyond the
``model: str`` argument, which means they can be unit-tested in
isolation.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from zubot_ingestion.infrastructure.metrics.prometheus import (
    ollama_duration,
    ollama_requests,
)

__all__ = [
    "record_ollama_request_status",
    "time_ollama_call",
    "STATUS_SUCCESS",
    "STATUS_ERROR",
    "STATUS_RETRY",
]


# Status label values pinned as module-level constants so the canonical
# Ollama client and the unit tests share a single source of truth.
STATUS_SUCCESS: str = "success"
STATUS_ERROR: str = "error"
STATUS_RETRY: str = "retry"


def record_ollama_request_status(model: str, status: str) -> None:
    """Increment ``ollama_requests{model=..., status=...}`` exactly once.

    Args:
        model: The Ollama model identifier (e.g. ``'qwen2.5vl:7b'``).
        status: One of {'success', 'error', 'retry'}. The helper does
            not enforce the taxonomy at runtime — callers are
            responsible for using the canonical labels — but the
            module-level ``STATUS_*`` constants exist as the single
            source of truth.
    """
    ollama_requests.labels(model=model, status=status).inc()


@contextmanager
def time_ollama_call(model: str) -> Iterator[None]:
    """Time-and-observe an Ollama HTTP call into ``ollama_duration{model=...}``.

    The context manager captures the start time before yielding and
    observes the elapsed seconds after the body completes — even if
    the body raises. This guarantees that timing is recorded for both
    success and failure paths.

    Example::

        with time_ollama_call(model='qwen2.5vl:7b'):
            response = httpx.post(url, json=payload)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = max(0.0, time.perf_counter() - start)
        ollama_duration.labels(model=model).observe(elapsed)
