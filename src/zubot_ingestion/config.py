"""Application configuration via Pydantic Settings.

Implements CAP-001 (configuration management). All runtime configuration is
read from environment variables (or a ``.env`` file in development) into a
single typed ``Settings`` instance, which is then exposed as a process-wide
cached singleton via :func:`get_settings`.

Layering: this module belongs to the application layer (it knows nothing
about web frameworks, databases, queues, etc.) and may be imported from any
other layer of the service.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from zubot_ingestion.shared.constants import (
    PERF_CELERY_WORKER_CONCURRENCY,
    PERF_CELERY_WORKER_PREFETCH_MULTIPLIER,
    PERF_COMPANION_SKIP_ENABLED,
    PERF_COMPANION_SKIP_MIN_WORDS,
    PERF_OLLAMA_HTTP_POOL_MAX_CONNECTIONS,
    PERF_OLLAMA_HTTP_POOL_MAX_KEEPALIVE,
    PERF_OLLAMA_HTTP_TIMEOUT_SECONDS,
    PERF_OLLAMA_KEEP_ALIVE,
    PERF_OLLAMA_NUM_PARALLEL,
    PERF_OLLAMA_RETRY_BACKOFF_MULTIPLIER,
    PERF_OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS,
    PERF_OLLAMA_RETRY_MAX_ATTEMPTS,
    PERF_OLLAMA_TEXT_MODEL,
    PERF_OLLAMA_VISION_MODEL,
)


class Settings(BaseSettings):
    """Strongly-typed runtime configuration for the Zubot Ingestion Service.

    Field names are case-sensitive and map 1:1 to environment variables of
    the same name. Defaults are tuned for the docker-compose environment
    described in the blueprint; the two secrets (``ZUBOT_INGESTION_API_KEY``
    and ``WOD_JWT_SECRET``) intentionally have no defaults so that startup
    fails fast in production if they are not provided.
    """

    # ------------------------------------------------------------------ #
    # HTTP server                                                        #
    # ------------------------------------------------------------------ #
    ZUBOT_HOST: str = "0.0.0.0"
    ZUBOT_PORT: int = 4243

    # ------------------------------------------------------------------ #
    # Datastores / external services                                     #
    # ------------------------------------------------------------------ #
    DATABASE_URL: str = (
        "postgresql+asyncpg://zubot:zubot@localhost:5432/zubot_ingestion"
    )
    REDIS_URL: str = "redis://redis:6379"
    OLLAMA_HOST: str = "http://ollama:11434"
    CHROMADB_HOST: str = "chromadb"
    CHROMADB_PORT: int = 8000

    # ------------------------------------------------------------------ #
    # Elasticsearch (CAP-024) — optional, no-op when ELASTICSEARCH_URL    #
    # is unset so local dev and CI still run without an ES instance.     #
    # ------------------------------------------------------------------ #
    ELASTICSEARCH_URL: str | None = None
    ELASTICSEARCH_USERNAME: str | None = None
    ELASTICSEARCH_PASSWORD: str | None = None
    ELASTICSEARCH_TIMEOUT: float = 10.0
    ELASTICSEARCH_VERIFY_CERTS: bool = True

    # ------------------------------------------------------------------ #
    # Webhook callback delivery (CAP-025) — optional, gated by            #
    # CALLBACK_ENABLED. When False the composition root wires a          #
    # NoOpCallbackClient so local dev / CI run without a live receiver.  #
    # When True, the real CallbackHttpClient is constructed and the     #
    # signing secret (if non-empty) is used to produce an                #
    # X-Zubot-Signature HMAC-SHA256 header on every delivery.            #
    # ------------------------------------------------------------------ #
    CALLBACK_ENABLED: bool = False
    CALLBACK_SIGNING_SECRET: str | None = None

    # ------------------------------------------------------------------ #
    # Observability                                                      #
    # ------------------------------------------------------------------ #
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://phoenix:4317"

    # ------------------------------------------------------------------ #
    # Auth secrets (no defaults — must be provided via env / .env)       #
    # ------------------------------------------------------------------ #
    ZUBOT_INGESTION_API_KEY: str = ""
    WOD_JWT_SECRET: str = ""

    # ------------------------------------------------------------------ #
    # Logging                                                            #
    # ------------------------------------------------------------------ #
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "/var/log/zubot-ingestion"

    # ------------------------------------------------------------------ #
    # Test fixtures                                                      #
    # ------------------------------------------------------------------ #
    TEST_PDF_DIR: str | None = None  # absolute path used by integration tests

    # ------------------------------------------------------------------ #
    # Rate limiting (CAP-030)                                            #
    # ------------------------------------------------------------------ #
    # Global default applied to every endpoint that does not declare its
    # own ``@limiter.limit(...)`` decorator. Per-endpoint limits override
    # this value (e.g. POST /extract uses 20/minute).
    RATE_LIMIT_DEFAULT: str = "100/minute"

    # ------------------------------------------------------------------ #
    # Performance tuning knobs                                           #
    # ------------------------------------------------------------------ #
    # Every field below is a scale-up lever for the single-box appliance
    # deployment. Defaults come from ``shared.constants`` (PERF_* names)
    # and preserve current behavior so that this change is purely
    # additive. Operators enable the aggressive settings by overriding
    # the environment variables in their deployment's .env file.
    #
    # These knobs are consumed (in downstream tasks) by:
    #   * infrastructure/ollama/client.py — num_parallel, keep_alive,
    #     HTTP pool sizing, timeouts, retry budget, model selection
    #   * services/celery_app.py          — worker_concurrency,
    #     worker_prefetch_multiplier
    #   * services/orchestrator.py        — companion-skip heuristic
    #
    # IMPORTANT: this task only ADDS the fields. The actual wiring into
    # the Ollama client, Celery app, and orchestrator is deferred to
    # downstream tasks that can re-bench after each change.

    # Ollama runtime hints forwarded as ``options`` on /api/chat.
    OLLAMA_NUM_PARALLEL: int = PERF_OLLAMA_NUM_PARALLEL
    OLLAMA_KEEP_ALIVE: str = PERF_OLLAMA_KEEP_ALIVE

    # Ollama HTTP transport — httpx.AsyncClient connection pool sizing.
    OLLAMA_HTTP_POOL_MAX_CONNECTIONS: int = PERF_OLLAMA_HTTP_POOL_MAX_CONNECTIONS
    OLLAMA_HTTP_POOL_MAX_KEEPALIVE: int = PERF_OLLAMA_HTTP_POOL_MAX_KEEPALIVE
    OLLAMA_HTTP_TIMEOUT_SECONDS: float = PERF_OLLAMA_HTTP_TIMEOUT_SECONDS

    # Ollama retry budget — preserves the current 3 attempts / 1s / 2s /
    # 4s exponential-backoff behavior.
    OLLAMA_RETRY_MAX_ATTEMPTS: int = PERF_OLLAMA_RETRY_MAX_ATTEMPTS
    OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS: float = (
        PERF_OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS
    )
    OLLAMA_RETRY_BACKOFF_MULTIPLIER: float = PERF_OLLAMA_RETRY_BACKOFF_MULTIPLIER

    # Ollama model selection. OLLAMA_TEXT_MODEL defaults to 'qwen2.5:7b'
    # to preserve existing behavior; the appliance deployment overrides
    # this to 'qwen2.5:3b' via env var so the T4 VRAM budget can host
    # both models simultaneously with num_parallel=2.
    OLLAMA_VISION_MODEL: str = PERF_OLLAMA_VISION_MODEL
    OLLAMA_TEXT_MODEL: str = PERF_OLLAMA_TEXT_MODEL

    # Celery worker sizing. Matches the docker-compose defaults of
    # --concurrency=2 and worker_prefetch_multiplier=1.
    CELERY_WORKER_CONCURRENCY: int = PERF_CELERY_WORKER_CONCURRENCY
    CELERY_WORKER_PREFETCH_MULTIPLIER: int = PERF_CELERY_WORKER_PREFETCH_MULTIPLIER

    # Companion-skip heuristic. Default is DISABLED to preserve current
    # behavior; when enabled, Stage 2 skips companion generation for
    # pages whose extracted text already exceeds COMPANION_SKIP_MIN_WORDS.
    COMPANION_SKIP_ENABLED: bool = PERF_COMPANION_SKIP_ENABLED
    COMPANION_SKIP_MIN_WORDS: int = PERF_COMPANION_SKIP_MIN_WORDS

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        """Lowercase alias for ``DATABASE_URL`` used by the database layer."""
        return self.DATABASE_URL

    # ------------------------------------------------------------------ #
    # Lowercase property aliases for the performance-tuning knobs.      #
    # These mirror the ``database_url`` pattern so downstream code can  #
    # access the fields via either the canonical UPPER_CASE name or a  #
    # lowercase alias without having to pick a convention.             #
    # ------------------------------------------------------------------ #

    @property
    def ollama_num_parallel(self) -> int:
        """Lowercase alias for :attr:`OLLAMA_NUM_PARALLEL`."""
        return self.OLLAMA_NUM_PARALLEL

    @property
    def ollama_keep_alive(self) -> str:
        """Lowercase alias for :attr:`OLLAMA_KEEP_ALIVE`."""
        return self.OLLAMA_KEEP_ALIVE

    @property
    def ollama_http_pool_max_connections(self) -> int:
        """Lowercase alias for :attr:`OLLAMA_HTTP_POOL_MAX_CONNECTIONS`."""
        return self.OLLAMA_HTTP_POOL_MAX_CONNECTIONS

    @property
    def ollama_http_pool_max_keepalive(self) -> int:
        """Lowercase alias for :attr:`OLLAMA_HTTP_POOL_MAX_KEEPALIVE`."""
        return self.OLLAMA_HTTP_POOL_MAX_KEEPALIVE

    @property
    def ollama_http_timeout_seconds(self) -> float:
        """Lowercase alias for :attr:`OLLAMA_HTTP_TIMEOUT_SECONDS`."""
        return self.OLLAMA_HTTP_TIMEOUT_SECONDS

    @property
    def ollama_retry_max_attempts(self) -> int:
        """Lowercase alias for :attr:`OLLAMA_RETRY_MAX_ATTEMPTS`."""
        return self.OLLAMA_RETRY_MAX_ATTEMPTS

    @property
    def ollama_retry_initial_backoff_seconds(self) -> float:
        """Lowercase alias for :attr:`OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS`."""
        return self.OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS

    @property
    def ollama_retry_backoff_multiplier(self) -> float:
        """Lowercase alias for :attr:`OLLAMA_RETRY_BACKOFF_MULTIPLIER`."""
        return self.OLLAMA_RETRY_BACKOFF_MULTIPLIER

    @property
    def ollama_vision_model(self) -> str:
        """Lowercase alias for :attr:`OLLAMA_VISION_MODEL`."""
        return self.OLLAMA_VISION_MODEL

    @property
    def ollama_text_model(self) -> str:
        """Lowercase alias for :attr:`OLLAMA_TEXT_MODEL`."""
        return self.OLLAMA_TEXT_MODEL

    @property
    def celery_worker_concurrency(self) -> int:
        """Lowercase alias for :attr:`CELERY_WORKER_CONCURRENCY`."""
        return self.CELERY_WORKER_CONCURRENCY

    @property
    def celery_worker_prefetch_multiplier(self) -> int:
        """Lowercase alias for :attr:`CELERY_WORKER_PREFETCH_MULTIPLIER`."""
        return self.CELERY_WORKER_PREFETCH_MULTIPLIER

    @property
    def companion_skip_enabled(self) -> bool:
        """Lowercase alias for :attr:`COMPANION_SKIP_ENABLED`."""
        return self.COMPANION_SKIP_ENABLED

    @property
    def companion_skip_min_words(self) -> int:
        """Lowercase alias for :attr:`COMPANION_SKIP_MIN_WORDS`."""
        return self.COMPANION_SKIP_MIN_WORDS


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process-wide :class:`Settings` singleton.

    The first call constructs ``Settings()``, which reads environment
    variables and the ``.env`` file. Subsequent calls return the same
    instance. Tests can clear the cache via ``get_settings.cache_clear()``.
    """
    return Settings()  # type: ignore[call-arg]
