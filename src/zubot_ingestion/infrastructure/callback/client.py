"""Callback webhook client — implements ICallbackClient (CAP-025).

:class:`CallbackHttpClient` delivers a POST notification to the caller's
``callback_url`` when a job has completed. The payload is JSON-serialized
and signed with an HMAC-SHA256 signature in the ``X-Zubot-Signature``
header so the receiving service can verify authenticity.

Layering: infrastructure adapter. May import from stdlib, httpx, the
:mod:`zubot_ingestion.shared`, :mod:`zubot_ingestion.config`, and
:mod:`zubot_ingestion.domain.protocols` modules. MUST NOT import from
:mod:`zubot_ingestion.services` or :mod:`zubot_ingestion.api`.

Retries: the adapter retries up to three times with exponential backoff
on 5xx responses and network errors. 4xx responses are NOT retried —
they indicate a permanent delivery failure (e.g. bad URL, wrong API
key) and should be surfaced immediately so an operator can investigate.

The adapter never raises out of :meth:`notify_completion`: it returns
``True`` on successful delivery, ``False`` on permanent failure. This
keeps the orchestrator's end-of-pipeline notification path simple and
idempotent.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

from zubot_ingestion.domain.entities import Job
from zubot_ingestion.domain.protocols import ICallbackClient

__all__ = [
    "CallbackHttpClient",
    "NoOpCallbackClient",
    "build_callback_client",
]

_LOG = logging.getLogger(__name__)

#: HTTP header carrying the HMAC-SHA256 signature of the raw request body.
SIGNATURE_HEADER: str = "X-Zubot-Signature"

#: HTTP header carrying the caller-supplied API key.
API_KEY_HEADER: str = "X-Zubot-Api-Key"

#: Default number of delivery attempts before giving up (1 initial + 2 retries).
DEFAULT_MAX_RETRIES: int = 3

#: Default base backoff delay (seconds). Doubled on each retry.
DEFAULT_BACKOFF_BASE: float = 0.5


def _compute_signature(secret: str, body: bytes) -> str:
    """Return the hex-encoded HMAC-SHA256 signature of ``body`` under ``secret``."""
    return hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()


def _serialize_job(job: Job) -> dict[str, Any]:
    """Render a :class:`Job` as the JSON payload shipped to the callback URL."""
    return {
        "job_id": str(job.job_id),
        "batch_id": str(job.batch_id),
        "filename": job.filename,
        "file_hash": str(job.file_hash),
        "status": job.status.value,
        "result": job.result,
        "confidence_tier": (
            job.confidence_tier.value
            if job.confidence_tier is not None
            else None
        ),
        "overall_confidence": job.overall_confidence,
        "otel_trace_id": job.otel_trace_id,
        "processing_time_ms": job.processing_time_ms,
    }


class CallbackHttpClient(ICallbackClient):
    """Concrete ICallbackClient implementation using httpx."""

    def __init__(
        self,
        *,
        signing_secret: str = "",
        timeout: float = 10.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        logger: logging.Logger | None = None,
    ) -> None:
        self._signing_secret = signing_secret
        self._timeout = timeout
        self._max_retries = max(1, int(max_retries))
        self._backoff_base = max(0.0, float(backoff_base))
        self._log = logger or _LOG

    async def notify_completion(
        self,
        callback_url: str,
        job: Job,
        api_key: str,
    ) -> bool:
        """POST a completion notification to ``callback_url``.

        Returns True on a 2xx response, False on any permanent failure.
        Retries up to ``max_retries`` times with exponential backoff on
        5xx responses and network errors.
        """
        payload = _serialize_job(job)
        body = json.dumps(payload, sort_keys=True).encode("utf-8")

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            API_KEY_HEADER: api_key,
        }
        if self._signing_secret:
            headers[SIGNATURE_HEADER] = _compute_signature(
                self._signing_secret, body
            )

        attempt = 0
        while attempt < self._max_retries:
            attempt += 1
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        callback_url, content=body, headers=headers
                    )
            except Exception as exc:  # noqa: BLE001 - retry on network error
                self._log.warning(
                    "callback_delivery_network_error",
                    extra={
                        "callback_url": callback_url,
                        "attempt": attempt,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                if attempt >= self._max_retries:
                    return False
                await asyncio.sleep(self._backoff_base * (2 ** (attempt - 1)))
                continue

            if 200 <= response.status_code < 300:
                return True

            if 400 <= response.status_code < 500:
                # Permanent — don't retry.
                self._log.warning(
                    "callback_delivery_4xx",
                    extra={
                        "callback_url": callback_url,
                        "attempt": attempt,
                        "status_code": response.status_code,
                    },
                )
                return False

            # 5xx — retry.
            self._log.warning(
                "callback_delivery_5xx",
                extra={
                    "callback_url": callback_url,
                    "attempt": attempt,
                    "status_code": response.status_code,
                },
            )
            if attempt >= self._max_retries:
                return False
            await asyncio.sleep(self._backoff_base * (2 ** (attempt - 1)))

        return False


class NoOpCallbackClient(ICallbackClient):
    """No-op fallback ICallbackClient used when callbacks are disabled.

    Every :meth:`notify_completion` call logs a debug line and returns
    ``True``. This is the safe default in local/dev environments where
    no callback signing secret has been configured.
    """

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._log = logger or _LOG

    async def notify_completion(
        self,
        callback_url: str,
        job: Job,
        api_key: str,
    ) -> bool:
        self._log.debug(
            "noop_callback_client_notify_completion",
            extra={"callback_url": callback_url, "job_id": str(job.job_id)},
        )
        return True


def build_callback_client(settings: Any) -> ICallbackClient:
    """Factory: return a configured ICallbackClient.

    Returns a real :class:`CallbackHttpClient` when
    ``settings.CALLBACK_ENABLED`` is truthy. Otherwise returns a
    :class:`NoOpCallbackClient` so the composition root is always fully
    wired.
    """
    enabled = bool(getattr(settings, "CALLBACK_ENABLED", False))
    if not enabled:
        return NoOpCallbackClient()
    signing_secret = str(getattr(settings, "CALLBACK_SIGNING_SECRET", "") or "")
    return CallbackHttpClient(signing_secret=signing_secret)
