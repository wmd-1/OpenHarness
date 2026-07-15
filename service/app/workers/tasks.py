"""Core Celery tasks for video generation."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import redis as _redis
from sqlalchemy import create_engine, func, update as sa_update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import TaskStatus, VideoLeaseFence, VideoTask
from app.storage.local import LocalVideoStorage
from app.workers.celery_app import celery_app
from app.workers.parser import OutputNotFoundError, locate_output_file, probe_mp4
from app.workers.identity import get_worker_id
from app.workers.runner import run_oh
from app.observability.metrics import render_inflight

# --- Per-worker render concurrency cap (scale-multi-instance Phase 7) -------
# Caps concurrently running ``oh`` render subprocesses in THIS worker process
# so horizontal scale-out does not OOM Chrome/ffmpeg. The task body acquires
# this around ``run_oh``.
render_semaphore = threading.BoundedSemaphore(settings.max_concurrent_renders)
MAX_CONCURRENT_RENDERS = settings.max_concurrent_renders

logger = logging.getLogger(__name__)

# --- Active lease tokens (strict lease / fencing, WS-C / R20) ---------------
# Maps ``str(task_id) -> lease_token`` for tasks this worker process is
# currently rendering. Populated by the render pipeline right after a
# successful ``claim`` and cleared when the render finishes (success/failure/
# cancellation). The liveness loop uses this map to fence heartbeats: a stale
# owner (reclaimed by another worker) carries a token that no longer matches
# the DB row, so its heartbeat UPDATE affects 0 rows and it is not mistaken
# for alive.
_active_tokens: dict[str, int] = {}

# --- Redis connection pooling -----------------------------------------------
# A single process-global pool reused by every log push / tail read / abort
# check, instead of opening a fresh connection per stdout line.
_LOG_POOL: _redis.ConnectionPool | None = None


def _redis_client() -> _redis.Redis:
    """Return a Redis client backed by a shared connection pool."""
    global _LOG_POOL
    if _LOG_POOL is None:
        _LOG_POOL = _redis.ConnectionPool.from_url(settings.broker_url)
    return _redis.Redis(connection_pool=_LOG_POOL)


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


# Markers used inside the Redis Stream that backs task logs.
_DONE_MARKER = "__DONE__"
_LOG_CAP = 10000  # max retained entries per task stream


def _append_log(task_id: str, line: str) -> None:
    """Append a log line to the task's Redis Stream.

    Uses a single XADD per line (replayed and tailed by the SSE endpoint via
    XREAD). Connection is taken from the shared pool.
    """
    try:
        r = _redis_client()
        # Coalesce the done marker to avoid duplicate terminal events.
        r.xadd(f"oh:logs:{task_id}", {"line": line})
    except Exception:
        logger.warning("Failed to push log line to Redis for task %s", task_id)


def claim(task_id: uuid.UUID, worker_id: str, celery_task_id: str | None = None) -> tuple[bool, int]:
    """Atomically claim a queued/retrying task for ``worker_id``.

    A single conditional UPDATE (row lock) serializes concurrent workers so
    exactly one becomes the owner. The ``lease_token`` is bumped atomically and
    its new value is returned to the caller (R20): the owning worker holds the
    current token in memory and must carry it on every effectful write so a
    reclaimed/stale owner's writes are fenced. Returns ``(claimed, token)``;
    ``token`` is ``0`` when the claim did not win.

    See OpenSpec scale-multi-instance R7 and strict-lease R20.
    """
    task_id = uuid.UUID(str(task_id))
    values = dict(
        status=TaskStatus.RUNNING,
        started_at=func.now(),
        worker_id=worker_id,
        attempt=VideoTask.attempt + 1,
        heartbeat_at=func.now(),
        # Bump the lease token on every ownership transfer. First claim yields
        # 1 (column default 0), reclaim yields the next value. R20.
        lease_token=VideoTask.lease_token + 1,
    )
    if celery_task_id is not None:
        values["celery_task_id"] = celery_task_id
    with _sync_session() as db:
        result = db.execute(
            sa_update(VideoTask)
            .where(
                VideoTask.id == task_id,
                VideoTask.status.in_([TaskStatus.QUEUED, TaskStatus.RETRYING]),
                # Allow re-claim by the same worker (idempotent re-dispatch) or
                # by any worker when the row is unowned. A row owned by a
                # *different* worker (healthy) is not re-claimed here — reclaim
                # is driven by the beat scan once the owner is declared lost.
                (VideoTask.worker_id.is_(None) | (VideoTask.worker_id == worker_id)),
            )
            .values(**values)
            .returning(VideoTask.lease_token)
        )
        row = result.fetchone()
        db.commit()
        if row is None:
            return (False, 0)
        return (True, row[0])


def _mark_succeeded(
    task_id: str,
    storage_key: str,
    meta,  # VideoMeta
    result,  # RunResult
    worker_id: str | None = None,
    token: int | None = None,
) -> bool:
    """Persist a successful render.

    Success guard (scale-multi-instance R9): the terminal state is only written
    when the row is still RUNNING *for this worker*. A stale/previous owner
    (e.g. after a reclaim) matches 0 rows, so the existing terminal state is
    left untouched. Returns True if the write happened.

    Strict-lease fence (R20): when ``token`` is provided it is added to the
    WHERE clause (``lease_token == token``). A reclaimed owner holds a stale
    token, so its write is fenced even at the DB layer (defense-in-depth — the
    authoritative artifact fence is in :func:`fence_artifact`).
    """
    conditions = [VideoTask.id == uuid.UUID(str(task_id)), VideoTask.status == TaskStatus.RUNNING]
    if worker_id is not None:
        conditions.append(VideoTask.worker_id == worker_id)
    if token is not None:
        conditions.append(VideoTask.lease_token == token)
    with _sync_session() as db:
        exec_result = db.execute(
            sa_update(VideoTask)
            .where(*conditions)
            .values(
                status=TaskStatus.SUCCEEDED,
                finished_at=func.now(),
                output_path=storage_key,
                file_size_bytes=meta.file_size_bytes,
                duration_seconds=meta.duration_seconds,
                resolution=meta.resolution,
                fps=meta.fps,
                exit_code=result.exit_code,
            )
        )
        db.commit()
        return exec_result.rowcount == 1


def _mark_failed(task_id: str, exc: Exception, exit_code: int | None = None, worker_id: str | None = None, token: int | None = None) -> bool:
    """Persist a failed render.

    Ownership guard (scale-multi-instance R9): only writes when the row is
    still RUNNING for this worker, so a reclaimed/stale owner cannot flip a
    task another replica has taken over into FAILED. When ``token`` is provided
    it additionally fences by ``lease_token`` (R20 defense-in-depth).
    """
    conditions = [VideoTask.id == uuid.UUID(str(task_id)), VideoTask.status == TaskStatus.RUNNING]
    if worker_id is not None:
        conditions.append(VideoTask.worker_id == worker_id)
    if token is not None:
        conditions.append(VideoTask.lease_token == token)
    with _sync_session() as db:
        result = db.execute(
            sa_update(VideoTask)
            .where(*conditions)
            .values(
                status=TaskStatus.FAILED,
                error_message=str(exc)[:4000],
                exit_code=exit_code,
                finished_at=func.now(),
            )
        )
        db.commit()
        return result.rowcount == 1


def _mark_canceled(task_id: str, exc: Exception | None = None, worker_id: str | None = None, token: int | None = None) -> bool:
    """Mark a task CANCELED (user-requested cancellation).

    Ownership guard (scale-multi-instance R9): only writes when the row is
    still RUNNING for this worker, so a reclaimed/stale owner cannot clobber a
    task another replica has since taken over. When ``token`` is provided it
    additionally fences by ``lease_token`` (R20 defense-in-depth).
    """
    conditions = [VideoTask.id == uuid.UUID(str(task_id)), VideoTask.status == TaskStatus.RUNNING]
    if worker_id is not None:
        conditions.append(VideoTask.worker_id == worker_id)
    if token is not None:
        conditions.append(VideoTask.lease_token == token)
    with _sync_session() as db:
        result = db.execute(
            sa_update(VideoTask)
            .where(*conditions)
            .values(
                status=TaskStatus.CANCELED,
                finished_at=func.now(),
                error_message=str(exc)[:4000] if exc else None,
            )
        )
        db.commit()
        return result.rowcount == 1


def _read_lease_token(task_id: str) -> int:
    """Return the ``lease_token`` currently stored for ``task_id`` (0 if none)."""
    try:
        with _sync_session() as db:
            task = db.get(VideoTask, uuid.UUID(str(task_id)))
            return task.lease_token if task is not None else 0
    except Exception:
        logger.warning("Failed to read lease_token for task %s", task_id)
        return 0


def fence_artifact(task_id: str, token: int, storage_key: str) -> bool:
    """Record the accepted artifact token for a task (R20 primary artifact fence).

    Returns ``True`` iff ``token`` is the strictly highest seen so far, meaning
    this worker's artifact is the authoritative one. A stale token (one lower
    than the currently accepted token) returns ``False`` — its storage write is
    discarded (no valid artifact survives). The terminal ``_mark_succeeded``
    (guarded by ``worker_id`` + ``lease_token``) is the final authority that
    points ``output_path`` at the artifact; this table is the defense-in-depth
    signal that the *object store* write itself is fenced.
    """
    tid = uuid.UUID(str(task_id))
    with _sync_session() as db:
        row = db.get(VideoLeaseFence, tid)
        if row is None:
            db.add(VideoLeaseFence(task_id=tid, accepted_token=token, storage_key=storage_key))
            db.commit()
            return True
        if token > row.accepted_token:
            row.accepted_token = token
            row.storage_key = storage_key
            db.commit()
            return True
        return False


def _abort_requested(task_id: str) -> bool:
    """True if a cancellation flag was set for this task (cross-replica safe)."""
    try:
        r = _redis_client()
        return r.get(f"oh:abort:{task_id}") is not None
    except Exception:
        return False


def _update_log_tail(task_id: str) -> None:
    """Read the full log stream from Redis and write the tail to DB."""
    try:
        r = _redis_client()
        entries = r.xrange(f"oh:logs:{task_id}")
        raw = "".join(
            _as_str(fields.get(b"line")) + "\n" for _id, fields in entries
        )
        tail = raw[-settings.log_tail_bytes :]
        with _sync_session() as db:
            task = db.get(VideoTask, task_id)
            if task is not None:
                task.log_tail = tail
                db.commit()
    except Exception:
        logger.warning("Failed to update log tail for task %s", task_id)


def _as_str(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


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
    """Celery task: run oh CLI to generate a video and persist results.

    The render body lives in
    :func:`app.workers.render_pipeline.execute_video_render` so the Temporal
    ``VideoGenerationActivity`` runs identical logic. ``TransientError`` is
    re-raised by the pipeline and retried via ``autoretry_for``.
    """
    execute_video_render(task_id, celery_task_id=self.request.id)


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

            # Clean up Redis log stream
            try:
                _redis_client().delete(f"oh:logs:{str(task.id)}")
            except Exception:
                pass

            # Null the now-stale pointers so a later download returns a clean
            # 404 instead of pointing at a deleted file.
            task.output_path = None
            task.workspace_path = None

        db.commit()
        logger.info("Cleaned up %d expired tasks", len(expired))


# Imported at the bottom to avoid a circular import: `render_pipeline` imports
# the shared helpers (claim / _mark_* / _abort_requested / TransientError) from
# this module, so they must be defined before this line runs.
from app.workers.render_pipeline import execute_video_render  # noqa: E402,F401
