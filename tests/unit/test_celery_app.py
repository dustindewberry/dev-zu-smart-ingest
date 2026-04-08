"""Unit tests for ``zubot_ingestion.services.celery_app``.

Exercises the Celery app wiring in isolation: broker/backend URL assembly,
the full ``app.conf`` contract from the blueprint, and the placeholder
``extract_document_task`` registration + NotImplementedError body.
"""

from __future__ import annotations

import pytest
from celery import Celery

from zubot_ingestion.services import celery_app as celery_mod
from zubot_ingestion.shared.constants import (
    CELERY_BROKER_DB,
    CELERY_QUEUE_DEFAULT,
    CELERY_RESULT_DB,
    CELERY_TASK_NAME_EXTRACTION,
)


# ---------------------------------------------------------------------------
# Broker/backend URL assembly
# ---------------------------------------------------------------------------


def test_build_redis_url_appends_db_index() -> None:
    assert celery_mod._build_redis_url("redis://redis:6379", 2) == "redis://redis:6379/2"


def test_build_redis_url_strips_trailing_slash() -> None:
    assert celery_mod._build_redis_url("redis://redis:6379/", 3) == "redis://redis:6379/3"


def test_broker_url_uses_broker_db() -> None:
    assert celery_mod.BROKER_URL.endswith(f"/{CELERY_BROKER_DB}")


def test_result_backend_url_uses_result_db() -> None:
    assert celery_mod.RESULT_BACKEND_URL.endswith(f"/{CELERY_RESULT_DB}")


def test_broker_and_backend_use_different_dbs() -> None:
    assert CELERY_BROKER_DB != CELERY_RESULT_DB
    assert celery_mod.BROKER_URL != celery_mod.RESULT_BACKEND_URL


# ---------------------------------------------------------------------------
# Celery app object
# ---------------------------------------------------------------------------


def test_app_is_celery_instance() -> None:
    assert isinstance(celery_mod.app, Celery)


def test_app_main_name() -> None:
    assert celery_mod.app.main == "zubot_ingestion"


def test_app_broker_url_matches_module_constant() -> None:
    # Celery normalizes the URL; compare the DB index suffix instead.
    assert celery_mod.app.conf.broker_url.endswith(f"/{CELERY_BROKER_DB}")


def test_app_result_backend_matches_module_constant() -> None:
    assert celery_mod.app.conf.result_backend.endswith(f"/{CELERY_RESULT_DB}")


# ---------------------------------------------------------------------------
# Celery configuration contract (blueprint §step-7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ("task_serializer", "json"),
        ("result_serializer", "json"),
        ("task_default_queue", CELERY_QUEUE_DEFAULT),
        ("worker_prefetch_multiplier", 1),
        ("task_acks_late", True),
        ("task_reject_on_worker_lost", True),
        ("broker_connection_retry_on_startup", True),
        ("task_default_retry_delay", 2),
        ("task_max_retries", 3),
    ],
)
def test_app_configuration_values(key: str, expected: object) -> None:
    assert celery_mod.app.conf[key] == expected


def test_accept_content_is_json_only() -> None:
    assert list(celery_mod.app.conf.accept_content) == ["json"]


# ---------------------------------------------------------------------------
# Task registration
# ---------------------------------------------------------------------------


def test_extract_document_task_registered_under_canonical_name() -> None:
    assert CELERY_TASK_NAME_EXTRACTION in celery_mod.app.tasks


def test_extract_document_task_name_matches_constant() -> None:
    assert celery_mod.extract_document_task.name == CELERY_TASK_NAME_EXTRACTION


def test_extract_document_task_has_callable_body() -> None:
    """The step-7 contract required this task to raise NotImplementedError
    with 'step-16' in the message as a landing-point marker. Step-16 has
    since landed and replaced the placeholder with the real pipeline
    execution body, so the NotImplementedError sentinel is gone. This
    smoke test simply confirms the task is still registered with a
    callable ``run`` method (the real execution path requires a live
    postgres and is covered by integration tests elsewhere).
    """
    task = celery_mod.extract_document_task
    assert callable(task.run)
    assert task.name == CELERY_TASK_NAME_EXTRACTION


def test_extract_document_task_retry_policy() -> None:
    task = celery_mod.extract_document_task
    # max_retries is set on the task class itself via the decorator kwargs.
    assert task.max_retries == 3
    assert task.autoretry_for == (Exception,)
    assert task.retry_backoff is True
    assert task.retry_backoff_max == 8
    assert task.retry_jitter is True
