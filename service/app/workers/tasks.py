"""Core Celery tasks for video generation."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.models import TaskStatus, VideoTask
from app.storage.local import LocalVideoStorage
from app.workers.celery_app import celery_app
from app.workers.parser import OutputNotFoundError, locate_output_file, probe_mp4
from app.workers.runner import run_oh

logger = logging.getLogger(__name__)

# Sync DB engine for Celery workers (they can't use async)
_sync_engine = None


def _get_sync_engine():
    global _sync_engine
    if _sync_engine is None:
        _sync_engine = create_engine(settings.db_sync_url, pool_size=5, max_overflow=10)
    return _sync_engine


def _sync_session() -> Session:
    engine = _get_sync_engine()
    return Session(engine)


def _append_log(task_id: str, line: str) -> None:
    """Append a log line to the task's log tail (in Redis list + update DB periodically)."""
    try:
        import redis

        r = redis.from_url(settings.broker_url)
        r.lpush(f"oh:logs:{task_id}", line)
        # Keep only last N entries
        r.ltrim(f"oh:logs:{task_id}", 0, 9999)
        r.publish(f"oh:channel:{task_id}", line)
        r.close()
    except Exception:
        logger.warning("Failed to push log line to Redis for task %s", task_id)


def _mark_succeeded(
    task_id: str,
    storage_key: str,
    meta,  # VideoMeta
    result,  # RunResult
) -> None:
    with _sync_session() as db:
        task = db.get(VideoTask, task_id)
        if task is None:
            return
        task.status = TaskStatus.SUCCEEDED
        task.output_path = storage_key
        task.file_size_bytes = meta.file_size_bytes
        task.duration_seconds = meta.duration_seconds
        task.resolution = meta.resolution
        task.fps = meta.fps
        task.exit_code = result.exit_code
        task.finished_at = datetime.now(timezone.utc)
        db.commit()


def _mark_failed(task_id: str, exc: Exception, exit_code: int | None = None) -> None:
    with _sync_session() as db:
        task = db.get(VideoTask, task_id)
        if task is None:
            return
        task.status = TaskStatus.FAILED
        task.error_message = str(exc)[:4000]
        if exit_code is not None:
            task.exit_code = exit_code
        task.finished_at = datetime.now(timezone.utc)
        db.commit()


def _update_log_tail(task_id: str) -> None:
    """Read log from Redis and write the tail to DB."""
    try:
        import redis

        r = redis.from_url(settings.broker_url)
        lines = r.lrange(f"oh:logs:{task_id}", 0, -1)
        r.close()
        raw = b"".join(lines).decode("utf-8", errors="replace")
        tail = raw[-settings.log_tail_bytes :]
        with _sync_session() as db:
            task = db.get(VideoTask, task_id)
            if task is not None:
                task.log_tail = tail
                db.commit()
    except Exception:
        logger.warning("Failed to update log tail for task %s", task_id)


class TransientError(Exception):
    """Errors that should trigger automatic retry."""


@celery_app.task(
    bind=True,
    name="generate_video",
    acks_late=True,
    autoretry_for=(TransientError,),
    retry_backoff=True,
    max_retries=2,
)
def generate_video_task(self, task_id: str) -> None:
    """Celery task: run oh CLI to generate a video and persist results."""
    storage = LocalVideoStorage()

    with _sync_session() as db:
        task = db.get(VideoTask, task_id)
        if task is None:
            logger.error("Task %s not found in DB", task_id)
            return
        if task.status == TaskStatus.CANCELED:
            return

        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now(timezone.utc)
        task.celery_task_id = self.request.id
        db.commit()

        prompt = task.prompt
        timeout = task.timeout_seconds
        extra_oh_args = json.loads(task.extra_oh_args) if task.extra_oh_args else []

    workspace = Path(settings.workspace_root) / task_id
    workspace.mkdir(parents=True, exist_ok=True)

    with _sync_session() as db:
        task = db.get(VideoTask, task_id)
        if task is not None:
            task.workspace_path = str(workspace)
            db.commit()

    try:
        result = run_oh(
            prompt=prompt,
            cwd=workspace,
            timeout=timeout,
            on_log_line=lambda line: _append_log(task_id, line),
            extra_args=extra_oh_args,
            oh_bin=settings.oh_bin,
            headless_shell_path=settings.headless_shell_path,
        )

        _update_log_tail(task_id)

        if result.exit_code != 0:
            _mark_failed(
                task_id,
                RuntimeError(f"oh exited with code {result.exit_code}"),
                exit_code=result.exit_code,
            )
            return

        mp4 = locate_output_file(result.stdout, workspace)
        meta = probe_mp4(mp4)
        final_key = storage.save(task_id, mp4)
        _mark_succeeded(task_id, final_key, meta, result)

        # Publish done event
        try:
            import redis

            r = redis.from_url(settings.broker_url)
            r.publish(f"oh:channel:{task_id}", "__DONE__")
            r.close()
        except Exception:
            pass

    except OutputNotFoundError as exc:
        _update_log_tail(task_id)
        _mark_failed(task_id, exc)
    except Exception as exc:
        _update_log_tail(task_id)
        _mark_failed(task_id, exc)
        raise


@celery_app.task(name="cleanup_expired_tasks")
def cleanup_expired_tasks() -> None:
    """Remove workspace dirs and log entries for tasks older than retention period."""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.cleanup_retention_days)
    with _sync_session() as db:
        expired = db.query(VideoTask).filter(
            VideoTask.created_at < cutoff,
            VideoTask.status.in_([
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELED,
            ]),
        ).all()

        storage = LocalVideoStorage()
        for task in expired:
            # Clean up workspace
            if task.workspace_path:
                wp = Path(task.workspace_path)
                if wp.exists():
                    import shutil
                    shutil.rmtree(wp, ignore_errors=True)

            # Clean up stored video
            if task.output_path:
                storage.delete(task.output_path)

            # Clean up Redis logs
            try:
                import redis

                r = redis.from_url(settings.broker_url)
                r.delete(f"oh:logs:{str(task.id)}")
                r.close()
            except Exception:
                pass

        logger.info("Cleaned up %d expired tasks", len(expired))
