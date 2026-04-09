"""Webhook callback client — concrete implementation of ``ICallbackClient``.

This module is the only code in the service that delivers
job-completion webhooks to external client-supplied callback URLs.
Callers depend on the :class:`ICallbackClient` protocol from
``zubot_ingestion.domain.protocols`` and receive a
:class:`CallbackHttpClient` instance (or a :class:`NoOpCallbackClient`
when ``CALLBACK_ENABLED`` is ``False``) via dependency injection from
the composition root.

Transport
---------
All requests go to the caller-supplied ``callback_url`` via
``httpx.AsyncClient`` as a JSON POST. A bounded exponential-backoff
retry loop (``base=0.5s``, ``factor=2``, ``max_retries=3``) retries on
5xx responses and network errors. 4xx responses are treated as
non-retryable failures (the client presumably rejected the payload
shape). The adapter never raises — a final failure logs a warning and
returns ``False`` so callers can continue processing.

Signing
-------
If a ``signing_secret`` is configured, every request body is signed
with HMAC-SHA256 and the hex digest is sent as the
``X-Zubot-Signature`` header. Receivers are expected to recompute the
digest with the same shared secret and compare it to the header in
constant time. When the signing secret is ``None`` or an empty string
the header is omitted.

Implements CAP-025. Protocol contract: ICallbackClient (§4.20 of
boundary-contracts.md).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

import httpx

from zubot_ingestion.domain.entities import Job
from zubot_ingestion.domain.protocols import ICallbackClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Header name carrying the HMAC-SHA256 signature of the raw request body.
SIGNATURE_HEADER: str = "X-Zubot-Signature"

#: Header name carrying the caller-supplied API key (optional mirror of the
#: inbound request auth used so receivers can authenticate us back).
API_KEY_HEADER: str = "X-API-Key"

#: Retry backoff base in seconds (first sleep is ``_BACKOFF_BASE``).
_BACKOFF_BASE: float = 0.5

#: Retry backoff growth factor (sleep ``i`` is ``base * factor ** (i - 1)``).
_BACKOFF_FACTOR: float = 2.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    """JSON encoder fallback for dataclasses / UUIDs / datetimes / enums."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serialisable")


def _job_to_payload(job: Job) -> dict[str, Any]:
    """Project a :class:`Job` into a stable JSON-serialisable payload."""
    return {
        "job_id": str(job.job_id),
        "batch_id": str(job.batch_id),
        "filename": job.filename,
        "file_hash": str(job.file_hash),
        "status": job.status.value if hasattr(job.status, "value") else str(job.status),
        "confidence_tier": (
            job.confidence_tier.value
            if job.confidence_tier is not None and hasattr(job.confidence_tier, "value")
            else None
        ),
        "overall_confidence": job.overall_confidence,
        "result": job.result,
        "error_message": job.error_message,
        "processing_time_ms": job.processing_time_ms,
        "otel_trace_id": job.otel_trace_id,
        "created_at": job.created_at.isoformat() if job.created_at is not None else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at is not None else None,
    }


def _serialise_body(payload: dict[str, Any]) -> bytes:
    """Serialise the payload to UTF-8 bytes with a stable key order.

    The hex HMAC-SHA256 signature is computed over these raw bytes, so
    receivers must validate the signature before parsing.
    """
    text = json.dumps(
        payload,
        default=_json_default,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return text.encode("utf-8")


def compute_signature(body: bytes, signing_secret: str) -> str:
    """Return the lowercase hex HMAC-SHA256 of ``body`` under ``signing_secret``.

    Exposed at module scope so tests (and any future receiver-side
    verifier) can import the exact canonical computation instead of
    re-implementing it.
    """
    return hmac.new(
        signing_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()


def _backoff_delay(attempt: int) -> float:
    """Return the sleep duration before retry ``attempt`` (1-indexed).

    ``attempt=1`` → ``base``, ``attempt=2`` → ``base * factor``, etc.
    """
    if attempt < 1:
        return 0.0
    return _BACKOFF_BASE * (_BACKOFF_FACTOR ** (attempt - 1))


# ---------------------------------------------------------------------------
# Concrete implementation
# ---------------------------------------------------------------------------


class CallbackHttpClient:
    """HTTP webhook notification client — implements :class:`ICallbackClient`.

    The constructor is keyword-only so the composition root cannot
    accidentally pass positional arguments. Pass ``signing_secret=None``
    (or an empty string) to disable request signing.
    """

    def __init__(
        self,
        *,
        signing_secret: str | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        self._signing_secret = signing_secret or None
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))

    async def notify_completion(
        self,
        callback_url: str,
        job: Job,
        api_key: str,
    ) -> bool:
        """Send a job completion webhook with bounded retry.

        Args:
            callback_url: Receiver URL to POST notification to.
            job: Completed job with result.
            api_key: API key forwarded to the receiver via ``X-API-Key``.

        Returns:
            ``True`` iff the receiver acknowledged the delivery with a
            2xx response. Never raises; a final failure logs a warning
            and returns ``False``.
        """
        payload = _job_to_payload(job)
        body = _serialise_body(payload)
        headers = self._build_headers(body, api_key)

        last_exc: Exception | None = None
        last_status: int | None = None

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                for attempt in range(1, self._max_retries + 1):
                    try:
                        response = await client.post(
                            callback_url,
                            content=body,
                            headers=headers,
                        )
                    except (httpx.TransportError, httpx.TimeoutException) as exc:
                        last_exc = exc
                        last_status = None
                        if attempt >= self._max_retries:
                            break
                        await asyncio.sleep(_backoff_delay(attempt))
                        continue

                    status = response.status_code
                    last_status = status
                    if 200 <= status < 300:
                        return True
                    if 500 <= status < 600:
                        if attempt >= self._max_retries:
                            break
                        await asyncio.sleep(_backoff_delay(attempt))
                        continue
                    # 4xx and anything else non-retryable: stop immediately.
                    logger.warning(
                        "callback.notify_completion.non_retryable_status",
                        extra={
                            "callback_url": callback_url,
                            "job_id": str(job.job_id),
                            "status_code": status,
                        },
                    )
                    return False
        except Exception as exc:  # pragma: no cover - defensive outer guard
            logger.warning(
                "callback.notify_completion.unexpected_error",
                extra={
                    "callback_url": callback_url,
                    "job_id": str(job.job_id),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return False

        logger.warning(
            "callback.notify_completion.exhausted_retries",
            extra={
                "callback_url": callback_url,
                "job_id": str(job.job_id),
                "max_retries": self._max_retries,
                "last_status_code": last_status,
                "last_error_type": type(last_exc).__name__ if last_exc else None,
                "last_error_message": str(last_exc) if last_exc else None,
            },
        )
        return False

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _build_headers(self, body: bytes, api_key: str) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "zubot-ingestion/1.0",
        }
        if api_key:
            headers[API_KEY_HEADER] = api_key
        if self._signing_secret:
            headers[SIGNATURE_HEADER] = compute_signature(body, self._signing_secret)
        return headers


# ---------------------------------------------------------------------------
# No-op variant for CALLBACK_ENABLED=False
# ---------------------------------------------------------------------------


class NoOpCallbackClient:
    """No-op :class:`ICallbackClient` used when callbacks are disabled.

    Every method returns success-like defaults without touching the
    network. Suitable for local development, CI, and integration tests
    where a live webhook receiver is unavailable.
    """

    def __init__(self) -> None:  # noqa: D401 - trivial no-op
        """Construct a no-op callback client (no state)."""

    async def notify_completion(
        self,
        callback_url: str,
        job: Job,
        api_key: str,
    ) -> bool:
        """Do nothing and return ``True``.

        The return value is ``True`` so callers that treat ``False`` as
        "webhook failed, surface error" do not emit spurious failures
        when callbacks are simply disabled.
        """
        del callback_url, job, api_key  # explicitly unused
        return True


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_callback_client(settings: Any) -> ICallbackClient:
    """Return the callback client selected by ``settings.CALLBACK_ENABLED``.

    When ``CALLBACK_ENABLED`` is ``False`` a :class:`NoOpCallbackClient`
    is returned. Otherwise a real :class:`CallbackHttpClient` is
    constructed with the configured signing secret (``None`` when the
    secret is an empty string).

    Args:
        settings: Application settings object exposing
            ``CALLBACK_ENABLED`` and ``CALLBACK_SIGNING_SECRET``
            attributes (typically :class:`zubot_ingestion.config.Settings`).

    Returns:
        An object conforming to :class:`ICallbackClient`.
    """
    if not getattr(settings, "CALLBACK_ENABLED", False):
        return NoOpCallbackClient()
    secret = getattr(settings, "CALLBACK_SIGNING_SECRET", "") or None
    return CallbackHttpClient(signing_secret=secret)


__all__ = [
    "API_KEY_HEADER",
    "CallbackHttpClient",
    "NoOpCallbackClient",
    "SIGNATURE_HEADER",
    "build_callback_client",
    "compute_signature",
]
