"""Top-level test configuration.

Sets environment variables that the application reads at import time so the
test suite is hermetic and does not depend on a live Redis, a writable
``/var/log/zubot-ingestion``, or a developer ``.env`` file.

Notes
-----
This module is imported by pytest BEFORE any test module is collected, so
the env vars set here are visible to module-level imports performed by the
test files (e.g. ``from zubot_ingestion.api.middleware.rate_limit import
limiter`` would otherwise instantiate a Limiter pointed at the production
Redis URL).
"""

from __future__ import annotations

import os
import tempfile

# ---------------------------------------------------------------------------
# Logging — point LOG_DIR at a writable temporary directory so the lifespan
# / setup_logging() call does not raise PermissionError when trying to mkdir
# the production /var/log path.
# ---------------------------------------------------------------------------
_TEST_LOG_DIR = os.environ.get("ZUBOT_TEST_LOG_DIR") or tempfile.mkdtemp(
    prefix="zubot-test-logs-"
)
os.environ.setdefault("LOG_DIR", _TEST_LOG_DIR)

# ---------------------------------------------------------------------------
# Rate limit storage — switch to in-memory so slowapi does not try to dial
# the production Redis (``redis://redis:6379``) during route tests that hit
# decorated endpoints via TestClient.
# ---------------------------------------------------------------------------
os.environ.setdefault("REDIS_URL", "memory://")

# ---------------------------------------------------------------------------
# Auth — provide a known static API key so tests that hit AuthMiddleware via
# ``create_app`` can pass ``X-API-Key: test-api-key`` to authenticate.
# ---------------------------------------------------------------------------
os.environ.setdefault("ZUBOT_INGESTION_API_KEY", "test-api-key")
os.environ.setdefault("WOD_JWT_SECRET", "test-jwt-secret")

# ---------------------------------------------------------------------------
# OpenTelemetry — clear the OTLP endpoint so the batch span exporter does not
# launch a background thread trying to reach ``phoenix:4317`` (the production
# Arize Phoenix collector). ``setup_otel`` treats an empty string the same as
# ``None`` and installs a noop provider instead of the OTLP one.
# ---------------------------------------------------------------------------
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "")
