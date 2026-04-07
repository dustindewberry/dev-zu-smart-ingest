"""Transitional stub of the Celery application.

NOTE: This is a MINIMAL stub exposing only the ``app`` attribute the
/health endpoint imports for ``celery_app.control.inspect()``. The
canonical full Celery configuration (broker URL, result backend, full
``app.conf`` contract, retry policy, ``extract_document_task``
placeholder) is produced by task-7 (nimble-badger / sharp-spark) and
must overwrite this stub on merge. The canonical broker/result-backend
URLs are derived from ``REDIS_URL`` + ``CELERY_BROKER_DB`` /
``CELERY_RESULT_DB`` so the runtime ``app.control`` interface is the
same — only the import-time wiring differs.
"""

from __future__ import annotations

from celery import Celery

from zubot_ingestion.config import get_settings

_settings = get_settings()
_base = _settings.REDIS_URL.rstrip("/")

# Match canonical broker/result DB selection (CELERY_BROKER_DB=2,
# CELERY_RESULT_DB=3) so the inspect-against-real-cluster path behaves
# identically to a merged build.
app = Celery(
    "zubot_ingestion",
    broker=f"{_base}/2",
    backend=f"{_base}/3",
)

__all__ = ["app"]
