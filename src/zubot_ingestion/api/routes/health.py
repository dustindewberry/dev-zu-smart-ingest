"""Health route metric instrumentation hooks (CAP-028 instrumentation slice).

This file is a MINIMAL STUB owned by the witty-atlas worktree (task-23 /
step-23 prometheus-metrics). The canonical full /health endpoint that
probes Postgres / Redis / Ollama / ChromaDB / Elasticsearch concurrently
is owned by task-13 (keen-hare worktree). On merge, the canonical
task-13 health route MUST take precedence over this stub. The ONLY
behavioural contribution this file makes is the
``update_queue_depth_gauge`` helper below — it MUST be ported into the
canonical health route at the location indicated by the docstring.

Single integration point in the canonical health route:

After the canonical ``_probe_celery`` (or whichever helper aggregates
``inspect.active()`` and ``inspect.reserved()`` into the
``workers.queue_depth`` integer in the response body), call
``update_queue_depth_gauge(workers['queue_depth'])`` so the
``zubot_queue_depth`` Prometheus gauge is refreshed every time
``/health`` is scraped.

Why update the gauge from the health endpoint and not from a Celery
signal? Two reasons:

1. The canonical health probe already pays the round-trip cost to
   query Celery's inspect RPC, so updating the gauge piggybacks on
   that work — no additional Celery traffic is needed.

2. Prometheus scrapes are cheap and frequent (typically every 15 s),
   which is the right cadence to keep ``zubot_queue_depth`` fresh.
   Updating the gauge on every Celery task enqueue would be wasteful
   and would require the ITaskQueue layer to take a dependency on the
   metrics layer.

The router defined here exists ONLY so the unit tests can import the
module without crashing. The handler returns a stub 200 response. The
canonical task-13 handler will replace it on merge.
"""

from __future__ import annotations

from fastapi import APIRouter

from zubot_ingestion.infrastructure.metrics.prometheus import queue_depth

__all__ = ["router", "update_queue_depth_gauge"]

router = APIRouter()


def update_queue_depth_gauge(current_celery_queue_depth: int) -> None:
    """Set the ``zubot_queue_depth`` gauge to the current Celery queue depth.

    Args:
        current_celery_queue_depth: The total number of tasks currently
            being processed or waiting in any worker's local queue,
            as reported by ``celery_app.control.inspect().active()`` +
            ``.reserved()``. Must be a non-negative integer.
    """
    queue_depth.set(max(0, int(current_celery_queue_depth)))


@router.get("/health")
async def health_endpoint() -> dict[str, object]:
    """Stub /health handler — canonical implementation lives in task-13.

    The stub always returns ``status='healthy'`` and refreshes the
    ``zubot_queue_depth`` gauge to ``0``. The canonical task-13 handler
    will replace this with a real concurrent dependency probe and the
    real Celery inspect call. The ``update_queue_depth_gauge`` line
    below shows the required call site for the metric integration that
    must be ported on merge.
    """
    update_queue_depth_gauge(0)
    return {
        "status": "healthy",
        "workers": {"active": 0, "queue_depth": 0},
    }
