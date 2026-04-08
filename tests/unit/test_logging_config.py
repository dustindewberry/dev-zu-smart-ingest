"""Unit tests for structured-logging configuration (CAP-029).

Covers:
- ``setup_logging`` actually creates rotated handlers and the log directory
- ``setup_logging`` is idempotent (re-invocation does not duplicate handlers)
- ``get_logger`` returns a structlog BoundLogger
- contextvars binding helpers (request, job, clear)
- the sensitive-value scrubber redacts every documented key fragment, in
  top-level keys and in nested dicts/lists, and leaves benign keys alone.
"""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest
import structlog

from zubot_ingestion.infrastructure.logging.config import (
    _is_sensitive_key,
    _scrub_value,
    bind_job_context,
    bind_request_context,
    clear_context,
    get_logger,
    scrub_sensitive_processor,
    setup_logging,
)
from zubot_ingestion.shared.constants import (
    REDACTION_PLACEHOLDER,
    SENSITIVE_KEY_FRAGMENTS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    """Make sure each test starts/ends with a clean root logger + contextvars."""
    clear_context()
    root = logging.getLogger()
    saved = list(root.handlers)
    for h in saved:
        root.removeHandler(h)
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved:
        root.addHandler(h)
    clear_context()


# ---------------------------------------------------------------------------
# Sensitive scrubber — REQUIRED TEST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sensitive_key",
    [
        "api_key",
        "API_KEY",
        "user_api_key",
        "x_api_key",
        "apikey",
        "jwt",
        "JWT",
        "access_jwt",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "user_password",
        "secret",
        "client_secret",
        "file_bytes",
        "file_contents",
        "authorization",
    ],
)
def test_scrub_sensitive_processor_redacts_top_level_keys(sensitive_key: str) -> None:
    """Every documented sensitive key must be replaced with the placeholder."""
    event_dict = {
        sensitive_key: "supersecret-value-do-not-leak",
        "event": "request.start",
        "user_id": "u-123",
    }

    out = scrub_sensitive_processor(None, "info", dict(event_dict))

    assert out[sensitive_key] == REDACTION_PLACEHOLDER, (
        f"sensitive key {sensitive_key!r} was not redacted"
    )
    # Benign fields are untouched.
    assert out["event"] == "request.start"
    assert out["user_id"] == "u-123"


def test_scrub_sensitive_processor_redacts_nested_dict_keys() -> None:
    """Nested dicts must also have their sensitive keys redacted."""
    event_dict = {
        "event": "request.start",
        "headers": {
            "authorization": "Bearer eyJhbGciOi...",
            "x-api-key": "ak_live_abc123",
            "user-agent": "curl/8.4.0",
        },
        "payload": {
            "username": "alice",
            "password": "hunter2",
            "nested": {
                "jwt": "another-token",
                "ok": "value",
            },
        },
    }

    out = scrub_sensitive_processor(None, "info", dict(event_dict))

    assert out["headers"]["authorization"] == REDACTION_PLACEHOLDER
    assert out["headers"]["x-api-key"] == REDACTION_PLACEHOLDER
    assert out["headers"]["user-agent"] == "curl/8.4.0"
    assert out["payload"]["username"] == "alice"
    assert out["payload"]["password"] == REDACTION_PLACEHOLDER
    assert out["payload"]["nested"]["jwt"] == REDACTION_PLACEHOLDER
    assert out["payload"]["nested"]["ok"] == "value"


def test_scrub_sensitive_processor_redacts_inside_lists() -> None:
    """Sensitive keys inside dicts inside lists must still be redacted."""
    event_dict = {
        "event": "batch.submit",
        "items": [
            {"file_bytes": b"raw-pdf-bytes", "filename": "a.pdf"},
            {"file_contents": "plain-text-leak", "filename": "b.pdf"},
        ],
    }

    out = scrub_sensitive_processor(None, "info", dict(event_dict))

    assert out["items"][0]["file_bytes"] == REDACTION_PLACEHOLDER
    assert out["items"][0]["filename"] == "a.pdf"
    assert out["items"][1]["file_contents"] == REDACTION_PLACEHOLDER
    assert out["items"][1]["filename"] == "b.pdf"


def test_scrub_sensitive_processor_leaves_unrelated_strings_alone() -> None:
    """Free-text values (even if they LOOK secret) must NOT be touched.

    The scrubber operates on KEYS, not VALUES, so callers retain control.
    """
    event_dict = {
        "event": "user mentioned 'password' in their message",
        "message": "Please reset my password",
    }

    out = scrub_sensitive_processor(None, "info", dict(event_dict))

    assert out["event"] == "user mentioned 'password' in their message"
    assert out["message"] == "Please reset my password"


def test_is_sensitive_key_matches_all_documented_fragments() -> None:
    """Every fragment in SENSITIVE_KEY_FRAGMENTS should match itself."""
    for fragment in SENSITIVE_KEY_FRAGMENTS:
        assert _is_sensitive_key(fragment), f"{fragment!r} did not self-match"
        assert _is_sensitive_key(fragment.upper())
        assert _is_sensitive_key(f"prefix_{fragment}_suffix")


def test_is_sensitive_key_rejects_non_string_keys() -> None:
    """Non-string keys (e.g. ints) must not crash the scrubber."""
    assert _is_sensitive_key(123) is False  # type: ignore[arg-type]
    assert _is_sensitive_key(None) is False  # type: ignore[arg-type]


def test_scrub_value_handles_tuples() -> None:
    """Tuples should be returned as tuples, not converted to lists."""
    out = _scrub_value(({"token": "x"}, {"ok": "y"}))
    assert isinstance(out, tuple)
    assert out[0]["token"] == REDACTION_PLACEHOLDER
    assert out[1]["ok"] == "y"


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


def test_setup_logging_creates_log_dir(tmp_path) -> None:
    """``setup_logging`` must mkdir -p the log directory."""
    log_dir = tmp_path / "nested" / "logs"
    assert not log_dir.exists()

    setup_logging(log_level="INFO", log_dir=str(log_dir))

    assert log_dir.is_dir()


def test_setup_logging_attaches_three_handlers_when_log_dir_set(tmp_path) -> None:
    """Console + app file + error file = 3 root handlers."""
    setup_logging(log_level="DEBUG", log_dir=str(tmp_path))

    handlers = logging.getLogger().handlers
    assert len(handlers) == 3

    levels = sorted(h.level for h in handlers)
    # DEBUG (10), DEBUG (10), ERROR (40)
    assert levels == [logging.DEBUG, logging.DEBUG, logging.ERROR]


def test_setup_logging_console_only_when_no_log_dir() -> None:
    """Without a log_dir we should still get exactly one console handler."""
    setup_logging(log_level="INFO", log_dir=None)

    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)


def test_setup_logging_is_idempotent(tmp_path) -> None:
    """Calling setup_logging twice must not duplicate handlers."""
    setup_logging(log_level="INFO", log_dir=str(tmp_path))
    setup_logging(log_level="INFO", log_dir=str(tmp_path))

    assert len(logging.getLogger().handlers) == 3


def test_setup_logging_respects_log_level_string() -> None:
    """A string level like 'WARNING' must be honoured."""
    setup_logging(log_level="WARNING", log_dir=None)

    assert logging.getLogger().level == logging.WARNING


# ---------------------------------------------------------------------------
# get_logger and contextvars
# ---------------------------------------------------------------------------


def test_get_logger_returns_structlog_logger() -> None:
    setup_logging(log_level="INFO", log_dir=None)
    logger = get_logger("test.module")
    # structlog returns a BoundLoggerLazyProxy until first use, but it must
    # quack like a logger:
    assert hasattr(logger, "info")
    assert hasattr(logger, "error")
    assert hasattr(logger, "exception")


def test_bind_request_context_propagates_to_log_lines() -> None:
    """Bound contextvars must show up in subsequent log lines as JSON keys."""
    # Configure structlog to render to a list, bypassing stdlib handlers.
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            scrub_sensitive_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=StringIO()),
    )

    buf = StringIO()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            scrub_sensitive_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
    )

    bind_request_context(
        request_id="req-abc",
        user_id="u-1",
        method="POST",
        path="/extract",
    )
    structlog.get_logger("test").info("hello")

    line = buf.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)

    assert payload["request_id"] == "req-abc"
    assert payload["user_id"] == "u-1"
    assert payload["method"] == "POST"
    assert payload["path"] == "/extract"
    assert payload["event"] == "hello"


def test_bind_job_context_then_clear_removes_all_keys() -> None:
    structlog.reset_defaults()
    buf = StringIO()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            scrub_sensitive_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
    )

    bind_job_context(
        job_id="job-1",
        batch_id="batch-1",
        file_hash="abc123",
    )
    structlog.get_logger("test").info("during")

    clear_context()
    structlog.get_logger("test").info("after")

    lines = [json.loads(l) for l in buf.getvalue().strip().splitlines()]
    during = lines[-2]
    after = lines[-1]

    assert during["job_id"] == "job-1"
    assert during["batch_id"] == "batch-1"
    assert during["file_hash"] == "abc123"

    assert "job_id" not in after
    assert "batch_id" not in after
    assert "file_hash" not in after


def test_end_to_end_scrubbing_through_real_log_call() -> None:
    """Smoke test: a real .info() with a token kwarg must produce JSON
    where the token is redacted but other fields survive."""
    structlog.reset_defaults()
    buf = StringIO()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            scrub_sensitive_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
    )

    structlog.get_logger("test").info(
        "auth.attempt",
        user_id="u-1",
        api_key="ak_live_DO_NOT_LEAK",
        jwt="eyJhbGciOiJIUzI1NiJ9.payload.sig",
    )

    line = buf.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)

    assert payload["event"] == "auth.attempt"
    assert payload["user_id"] == "u-1"
    assert payload["api_key"] == REDACTION_PLACEHOLDER
    assert payload["jwt"] == REDACTION_PLACEHOLDER
    # Make absolutely sure the secret never appears anywhere in the rendered line.
    assert "ak_live_DO_NOT_LEAK" not in line
    assert "eyJhbGciOiJIUzI1NiJ9" not in line
