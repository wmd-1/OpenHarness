"""Scheduler abstraction (scale-multi-instance Phase 6 / Phase 7).

Decouples task enqueue/cancel from the concrete backend so a future Temporal
migration is a drop-in. The default backend is Celery; Temporal is a placeholder
and is disabled by default (selected via ``OH_SCHEDULER_BACKEND=temporal``).

Enqueue routing also drives Phase 7 priority tiers: a task's numeric
``priority`` (1-10) is mapped to a named queue (high/normal/low) so workers can
drain higher-priority work first.
"""

from __future__ import annotations

from typing import Protocol

from app.config import settings
from app.workers.tasks import generate_video_task


# Priority tiers -> Celery queue names (design source §2). A task with
# ``priority >= PRIORITY_HIGH`` lands in "high"; ``>= PRIORITY_NORMAL`` in
# "normal"; anything lower in "low".
PRIORITY_HIGH = 7
PRIORITY_NORMAL = 4


def queue_for_priority(priority: int) -> str:
    """Map a numeric priority (1-10) to a named queue tier."""
    if priority >= PRIORITY_HIGH:
        return "high"
    if priority >= PRIORITY_NORMAL:
        return "normal"
    return "low"


class Scheduler(Protocol):
    """Enqueue/cancel contract shared by all backends."""

    backend: str

    def enqueue(self, task_id: str, *, priority: int = 5) -> str:
        """Enqueue a render and return the backend task id."""
        ...

    def cancel(self, celery_task_id: str) -> None:
        """Best-effort cancel of a previously enqueued backend task."""
        ...


class CeleryScheduler:
    """Default scheduler: routes work through the Celery broker."""

    backend = "celery"

    def enqueue(self, task_id: str, *, priority: int = 5) -> str:
        async_result = generate_video_task.apply_async(
            (task_id,),
            queue=queue_for_priority(priority),
        )
        return async_result.id

    def cancel(self, celery_task_id: str) -> None:
        # Durable, cross-replica cancellation is owned by the DELETE endpoint
        # (DB flag + Redis abort key). This is a best-effort broker revoke for
        # a task that has not yet started on a worker.
        generate_video_task.app.control.revoke(
            celery_task_id, terminate=True, signal="SIGTERM"
        )


class TemporalScheduler:
    """Placeholder for a future Temporal backend. Not wired by default."""

    backend = "temporal"

    def enqueue(self, task_id: str, *, priority: int = 5) -> str:
        raise NotImplementedError(
            "TemporalScheduler is a placeholder; set OH_SCHEDULER_BACKEND=celery (default)."
        )

    def cancel(self, celery_task_id: str) -> None:
        raise NotImplementedError("TemporalScheduler is a placeholder.")


def get_scheduler() -> Scheduler:
    """Return the configured scheduler (default: celery)."""
    backend = (settings.scheduler_backend or "celery").lower()
    if backend == "temporal":
        return TemporalScheduler()
    return CeleryScheduler()
