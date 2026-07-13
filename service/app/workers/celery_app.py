"""Celery application configuration."""

from celery import Celery

from app.config import settings

celery_app = Celery(
    "oh-worker",
    broker=settings.broker_url,
    backend=settings.broker_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    # Phase 7: support priority-tiered queues (high/normal/low). For the Redis
    # broker, routing is driven by the queue set at enqueue time (scheduler);
    # this cap is honored by AMQP brokers and documents intent for Redis.
    task_queue_max_priority=10,
)

# Periodic schedule. cleanup_expired_tasks is idempotent (deleting an already
# deleted path is a no-op), so running it from more than one beat replica is
# harmless. For a single authoritative scheduler, prefer redbeat or run beat on
# one designated replica.
celery_app.conf.beat_schedule = {
    "cleanup-expired-tasks": {
        "task": "cleanup_expired_tasks",
        "schedule": 86400.0,  # daily
    },
    # Lost-task reclaim (scale-multi-instance R8/R9). Idempotent via a row-lock
    # UPDATE, so running it from a single beat replica is sufficient; should
    # multiple beats run it, double-reclaim/double re-enqueue cannot happen.
    "recover-lost-tasks": {
        "task": "recover_lost_tasks",
        "schedule": 30.0,  # every 30s
    },
}

# Phase 7: default queue tier for ``generate_video``. The scheduler sets an
# explicit queue (high/normal/low) from the task's ``priority`` column; this
# route is the safety-net for any enqueue that omits a queue.
celery_app.conf.task_routes = {
    "generate_video": {"queue": "normal"},
    # Periodic (beat) tasks must land on a queue the workers actually consume,
    # otherwise they pile up on the default "celery" queue and never run — this
    # silently disabled auto reclaim (R7-R9) and expired-task cleanup.
    "recover_lost_tasks": {"queue": "normal"},
    "cleanup_expired_tasks": {"queue": "normal"},
}

celery_app.autodiscover_tasks(["app.workers.tasks"])

# Register the liveness (heartbeat) signal handlers and the periodic reclaim
# task. Importing the module wires up worker_process_init (per-replica
# registration + heartbeat refresh) and the recover_lost_tasks celery task.
from app.workers import beat  # noqa: E402,F401
