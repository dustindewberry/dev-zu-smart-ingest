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
    # Ollama HTTP connection pool + retry budget (perf tuning)           #
    # ------------------------------------------------------------------ #
    # Bounded httpx.AsyncClient pool limits shared across every Ollama
    # request the OllamaClient instance issues. Defaults match the
    # pre-refactor effective behaviour (one-shot client per call) while
    # allowing operators to scale up via environment variables when new
    # hardware is added.
    OLLAMA_HTTP_POOL_MAX_CONNECTIONS: int = 10
    OLLAMA_HTTP_POOL_MAX_KEEPALIVE: int = 5
    OLLAMA_HTTP_TIMEOUT_SECONDS: float = 120.0

    # Retry budget for transient Ollama failures (503, 429, transport
    # errors). ``OLLAMA_RETRY_MAX_ATTEMPTS`` is the TOTAL attempt count
    # (1 initial + N-1 retries). Backoff is exponential:
    # delay(i) = INITIAL_BACKOFF * (MULTIPLIER ** i)
    # Default (3 attempts, 1s initial, x2 multiplier) preserves the
    # pre-refactor 1s / 2s / 4s schedule.
    OLLAMA_RETRY_MAX_ATTEMPTS: int = 3
    OLLAMA_RETRY_INITIAL_BACKOFF_SECONDS: float = 1.0
    OLLAMA_RETRY_BACKOFF_MULTIPLIER: float = 2.0

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        """Lowercase alias for ``DATABASE_URL`` used by the database layer."""
        return self.DATABASE_URL


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process-wide :class:`Settings` singleton.

    The first call constructs ``Settings()``, which reads environment
    variables and the ``.env`` file. Subsequent calls return the same
    instance. Tests can clear the cache via ``get_settings.cache_clear()``.
    """
    return Settings()  # type: ignore[call-arg]
