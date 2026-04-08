"""Callback HTTP client (CAP-025 / step-21 canonical)."""
from __future__ import annotations

import asyncio
import logging

import httpx

from zubot_ingestion.domain.protocols import ICallbackClient
from zubot_ingestion.shared.types import JobId

_LOG = logging.getLogger(__name__)


class CallbackHttpClient:
    def __init__(
        self,
        *,
        timeout: float = 10.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay

    async def notify_completion(
        self,
        *,
        callback_url: str,
        job_id: JobId,
        status: str,
        result: dict | None,
        error: str | None,
    ) -> bool:
        payload = {
            "job_id": str(job_id),
            "status": status,
            "result": result,
            "error": error,
        }
        for attempt in range(self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(callback_url, json=payload)
                    if 200 <= response.status_code < 300:
                        return True
                    _LOG.warning(
                        "callback non-2xx",
                        extra={"status": response.status_code, "attempt": attempt},
                    )
            except httpx.HTTPError as exc:
                _LOG.warning("callback error", extra={"error": str(exc), "attempt": attempt})
            if attempt < self._max_retries - 1:
                await asyncio.sleep(self._retry_delay * (2 ** attempt))
        return False


__all__ = ["CallbackHttpClient"]
