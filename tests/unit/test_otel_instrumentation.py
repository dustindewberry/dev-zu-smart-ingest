"""Unit tests for ``zubot_ingestion.infrastructure.otel.instrumentation``.

These tests cover CAP-027 setup_otel() and get_tracer() behavior:

* setup_otel() with no endpoint installs an in-process TracerProvider with
  the correct service.name + service.version resource attributes
* setup_otel() with an endpoint adds an OTLP gRPC exporter
* setup_otel() is idempotent (safe to call multiple times)
* setup_otel() turns on the five auto-instrumentations and tolerates
  missing optional dependencies
* get_tracer() returns a Tracer in the ``zubot_ingestion`` scope
* The lifespan handler in api/app.py calls setup_otel() with the
  configured OTEL_EXPORTER_OTLP_ENDPOINT
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from zubot_ingestion.infrastructure.otel.instrumentation import (
    get_tracer,
    setup_otel,
)
from zubot_ingestion.shared.constants import SERVICE_NAME, SERVICE_VERSION


@pytest.fixture(autouse=True)
def _reset_otel_global_state() -> None:
    """Reset OTEL's global tracer-provider latch before each test.

    ``trace.set_tracer_provider`` installs the provider exactly once per
    process via a ``SET_ONCE`` latch; subsequent calls log a warning and
    silently keep the first provider. Without this reset, every test
    after the first would see stale state and fail on assertions about
    the resource attributes or registered span processors.
    """
    from opentelemetry.util._once import Once

    trace._TRACER_PROVIDER_SET_ONCE = Once()  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]


class TestSetupOtelNoEndpoint:
    """setup_otel() called with otlp_endpoint=None."""

    def test_installs_tracer_provider(self) -> None:
        setup_otel(otlp_endpoint=None)
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)

    def test_resource_carries_service_name(self) -> None:
        setup_otel(otlp_endpoint=None)
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        resource: Resource = provider.resource
        assert resource.attributes["service.name"] == SERVICE_NAME

    def test_resource_carries_service_version(self) -> None:
        setup_otel(otlp_endpoint=None)
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        resource: Resource = provider.resource
        assert resource.attributes["service.version"] == SERVICE_VERSION

    def test_custom_service_name_overrides_default(self) -> None:
        setup_otel(service_name="zubot-ingestion-test", otlp_endpoint=None)
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        assert (
            provider.resource.attributes["service.name"]
            == "zubot-ingestion-test"
        )

    def test_no_span_processors_attached_when_endpoint_none(self) -> None:
        setup_otel(otlp_endpoint=None)
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        # provider._active_span_processor.SpanProcessors gives the
        # underlying list. With no endpoint we expect zero processors.
        active = provider._active_span_processor  # type: ignore[attr-defined]
        # The composite processor exposes a tuple of children. When the
        # OTLP exporter is not installed there should be exactly zero.
        children = getattr(active, "_span_processors", ())
        assert len(children) == 0


class TestSetupOtelWithEndpoint:
    """setup_otel() called with a real otlp_endpoint string."""

    def test_otlp_exporter_attached_when_endpoint_set(self) -> None:
        with patch(
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter."
            "OTLPSpanExporter"
        ) as mock_exporter_cls:
            setup_otel(otlp_endpoint="http://otel-collector:4317")

            # The exporter constructor must be called with the endpoint
            # and insecure=True per the task spec.
            mock_exporter_cls.assert_called_once_with(
                endpoint="http://otel-collector:4317", insecure=True
            )

        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        active = provider._active_span_processor  # type: ignore[attr-defined]
        children = getattr(active, "_span_processors", ())
        # Exactly one BatchSpanProcessor should be wired up.
        assert len(children) == 1

    def test_empty_endpoint_treated_as_none(self) -> None:
        # An empty string is falsy and should NOT install an exporter.
        with patch(
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter."
            "OTLPSpanExporter"
        ) as mock_exporter_cls:
            setup_otel(otlp_endpoint="")
            mock_exporter_cls.assert_not_called()


class TestSetupOtelIdempotency:
    """setup_otel() must be safe to call multiple times."""

    def test_calling_twice_does_not_raise(self) -> None:
        setup_otel(otlp_endpoint=None)
        setup_otel(otlp_endpoint=None)  # second call must be safe

    def test_second_call_replaces_provider(self) -> None:
        setup_otel(service_name="first", otlp_endpoint=None)
        first_provider = trace.get_tracer_provider()
        setup_otel(service_name="second", otlp_endpoint=None)
        second_provider = trace.get_tracer_provider()
        # The OTEL SDK allows overriding the global provider — both
        # objects should be TracerProvider instances and the second one
        # should carry the new service.name attribute.
        assert isinstance(second_provider, TracerProvider)
        assert (
            second_provider.resource.attributes["service.name"] == "second"
        )


class TestAutoInstrumentations:
    """setup_otel() must call instrument() on each of the five libraries."""

    def test_instrumentors_are_invoked(self) -> None:
        # We patch each instrumentor at its CANONICAL module path so that
        # the dynamic __import__() inside _enable_auto_instrumentations()
        # picks up the mock.
        with (
            patch(
                "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor"
            ) as mock_fastapi,
            patch(
                "opentelemetry.instrumentation.httpx."
                "HTTPXClientInstrumentor"
            ) as mock_httpx,
            patch(
                "opentelemetry.instrumentation.asyncpg."
                "AsyncPGInstrumentor"
            ) as mock_asyncpg,
            patch(
                "opentelemetry.instrumentation.redis.RedisInstrumentor"
            ) as mock_redis,
            patch(
                "opentelemetry.instrumentation.celery.CeleryInstrumentor"
            ) as mock_celery,
        ):
            setup_otel(otlp_endpoint=None)

            mock_fastapi.return_value.instrument.assert_called_once()
            mock_httpx.return_value.instrument.assert_called_once()
            mock_asyncpg.return_value.instrument.assert_called_once()
            mock_redis.return_value.instrument.assert_called_once()
            mock_celery.return_value.instrument.assert_called_once()

    def test_missing_instrumentor_does_not_break_setup(self) -> None:
        # If an instrumentor's underlying library isn't installed, the
        # __import__ raises ModuleNotFoundError. setup_otel() must NOT
        # propagate that. We simulate by patching __import__ to raise
        # for one specific module path.
        original_import = __builtins__["__import__"] if isinstance(
            __builtins__, dict
        ) else __import__

        def fake_import(name, *args, **kwargs):
            if name == "opentelemetry.instrumentation.asyncpg":
                raise ModuleNotFoundError("simulated missing asyncpg")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            # Must not raise.
            setup_otel(otlp_endpoint=None)

    def test_instrumentor_already_active_does_not_raise(self) -> None:
        # Some instrumentors raise on the second .instrument() call. The
        # wrapper must catch and log-debug, never re-raise.
        with patch(
            "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor"
        ) as mock_fastapi:
            mock_fastapi.return_value.instrument.side_effect = (
                RuntimeError("already instrumented")
            )
            # Must not raise.
            setup_otel(otlp_endpoint=None)


class TestGetTracer:
    """get_tracer() returns a Tracer scoped to ``zubot_ingestion``."""

    def test_returns_tracer(self) -> None:
        setup_otel(otlp_endpoint=None)
        tracer = get_tracer()
        # The OTEL SDK's Tracer doesn't have a stable public type to
        # isinstance against, but it always exposes start_as_current_span.
        assert hasattr(tracer, "start_as_current_span")

    def test_tracer_emits_spans_in_correct_scope(self) -> None:
        setup_otel(otlp_endpoint=None)
        tracer = get_tracer()
        with tracer.start_as_current_span("test.span") as span:
            ctx = span.get_span_context()
            # Trace ID should be a non-zero 128-bit integer.
            assert ctx.trace_id != 0
            assert ctx.span_id != 0

    def test_tracer_scope_name_is_zubot_ingestion(self) -> None:
        setup_otel(otlp_endpoint=None)
        tracer = get_tracer()
        # OTEL SDK Tracer exposes the instrumentation scope on the
        # `_instrumentation_scope` private attribute.
        scope = getattr(tracer, "_instrumentation_scope", None)
        if scope is not None:
            assert scope.name == "zubot_ingestion"


class TestLifespanWiring:
    """The api/app.py lifespan handler must call setup_otel() on startup."""

    @pytest.mark.asyncio
    async def test_lifespan_calls_setup_otel(self) -> None:
        from zubot_ingestion.api.app import lifespan, create_app

        # get_settings() is lru_cached — clear the cache so the test can
        # inject its own settings.
        from zubot_ingestion.config import get_settings

        get_settings.cache_clear()

        # Also patch setup_logging so the lifespan handler doesn't try to
        # mkdir the production log directory under /var/log/.
        with patch(
            "zubot_ingestion.api.app.setup_otel"
        ) as mock_setup_otel, patch(
            "zubot_ingestion.api.app.setup_logging"
        ):
            app = create_app()
            async with lifespan(app):
                pass

            mock_setup_otel.assert_called_once()
            # Verify the call passes the endpoint from settings.
            call_kwargs = mock_setup_otel.call_args.kwargs
            assert "otlp_endpoint" in call_kwargs

    @pytest.mark.asyncio
    async def test_lifespan_passes_configured_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317"
        )
        from zubot_ingestion.config import get_settings

        get_settings.cache_clear()

        from zubot_ingestion.api.app import lifespan, create_app

        with patch(
            "zubot_ingestion.api.app.setup_otel"
        ) as mock_setup_otel, patch(
            "zubot_ingestion.api.app.setup_logging"
        ):
            app = create_app()
            async with lifespan(app):
                pass

            mock_setup_otel.assert_called_once_with(
                otlp_endpoint="http://collector:4317"
            )

        get_settings.cache_clear()
