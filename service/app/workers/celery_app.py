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
}

celery_app.autodiscover_tasks(["app.workers.tasks"])
