"""Programmatic entrypoint: ``python -m zubot_ingestion``.

Resolves runtime configuration from :func:`zubot_ingestion.config.get_settings`
and launches the FastAPI application returned by
:func:`zubot_ingestion.api.app.create_app` under uvicorn.
"""

from __future__ import annotations

import uvicorn

from zubot_ingestion.config import get_settings
from zubot_ingestion.shared.constants import SERVICE_NAME, SERVICE_VERSION


def main() -> None:
    """Run the API server using settings from the environment."""
    settings = get_settings()
    uvicorn.run(
        "zubot_ingestion.api.app:create_app",
        factory=True,
        host=settings.ZUBOT_HOST,
        port=settings.ZUBOT_PORT,
        log_level=settings.LOG_LEVEL.lower(),
        # Reload is intentionally disabled — dev reload is the developer's
        # responsibility (e.g. via ``uvicorn ... --reload``).
        reload=False,
        access_log=True,
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    # Touch the constants so static checkers see SERVICE_NAME / SERVICE_VERSION
    # are intentionally re-exported here for ops tooling that grep this file.
    _ = (SERVICE_NAME, SERVICE_VERSION)
    main()
