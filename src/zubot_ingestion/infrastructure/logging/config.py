"""Structured JSON logging configuration (CAP-029).

Provides:
- ``setup_logging``: configures stdlib + structlog with daily-rotated JSON files.
- ``get_logger``: thin wrapper around ``structlog.get_logger``.
- ``bind_request_context`` / ``bind_job_context`` / ``clear_context``: helpers
  that bind contextvars so every log line emitted on the same task / request
  inherits the same correlation IDs.
- ``scrub_sensitive_processor``: a structlog processor that recursively
  redacts any value whose key matches a sensitive fragment (api_key, jwt,
  token, password, secret, file_bytes, file_contents, ...).

This module is the only place that should call ``structlog.configure``.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.types import EventDict, Processor, WrappedLogger

from zubot_ingestion.shared.constants import (
    REDACTION_PLACEHOLDER,
    SENSITIVE_KEY_FRAGMENTS,
)


# ---------------------------------------------------------------------------
# Sensitive value scrubber
# ---------------------------------------------------------------------------


def _is_sensitive_key(key: str) -> bool:
    """Return True if ``key`` (case-insensitive) contains a sensitive fragment.

    Hyphens, dots, and spaces are normalised to underscores before matching
    so that ``X-API-Key``, ``x_api_key``, and ``api.key`` all hit the
    ``api_key`` fragment.
    """
    if not isinstance(key, str):
        return False
    normalised = key.lower().replace("-", "_").replace(".", "_").replace(" ", "_")
    return any(fragment in normalised for fragment in SENSITIVE_KEY_FRAGMENTS)


def _scrub_value(value: Any) -> Any:
    """Recursively scrub a value, redacting any nested sensitive keys."""
    if isinstance(value, dict):
        return {
            k: (REDACTION_PLACEHOLDER if _is_sensitive_key(k) else _scrub_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        scrubbed = [_scrub_value(item) for item in value]
        return type(value)(scrubbed) if isinstance(value, tuple) else scrubbed
    return value


def scrub_sensitive_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Structlog processor that redacts sensitive keys from the event dict.

    Operates in-place on the top-level keys and recursively on nested
    dict/list/tuple values. Strings are left untouched (we cannot reliably
    pattern-match secrets inside free text without false positives — callers
    must put secrets in a key that the scrubber recognises).
    """
    for key in list(event_dict.keys()):
        if _is_sensitive_key(key):
            event_dict[key] = REDACTION_PLACEHOLDER
        else:
            event_dict[key] = _scrub_value(event_dict[key])
    return event_dict


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def _build_processors() -> list[Processor]:
    """Build the canonical structlog processor chain (CAP-029)."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        scrub_sensitive_processor,
        structlog.processors.JSONRenderer(),
    ]


def setup_logging(log_level: str = "INFO", log_dir: str | None = None) -> None:
    """Configure stdlib logging + structlog for structured JSON output.

    - Console handler at ``log_level``
    - If ``log_dir`` is given:
        * ``{log_dir}/app.log`` — rotated at midnight, 7 backups, all levels
        * ``{log_dir}/error.log`` — rotated at midnight, 7 backups, ERROR+
    - structlog is configured with the canonical processor chain that
      includes the sensitive-value scrubber.

    This function is idempotent: calling it twice replaces all root handlers,
    so it is safe to invoke from FastAPI's ``lifespan`` and from Celery's
    ``worker_process_init`` signal in the same process.
    """
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Reset root handlers so re-invocation is idempotent.
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(numeric_level)

    # All handlers emit raw JSON strings, since structlog has already
    # rendered the event dict via JSONRenderer.
    plain_formatter = logging.Formatter("%(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(plain_formatter)
    root.addHandler(console_handler)

    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

        app_handler = TimedRotatingFileHandler(
            filename=os.path.join(log_dir, "app.log"),
            when="midnight",
            backupCount=7,
            encoding="utf-8",
        )
        app_handler.setLevel(numeric_level)
        app_handler.setFormatter(plain_formatter)
        root.addHandler(app_handler)

        error_handler = TimedRotatingFileHandler(
            filename=os.path.join(log_dir, "error.log"),
            when="midnight",
            backupCount=7,
            encoding="utf-8",
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(plain_formatter)
        root.addHandler(error_handler)

    structlog.configure(
        processors=_build_processors(),
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog BoundLogger named ``name``."""
    return structlog.get_logger(name)


# ---------------------------------------------------------------------------
# Context binding helpers (contextvars-based)
# ---------------------------------------------------------------------------


def bind_request_context(
    request_id: str,
    user_id: str | None = None,
    method: str | None = None,
    path: str | None = None,
) -> None:
    """Bind HTTP-request correlation IDs into contextvars.

    Every subsequent log line emitted on the same async task will inherit
    these fields via ``structlog.contextvars.merge_contextvars``.
    """
    bind_contextvars(
        request_id=request_id,
        user_id=user_id,
        method=method,
        path=path,
    )


def bind_job_context(
    job_id: str,
    batch_id: str | None = None,
    file_hash: str | None = None,
) -> None:
    """Bind extraction-job correlation IDs into contextvars."""
    bind_contextvars(
        job_id=job_id,
        batch_id=batch_id,
        file_hash=file_hash,
    )


def clear_context() -> None:
    """Drop all bound contextvars (call at the end of a request/task)."""
    clear_contextvars()
