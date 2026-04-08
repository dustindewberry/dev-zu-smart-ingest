"""OpenTelemetry tracing setup and tracer accessor (CAP-027).

This module wires up an OTEL ``TracerProvider`` for the zubot-ingestion
service, attaches an OTLP gRPC exporter (when an endpoint is configured),
and turns on the auto-instrumentations for FastAPI, HTTPX, asyncpg, Redis,
and Celery so that calls into those libraries automatically produce spans.

Design notes
------------

* :func:`setup_otel` is **idempotent**. Calling it more than once is safe and
  is in fact required for some test scenarios (FastAPI lifespan handlers
  may run multiple times under TestClient). On the second and subsequent
  calls a fresh :class:`TracerProvider` is installed and the auto-
  instrumentations are re-registered. Each individual auto-instrumentor is
  wrapped in its own try/except so that a missing optional dependency (e.g.
  asyncpg in a non-database test environment) does not bring down startup.

* :func:`get_tracer` is a thin wrapper around
  :func:`opentelemetry.trace.get_tracer` so callers throughout the codebase
  can keep their imports local to ``zubot_ingestion.infrastructure.otel``
  rather than reaching directly into the OTEL SDK. The instrumentation
  scope name is fixed at ``"zubot_ingestion"`` so all spans produced by
  application code (orchestrator, extractors, etc.) carry a consistent
  scope identifier.

* The OTLP gRPC exporter is registered ONLY when ``otlp_endpoint`` is a
  truthy string. Production deployments set
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` to the collector URL; local dev and unit
  tests typically leave it unset and the resulting in-memory provider
  produces spans that callers can still inspect (e.g. via an
  ``InMemorySpanExporter`` registered separately).

Layering
--------

This module lives in the infrastructure layer per
``boundary-contracts.md`` §3 (it depends on third-party libraries —
opentelemetry-sdk, opentelemetry-exporter-otlp). It MUST NOT import from
the api or services layers; it imports only from
``zubot_ingestion.shared.constants``, the OTEL SDK, and stdlib.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from zubot_ingestion.shared.constants import SERVICE_NAME, SERVICE_VERSION

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

__all__ = ["get_tracer", "setup_otel"]

_LOG = logging.getLogger(__name__)

# Instrumentation scope name used by every tracer this module hands out.
_TRACER_SCOPE: str = "zubot_ingestion"


def setup_otel(
    service_name: str = SERVICE_NAME,
    otlp_endpoint: str | None = None,
) -> None:
    """Configure the global OpenTelemetry tracer provider.

    Args:
        service_name: The ``service.name`` resource attribute. Defaults to
            the canonical :data:`SERVICE_NAME` constant
            (``"zubot-ingestion"``).
        otlp_endpoint: Optional OTLP gRPC collector endpoint
            (e.g. ``"http://otel-collector:4317"``). When ``None`` or
            empty, no exporter is attached and spans remain in-process —
            useful for local dev and unit testing.

    Side effects:
        * Replaces the global :class:`TracerProvider` with a freshly
          constructed one carrying ``service.name`` and ``service.version``
          resource attributes.
        * If ``otlp_endpoint`` is set, attaches a
          :class:`BatchSpanProcessor` exporting via the OTLP gRPC protocol.
        * Calls :meth:`instrument` on FastAPIInstrumentor,
          HTTPXClientInstrumentor, AsyncPGInstrumentor, RedisInstrumentor,
          and CeleryInstrumentor. Each instrumentor is invoked in its own
          try/except so an unavailable optional dependency does not break
          startup.
    """
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": SERVICE_VERSION,
        }
    )
    provider = TracerProvider(resource=resource)

    if otlp_endpoint:
        # Lazy-import the gRPC exporter so environments without the gRPC
        # extension still get a usable in-process tracer when otlp_endpoint
        # is left unset.
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)

    _enable_auto_instrumentations()


def _enable_auto_instrumentations() -> None:
    """Best-effort registration of OTEL auto-instrumentations.

    Each instrumentor is invoked inside its own try/except so a missing
    optional library (asyncpg in a frontend-only deployment, celery in an
    api-only deployment, etc.) does not block startup. We log a debug
    message rather than warning because these failures are expected and
    benign in environments where the target library is intentionally
    absent.
    """
    instrumentors: list[tuple[str, str, str]] = [
        (
            "fastapi",
            "opentelemetry.instrumentation.fastapi",
            "FastAPIInstrumentor",
        ),
        (
            "httpx",
            "opentelemetry.instrumentation.httpx",
            "HTTPXClientInstrumentor",
        ),
        (
            "asyncpg",
            "opentelemetry.instrumentation.asyncpg",
            "AsyncPGInstrumentor",
        ),
        (
            "redis",
            "opentelemetry.instrumentation.redis",
            "RedisInstrumentor",
        ),
        (
            "celery",
            "opentelemetry.instrumentation.celery",
            "CeleryInstrumentor",
        ),
    ]

    for label, module_path, class_name in instrumentors:
        try:
            module = __import__(module_path, fromlist=[class_name])
            instrumentor_cls = getattr(module, class_name)
            instrumentor = instrumentor_cls()
            # Some instrumentors raise if already instrumented; suppress.
            try:
                instrumentor.instrument()
            except Exception as exc:  # noqa: BLE001 - already-instrumented OK
                _LOG.debug(
                    "otel_instrumentor_already_active",
                    extra={"library": label, "error": str(exc)},
                )
        except Exception as exc:  # noqa: BLE001 - missing dep is fine
            _LOG.debug(
                "otel_instrumentor_unavailable",
                extra={"library": label, "error": str(exc)},
            )


def get_tracer() -> "Tracer":
    """Return the application's :class:`Tracer`.

    All call sites in :mod:`zubot_ingestion` should obtain their tracer
    from this function so that the instrumentation scope is consistent
    (``"zubot_ingestion"``) across the codebase.
    """
    return trace.get_tracer(_TRACER_SCOPE)
