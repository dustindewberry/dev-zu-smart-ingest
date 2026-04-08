"""Ollama HTTP client — concrete implementation of ``IOllamaClient``.

This module is the only code in the service that talks to the Ollama
inference server over HTTP. Callers (Stage 1 extractors, Stage 2
companion generator, health probes) depend on the ``IOllamaClient``
protocol and receive an ``OllamaClient`` instance via dependency
injection from the composition root.

Transport
---------
Vision requests go to ``{base_url}/api/generate`` (POST) and text
requests go to ``{base_url}/api/chat`` (POST) with structured
``messages=[{"role": "system", ...}, {"role": "user", ...}]`` turns so
untrusted document text is isolated from the instruction prompt.
Model-availability checks use ``{base_url}/api/tags`` (GET). All
calls flow through ``httpx.AsyncClient``. Retries are applied only on
503 and 429 responses using a fixed exponential backoff of 1s / 2s /
4s across three total attempts. On persistent failure the client
raises ``OllamaError``, which callers translate into domain-specific
errors (``OllamaTimeoutError`` / ``OllamaUnavailableError``) if needed.

Prompt-injection hardening
--------------------------
``generate_text`` passes the instruction and the untrusted document
text as separate ``system`` / ``user`` turns over ``/api/chat`` AND
defensively escapes closing fences (``` ``` ```) and closing
XML-like tags (``</context>``, ``</system>``, ``</user>``) in the
untrusted text via :func:`_escape_untrusted_text`, using a zero-width
space to neutralize exact-match token sequences without altering
the rendered output.

Determinism
-----------
The default ``temperature`` is ``OLLAMA_TEMPERATURE_DETERMINISTIC`` (0.0)
so the extraction pipeline produces reproducible output. Callers that
need non-deterministic sampling must pass an explicit value.

Instrumentation (CAP-028)
-------------------------
Each ``_post_generate`` call is wrapped in :func:`time_ollama_call` so
the wall-clock duration lands in the ``ollama_duration{model=...}``
histogram for both the success and failure paths. Inside the retry
loop, every retried 503/429/transport-error attempt records a
``status='retry'`` increment via :func:`record_ollama_request_status`,
the final 200 records ``status='success'``, and any non-retryable
HTTP error / exhausted retry budget records ``status='error'``. The
status taxonomy constants are exposed as ``STATUS_SUCCESS``,
``STATUS_ERROR``, and ``STATUS_RETRY``.

Implements CAP-008. Protocol contract: IOllamaClient (§4.16 of
boundary-contracts.md).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterator

from zubot_ingestion.domain.entities import OllamaResponse
from zubot_ingestion.domain.protocols import IOllamaClient
from zubot_ingestion.infrastructure.metrics.prometheus import (
    ollama_duration,
    ollama_requests,
)
from zubot_ingestion.shared.constants import (
    OLLAMA_MODEL_TEXT,
    OLLAMA_MODEL_VISION,
    OLLAMA_TEMPERATURE_DETERMINISTIC,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt-injection hardening helpers
# ---------------------------------------------------------------------------


def _escape_untrusted_text(text: str) -> str:
    """Defensive escaping for user-controlled text passed to the LLM.

    Even though /api/chat separates system and user turns, some models
    concatenate message content internally. Neutralize closing fences,
    closing XML-like tags, and obvious instruction-hijack phrases.

    A zero-width space (``\u200b``) is inserted inside the escaped
    sequences so the sanitized text renders identically to the
    original but no longer matches exact token sequences that models
    or downstream consumers might interpret as structural markers.
    """
    if not text:
        return text
    # Escape triple backticks and common closing tags.
    sanitized = text.replace("```", "`\u200b``")
    sanitized = sanitized.replace("</context>", "</\u200bcontext>")
    sanitized = sanitized.replace("</system>", "</\u200bsystem>")
    sanitized = sanitized.replace("</user>", "</\u200buser>")
    return sanitized


# ---------------------------------------------------------------------------
# CAP-028 instrumentation helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OllamaError(RuntimeError):
    """Raised when the Ollama client exhausts its retry budget.

    The ``status_code`` attribute is populated when the failure mode is
    a persistent HTTP error (503, 429, other 5xx, or a non-200 final
    attempt). It is ``None`` when the failure is a transport-level
    error (timeout, connection refused) that never produced a response.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.__cause__ = cause


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

_MAX_ATTEMPTS: int = 3
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 503})
_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)


def _sleep_for_attempt(attempt_index: int) -> float:
    """Return the backoff duration (seconds) before retry attempt N.

    ``attempt_index`` is zero-based relative to the FIRST retry, i.e.
    the wait BEFORE retry 1 is ``_BACKOFF_SECONDS[0]`` (1s), before
    retry 2 is ``_BACKOFF_SECONDS[1]`` (2s), and before retry 3 is
    ``_BACKOFF_SECONDS[2]`` (4s). Indices beyond the defined schedule
    fall back to the last defined value.
    """
    if attempt_index < 0:
        return 0.0
    if attempt_index >= len(_BACKOFF_SECONDS):
        return _BACKOFF_SECONDS[-1]
    return _BACKOFF_SECONDS[attempt_index]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OllamaClient(IOllamaClient):
    """Concrete httpx-based implementation of ``IOllamaClient``.

    Parameters
    ----------
    base_url:
        Base URL of the Ollama server, e.g. ``http://ollama:11434``.
        A trailing slash is allowed and will be stripped on construction.
    default_timeout:
        Default request timeout in seconds. Individual calls can
        override this via the ``timeout_seconds`` parameter. Stored as
        an ``int`` to match the protocol-defined constructor signature,
        but forwarded to ``httpx`` as a ``float``.
    transport:
        Optional ``httpx.AsyncBaseTransport`` for dependency injection
        during testing (e.g. ``httpx.MockTransport`` or a ``respx``
        mock router). In production, leave as ``None`` so httpx uses
        its default network transport.
    """

    def __init__(
        self,
        base_url: str,
        default_timeout: int = 120,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url: str = base_url.rstrip("/")
        self._default_timeout: int = default_timeout
        self._transport: httpx.AsyncBaseTransport | None = transport

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate_vision(
        self,
        image_base64: str,
        prompt: str,
        model: str = OLLAMA_MODEL_VISION,
        temperature: float = OLLAMA_TEMPERATURE_DETERMINISTIC,
        timeout_seconds: int = 120,
    ) -> OllamaResponse:
        """POST an image + prompt to the vision model.

        See ``IOllamaClient.generate_vision`` for the contract. The
        request body matches Ollama's ``/api/generate`` envelope with
        ``format='json'`` (forcing JSON output) and ``stream=false``
        (single non-streamed response).
        """
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "images": [image_base64],
            "format": "json",
            "stream": False,
            "options": {"temperature": temperature},
        }
        return await self._post_generate(
            payload=payload,
            model=model,
            timeout_seconds=int(timeout_seconds),
        )

    async def generate_text(
        self,
        text: str,
        prompt: str,
        model: str = OLLAMA_MODEL_TEXT,
        temperature: float = 0.0,
        timeout_seconds: int = 60,
    ) -> OllamaResponse:
        """POST a text-only prompt with embedded context to the text model.

        Prompt-injection hardening (hybrid fix):
            1. The call targets Ollama's ``/api/chat`` endpoint and
               passes the instruction and the untrusted document text
               as two distinct turns — ``prompt`` becomes the
               ``system`` message and ``text`` becomes the ``user``
               message. This gives models that honour turn boundaries
               real structural separation.
            2. As belt-and-suspenders defence against models that
               concatenate message content internally, the untrusted
               ``text`` is first passed through
               :func:`_escape_untrusted_text` so exact-match closing
               fences and closing XML-like tags are neutralized via
               zero-width-space insertion.

        The public signature is preserved: callers still pass
        ``(text, prompt)`` and receive an ``OllamaResponse`` whose
        ``response_text`` holds the assistant's reply.
        """
        safe_text = _escape_untrusted_text(text)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": safe_text},
        ]
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "format": "json",
            "stream": False,
            "options": {"temperature": temperature},
        }
        return await self._post_chat(
            payload=payload,
            model=model,
            timeout_seconds=int(timeout_seconds),
        )

    async def check_model_available(self, model: str) -> bool:
        """Return ``True`` iff Ollama reports ``model`` is installed.

        Calls ``GET /api/tags``, parses the ``models`` array, and
        returns ``True`` if any entry's ``name`` field starts with the
        requested model string. This prefix-match is intentional: it
        allows callers to pass either a bare model name
        (``"qwen2.5vl"``) or a fully-qualified tag
        (``"qwen2.5vl:7b"``).

        Network errors and unexpected status codes are swallowed and
        logged, returning ``False``. This method must not raise so it
        is safe to call from the health-check endpoint.
        """
        url = f"{self._base_url}/api/tags"
        try:
            async with self._client(timeout=self._default_timeout) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            logger.warning(
                "ollama.check_model_available transport failure: %s", exc
            )
            return False

        if response.status_code != 200:
            logger.warning(
                "ollama.check_model_available non-200: %s", response.status_code
            )
            return False

        try:
            body = response.json()
        except ValueError:
            logger.warning("ollama.check_model_available non-JSON body")
            return False

        models = body.get("models") if isinstance(body, dict) else None
        if not isinstance(models, list):
            return False

        for entry in models:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if isinstance(name, str) and name.startswith(model):
                return True
        return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _client(self, timeout: float) -> httpx.AsyncClient:
        """Build a fresh ``httpx.AsyncClient`` for a single request.

        A new client is constructed per request so the supplied
        per-call timeout is honoured and so the optional injected
        ``transport`` (used for mocking in tests) is attached.
        """
        kwargs: dict[str, Any] = {"timeout": timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def _post_generate(
        self,
        *,
        payload: dict[str, Any],
        model: str,
        timeout_seconds: int,
    ) -> OllamaResponse:
        """POST ``payload`` to ``/api/generate`` with retry + parsing.

        Retries are limited to ``_MAX_ATTEMPTS`` (3) total attempts.
        The retry schedule is applied ONLY when the server returns an
        HTTP status listed in ``_RETRYABLE_STATUS_CODES`` (503, 429).
        Transport errors (timeouts, connection resets) are also
        retried with the same backoff schedule. Any other non-2xx
        status code short-circuits with an immediate ``OllamaError``.

        CAP-028: the entire body is wrapped in :func:`time_ollama_call`
        so the request duration is recorded for both the success and
        failure paths. Each retried attempt increments the
        ``status='retry'`` counter, the final 200 increments
        ``status='success'``, and any error path (non-retryable HTTP
        status, exhausted retries) increments ``status='error'``.
        """
        url = f"{self._base_url}/api/generate"
        last_error: BaseException | None = None
        last_status: int | None = None

        with time_ollama_call(model=model):
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    async with self._client(
                        timeout=float(timeout_seconds)
                    ) as client:
                        response = await client.post(url, json=payload)
                except httpx.HTTPError as exc:
                    last_error = exc
                    last_status = None
                    logger.warning(
                        "ollama.generate transport failure (attempt %d/%d): %s",
                        attempt + 1,
                        _MAX_ATTEMPTS,
                        exc,
                    )
                    if attempt + 1 < _MAX_ATTEMPTS:
                        record_ollama_request_status(model, STATUS_RETRY)
                        await asyncio.sleep(_sleep_for_attempt(attempt))
                        continue
                    break

                status = response.status_code
                if status == 200:
                    record_ollama_request_status(model, STATUS_SUCCESS)
                    return self._parse_response(response, model=model)

                if status in _RETRYABLE_STATUS_CODES:
                    last_error = None
                    last_status = status
                    logger.warning(
                        "ollama.generate retryable status %d (attempt %d/%d)",
                        status,
                        attempt + 1,
                        _MAX_ATTEMPTS,
                    )
                    if attempt + 1 < _MAX_ATTEMPTS:
                        record_ollama_request_status(model, STATUS_RETRY)
                        await asyncio.sleep(_sleep_for_attempt(attempt))
                        continue
                    break

                # Non-retryable HTTP error — abort immediately.
                record_ollama_request_status(model, STATUS_ERROR)
                raise OllamaError(
                    f"Ollama /api/generate returned HTTP {status}",
                    status_code=status,
                )

            # All attempts exhausted.
            record_ollama_request_status(model, STATUS_ERROR)
            if last_status is not None:
                raise OllamaError(
                    f"Ollama /api/generate exhausted {_MAX_ATTEMPTS} retries "
                    f"(last status {last_status})",
                    status_code=last_status,
                )
            raise OllamaError(
                f"Ollama /api/generate exhausted {_MAX_ATTEMPTS} retries "
                f"(transport error)",
                status_code=None,
                cause=last_error,
            )

    @staticmethod
    def _parse_response(
        response: httpx.Response,
        *,
        model: str,
    ) -> OllamaResponse:
        """Convert a successful HTTP response into an ``OllamaResponse``.

        Ollama returns a JSON object whose notable fields are:
            response: str — the model's generated text
            prompt_eval_count: int | None
            eval_count: int | None
            total_duration: int | None — in nanoseconds
        Unknown fields are preserved verbatim in ``OllamaResponse.raw``.
        """
        try:
            body = response.json()
        except ValueError as exc:
            raise OllamaError(
                "Ollama /api/generate returned non-JSON body",
                status_code=response.status_code,
                cause=exc,
            ) from exc

        if not isinstance(body, dict):
            raise OllamaError(
                "Ollama /api/generate returned non-object JSON body",
                status_code=response.status_code,
            )

        response_text = body.get("response")
        if not isinstance(response_text, str):
            raise OllamaError(
                "Ollama /api/generate response missing 'response' field",
                status_code=response.status_code,
            )

        return OllamaResponse(
            response_text=response_text,
            model=model,
            prompt_eval_count=_coerce_optional_int(body.get("prompt_eval_count")),
            eval_count=_coerce_optional_int(body.get("eval_count")),
            total_duration_ns=_coerce_optional_int(body.get("total_duration")),
            raw=body,
        )

    async def _post_chat(
        self,
        *,
        payload: dict[str, Any],
        model: str,
        timeout_seconds: int,
    ) -> OllamaResponse:
        """POST ``payload`` to ``/api/chat`` with retry + parsing.

        Parallel to :meth:`_post_generate` but targets Ollama's
        ``/api/chat`` endpoint, whose response envelope is
        ``{"message": {"role": "assistant", "content": "..."}, ...}``
        instead of ``{"response": "..."}``. All retry / backoff /
        Prometheus metrics semantics are identical to
        :meth:`_post_generate` so ``generate_text`` gets the same
        observability treatment as ``generate_vision``.
        """
        url = f"{self._base_url}/api/chat"
        last_error: BaseException | None = None
        last_status: int | None = None

        with time_ollama_call(model=model):
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    async with self._client(
                        timeout=float(timeout_seconds)
                    ) as client:
                        response = await client.post(url, json=payload)
                except httpx.HTTPError as exc:
                    last_error = exc
                    last_status = None
                    logger.warning(
                        "ollama.chat transport failure (attempt %d/%d): %s",
                        attempt + 1,
                        _MAX_ATTEMPTS,
                        exc,
                    )
                    if attempt + 1 < _MAX_ATTEMPTS:
                        record_ollama_request_status(model, STATUS_RETRY)
                        await asyncio.sleep(_sleep_for_attempt(attempt))
                        continue
                    break

                status = response.status_code
                if status == 200:
                    record_ollama_request_status(model, STATUS_SUCCESS)
                    return self._parse_chat_response(response, model=model)

                if status in _RETRYABLE_STATUS_CODES:
                    last_error = None
                    last_status = status
                    logger.warning(
                        "ollama.chat retryable status %d (attempt %d/%d)",
                        status,
                        attempt + 1,
                        _MAX_ATTEMPTS,
                    )
                    if attempt + 1 < _MAX_ATTEMPTS:
                        record_ollama_request_status(model, STATUS_RETRY)
                        await asyncio.sleep(_sleep_for_attempt(attempt))
                        continue
                    break

                # Non-retryable HTTP error — abort immediately.
                record_ollama_request_status(model, STATUS_ERROR)
                raise OllamaError(
                    f"Ollama /api/chat returned HTTP {status}",
                    status_code=status,
                )

            # All attempts exhausted.
            record_ollama_request_status(model, STATUS_ERROR)
            if last_status is not None:
                raise OllamaError(
                    f"Ollama /api/chat exhausted {_MAX_ATTEMPTS} retries "
                    f"(last status {last_status})",
                    status_code=last_status,
                )
            raise OllamaError(
                f"Ollama /api/chat exhausted {_MAX_ATTEMPTS} retries "
                f"(transport error)",
                status_code=None,
                cause=last_error,
            )

    @staticmethod
    def _parse_chat_response(
        response: httpx.Response,
        *,
        model: str,
    ) -> OllamaResponse:
        """Convert a successful ``/api/chat`` response into ``OllamaResponse``.

        Ollama's ``/api/chat`` endpoint returns a JSON object shaped as::

            {
                "message": {"role": "assistant", "content": "..."},
                "done": true,
                "prompt_eval_count": int | None,
                "eval_count": int | None,
                "total_duration": int | None,
                ...
            }

        The assistant's text is extracted from ``message.content`` and
        mapped onto the existing :class:`OllamaResponse.response_text`
        field so downstream callers remain oblivious to the endpoint
        migration.
        """
        try:
            body = response.json()
        except ValueError as exc:
            raise OllamaError(
                "Ollama /api/chat returned non-JSON body",
                status_code=response.status_code,
                cause=exc,
            ) from exc

        if not isinstance(body, dict):
            raise OllamaError(
                "Ollama /api/chat returned non-object JSON body",
                status_code=response.status_code,
            )

        message = body.get("message")
        if not isinstance(message, dict):
            raise OllamaError(
                "Ollama /api/chat response missing 'message' object",
                status_code=response.status_code,
            )

        content = message.get("content")
        if not isinstance(content, str):
            raise OllamaError(
                "Ollama /api/chat response missing 'message.content' field",
                status_code=response.status_code,
            )

        return OllamaResponse(
            response_text=content,
            model=model,
            prompt_eval_count=_coerce_optional_int(body.get("prompt_eval_count")),
            eval_count=_coerce_optional_int(body.get("eval_count")),
            total_duration_ns=_coerce_optional_int(body.get("total_duration")),
            raw=body,
        )


def _coerce_optional_int(value: Any) -> int | None:
    """Coerce a JSON-decoded value into ``int | None``.

    Ollama sometimes omits counter fields on short responses, so we
    gracefully pass through ``None`` and refuse to raise on unexpected
    numeric-looking values.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is a subclass of int — reject it explicitly.
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


__all__ = [
    "STATUS_ERROR",
    "STATUS_RETRY",
    "STATUS_SUCCESS",
    "OllamaClient",
    "OllamaError",
    "_escape_untrusted_text",
    "record_ollama_request_status",
    "time_ollama_call",
]
