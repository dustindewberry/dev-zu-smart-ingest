"""Tests for Celery worker concurrency / prefetch wiring.

These tests assert that the two new ``Settings`` fields
``CELERY_WORKER_CONCURRENCY`` and ``CELERY_WORKER_PREFETCH_MULTIPLIER``
are honoured by :mod:`zubot_ingestion.services.celery_app`:

* With default settings, the Celery app exposes
  ``worker_concurrency=2`` and ``worker_prefetch_multiplier=1`` on its
  ``app.conf`` (preserving the behaviour that previously lived in
  ``docker-compose.yml`` as a hardcoded ``--concurrency=2`` CLI flag).
* When the env vars are overridden and the settings cache is cleared,
  a fresh import of :mod:`celery_app` picks up the new values and
  applies them to the same ``app.conf`` keys.

Module-reload strategy: :mod:`celery_app` reads ``get_settings()`` at
import time and calls ``app.conf.update(...)`` as a side-effect, so
the override test uses :func:`importlib.reload` after swapping
``os.environ`` and clearing the settings LRU cache.
"""

from __future__ import annotations

import importlib
import os

import pytest

from zubot_ingestion import config
from zubot_ingestion.services import celery_app as celery_app_module


def test_default_settings_produce_expected_celery_conf() -> None:
    """With default Settings, app.conf carries 2 / 1."""
    settings = config.Settings()  # type: ignore[call-arg]
    assert settings.CELERY_WORKER_CONCURRENCY == 2
    assert settings.CELERY_WORKER_PREFETCH_MULTIPLIER == 1

    assert celery_app_module.app.conf.worker_concurrency == 2
    assert celery_app_module.app.conf.worker_prefetch_multiplier == 1


def test_env_override_changes_celery_conf_on_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overriding env vars and reloading celery_app updates app.conf."""
    monkeypatch.setenv("CELERY_WORKER_CONCURRENCY", "8")
    monkeypatch.setenv("CELERY_WORKER_PREFETCH_MULTIPLIER", "4")

    # Clear the Settings LRU cache so the next get_settings() call
    # re-reads os.environ instead of returning the cached instance
    # constructed at collection time.
    config.get_settings.cache_clear()
    try:
        reloaded = importlib.reload(celery_app_module)
        assert reloaded.app.conf.worker_concurrency == 8
        assert reloaded.app.conf.worker_prefetch_multiplier == 4
    finally:
        # Restore the default-backed Settings + app.conf for any
        # downstream tests that import celery_app_module by name.
        monkeypatch.delenv("CELERY_WORKER_CONCURRENCY", raising=False)
        monkeypatch.delenv("CELERY_WORKER_PREFETCH_MULTIPLIER", raising=False)
        config.get_settings.cache_clear()
        importlib.reload(celery_app_module)


def test_preserves_existing_app_conf_keys() -> None:
    """Adding concurrency/prefetch must not clobber existing conf."""
    conf = celery_app_module.app.conf
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.broker_connection_retry_on_startup is True
    assert conf.task_default_retry_delay == 2
    assert conf.task_max_retries == 3
    assert conf.task_serializer == "json"
    assert conf.result_serializer == "json"
