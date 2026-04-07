"""Unit tests for ``zubot_ingestion.services.task_queue``.

Exercises :class:`CeleryTaskQueue` without contacting a real broker by
monkeypatching the module-level ``extract_document_task.delay`` and the
``AsyncResult`` class that ``get_task_status`` constructs.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from zubot_ingestion.services import task_queue as tq_mod
from zubot_ingestion.services.task_queue import CeleryTaskQueue
from zubot_ingestion.shared.types import TaskStatus


# ---------------------------------------------------------------------------
# enqueue_extraction
# ---------------------------------------------------------------------------


def test_enqueue_extraction_returns_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_delay(job_id_str: str) -> SimpleNamespace:
        captured["job_id_str"] = job_id_str
        return SimpleNamespace(id="celery-task-42")

    monkeypatch.setattr(tq_mod.extract_document_task, "delay", fake_delay)

    queue = CeleryTaskQueue()
    job_id = uuid4()
    returned = queue.enqueue_extraction(job_id)

    assert returned == "celery-task-42"
    assert captured["job_id_str"] == str(job_id)


def test_enqueue_extraction_coerces_uuid_to_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UUID must be converted to a str before crossing the Celery wire.

    This is important because Celery's JSON serializer cannot serialize
    :class:`UUID` natively — if the service layer forgot to coerce, tasks
    would crash at broker publish time rather than here in a unit test.
    """
    seen: list[object] = []

    def fake_delay(arg: object) -> SimpleNamespace:
        seen.append(arg)
        return SimpleNamespace(id="tid")

    monkeypatch.setattr(tq_mod.extract_document_task, "delay", fake_delay)

    known = UUID("12345678-1234-5678-1234-567812345678")
    CeleryTaskQueue().enqueue_extraction(known)

    assert len(seen) == 1
    assert isinstance(seen[0], str)
    assert seen[0] == "12345678-1234-5678-1234-567812345678"


# ---------------------------------------------------------------------------
# get_task_status — helpers
# ---------------------------------------------------------------------------


class _FakeAsyncResult:
    """Drop-in replacement for :class:`celery.result.AsyncResult`.

    Only implements the attributes accessed by :meth:`CeleryTaskQueue.get_task_status`.
    """

    def __init__(
        self,
        state: str,
        result: object = None,
        traceback: object = None,
    ) -> None:
        self.state = state
        self.result = result
        self.traceback = traceback


def _patch_async_result(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeAsyncResult
) -> None:
    """Make ``tq_mod.AsyncResult(task_id, app=app)`` return ``fake``."""

    def _factory(task_id: str, app: object | None = None) -> _FakeAsyncResult:
        return fake

    monkeypatch.setattr(tq_mod, "AsyncResult", _factory)


# ---------------------------------------------------------------------------
# get_task_status — state mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "celery_state",
    ["PENDING", "STARTED", "RETRY"],
)
def test_get_task_status_passthrough_states(
    monkeypatch: pytest.MonkeyPatch, celery_state: str
) -> None:
    _patch_async_result(monkeypatch, _FakeAsyncResult(state=celery_state))
    status = CeleryTaskQueue().get_task_status("tid-1")

    assert isinstance(status, TaskStatus)
    assert status.state == celery_state
    assert status.result is None
    assert status.traceback is None


def test_get_task_status_success_carries_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"job_id": "abc", "confidence": 0.92}
    _patch_async_result(
        monkeypatch, _FakeAsyncResult(state="SUCCESS", result=payload)
    )

    status = CeleryTaskQueue().get_task_status("tid-2")

    assert status.state == "SUCCESS"
    assert status.result == payload
    assert status.traceback is None


def test_get_task_status_failure_carries_traceback_from_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exc = RuntimeError("boom")
    try:
        raise exc
    except RuntimeError:
        captured = exc  # now has a real __traceback__

    _patch_async_result(
        monkeypatch, _FakeAsyncResult(state="FAILURE", result=captured)
    )

    status = CeleryTaskQueue().get_task_status("tid-3")

    assert status.state == "FAILURE"
    assert status.result is None
    assert status.traceback is not None
    assert "RuntimeError" in status.traceback
    assert "boom" in status.traceback


def test_get_task_status_failure_uses_string_traceback_when_no_exc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_async_result(
        monkeypatch,
        _FakeAsyncResult(
            state="FAILURE",
            result="non-exception payload",
            traceback="Traceback: something bad happened",
        ),
    )

    status = CeleryTaskQueue().get_task_status("tid-4")

    assert status.state == "FAILURE"
    assert status.result is None
    assert status.traceback == "Traceback: something bad happened"


def test_get_task_status_failure_fallback_when_only_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_async_result(
        monkeypatch,
        _FakeAsyncResult(state="FAILURE", result="something bad", traceback=None),
    )

    status = CeleryTaskQueue().get_task_status("tid-5")

    assert status.state == "FAILURE"
    # repr() of the string payload, since it wasn't an exception.
    assert status.traceback == repr("something bad")


def test_get_task_status_unknown_state_maps_to_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_async_result(monkeypatch, _FakeAsyncResult(state="REVOKED"))
    status = CeleryTaskQueue().get_task_status("tid-6")

    assert status.state == "PENDING"
    assert status.result is None
    assert status.traceback is None


def test_get_task_status_success_swallows_result_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the result backend blows up while deserializing, we return None."""

    class BoomResult(_FakeAsyncResult):
        @property  # type: ignore[override]
        def result(self) -> object:  # noqa: D401
            raise RuntimeError("backend exploded")

        @result.setter
        def result(self, _value: object) -> None:  # pragma: no cover - setter unused
            pass

    fake = BoomResult(state="SUCCESS")
    _patch_async_result(monkeypatch, fake)

    status = CeleryTaskQueue().get_task_status("tid-7")

    assert status.state == "SUCCESS"
    assert status.result is None


# ---------------------------------------------------------------------------
# Structural protocol conformance
# ---------------------------------------------------------------------------


def test_celery_task_queue_exposes_required_methods() -> None:
    queue = CeleryTaskQueue()
    assert callable(queue.enqueue_extraction)
    assert callable(queue.get_task_status)
