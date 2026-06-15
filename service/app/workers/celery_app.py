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

celery_app.autodiscover_modules(["app.workers.tasks"])
