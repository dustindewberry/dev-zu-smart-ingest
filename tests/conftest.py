"""Top-level pytest conftest.

Sets hermetic environment variables BEFORE any test module is collected so
module-level singletons (slowapi Limiter, setup_logging, OTEL TracerProvider)
use safe values on the very first import. Without this conftest, tests that
import api.app or api.middleware.rate_limit at module scope would try to
connect to a real Redis at ``redis:6379`` or write logs to ``/var/log``.
"""

from __future__ import annotations

import os
import tempfile

# LOG_DIR — setup_logging() calls os.makedirs(log_dir, exist_ok=True), which
# explodes with PermissionError on /var/log/zubot-ingestion (the production
# default baked into config.Settings). Point it at a per-session tmpdir.
_LOG_TMP = tempfile.mkdtemp(prefix="zubot-ingestion-test-logs-")
os.environ.setdefault("LOG_DIR", _LOG_TMP)

# REDIS_URL — slowapi's Limiter is constructed at module import time with
# a storage backend built from settings.REDIS_URL. In-memory storage avoids
# the connection attempt during test collection.
os.environ.setdefault("REDIS_URL", "memory://")

# API key — AuthMiddleware raises on import-time settings access if the env
# var is missing. A stable known value keeps auth-dependent tests hermetic.
os.environ.setdefault("ZUBOT_INGESTION_API_KEY", "test-api-key")

# OTEL endpoint — leave empty so setup_otel() installs a noop provider
# instead of attempting a gRPC connection to a collector.
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "")
