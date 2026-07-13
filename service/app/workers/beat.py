"""Worker liveness registration, heartbeat refresh, and lost-task recovery.

This module implements the multi-instance *ownership / reclaim* machinery.
It is deliberately **not** a strict lease/fencing protocol — it is a
heartbeat + Redis-TTL heuristic (design source §11):

* each worker process advertises itself in Redis with a short TTL and refreshes
  it on a timer;
* each worker refreshes the ``heartbeat_at`` column of the tasks it owns;
* a periodic ``recover_lost_tasks`` scan flips ``running`` tasks whose owner is
  no longer advertised AND whose heartbeat has gone stale back to ``retrying``,
  re-enqueueing them exactly once (a single conditional UPDATE serialized by the
  row lock guarantees idempotency — no double-reclaim, no double re-enqueue).

Because it is a heartbeat heuristic rather than a lease, a stale owner can in
rare cases still write a terminal state; the *success guard* (R9, in
``tasks._mark_succeeded``) prevents that write from clobbering a task another
replica has since taken over. See design source §11.7 for the residual risks,
which are explicitly out of scope for normal-crash acceptance.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

import redis as _redis
from celery import signals as _celery_signals
from sqlalchemy import func, select, update as sa_update

from app.config import settings
from app.models import TaskStatus, VideoTask
from app.workers import tasks as worker_tasks
from app.workers.celery_app import celery_app
from app.workers.identity import get_worker_id

logger = logging.getLogger(__name__)

# --- Tunables (design source §11.2) -----------------------------------------
WORKER_REGISTRY_TTL = 20  # seconds; Redis key lifetime — must exceed the refresh interval
WORKER_REGISTRY_INTERVAL = 10  # seconds between registry refreshes
HEARTBEAT_INTERVAL = 10  # seconds between heartbeat refreshes for owned tasks
STALE_AFTER = 60  # seconds; a task with no heartbeat for this long is "lost"
RECOVER_INTERVAL = 30  # seconds between recovery scans


# --- Registry (Redis) -------------------------------------------------------

def register_worker(
    redis_client: _redis.Redis,
    worker_id: str | None = None,
    ttl: int = WORKER_REGISTRY_TTL,
) -> None:
    """Advertise this worker's liveness in Redis with a short TTL.

    As long as the key exists the worker is considered alive; the periodic
    refresh keeps it from expiring while the process runs.
    """
    wid = worker_id or get_worker_id()
    try:
        redis_client.set(f"oh:worker:{wid}", "1", ex=ttl)
    except Exception:
        logger.warning("Failed to register worker %s in Redis", wid)


def alive_worker_ids(redis_client: _redis.Redis) -> set[str] | None:
    """Return the set of worker ids currently advertised as alive.

    Returns ``None`` (rather than an empty set) when Redis cannot be reached,
    so callers can distinguish "Redis is down" (skip recovery — do NOT risk a
    mass reclaim) from "no workers registered" (genuinely empty → reclaim all
    stale tasks).
    """
    try:
        keys = redis_client.keys("oh:worker:*")
    except Exception:
        logger.warning("Failed to read worker registry from Redis; skipping recovery")
        return None
    ids: set[str] = set()
    for k in keys:
        name = k.decode() if isinstance(k, bytes) else str(k)
        if name.startswith("oh:worker:"):
            ids.add(name[len("oh:worker:"):])
    return ids


# --- Heartbeat (DB) ---------------------------------------------------------

def refresh_owned_heartbeats(
    worker_id: str, db_session_factory: Callable | None = None
) -> int:
    """Refresh ``heartbeat_at`` for all RUNNING tasks owned by this worker.

    Returns the number of rows refreshed. The WHERE clause is scoped to this
    worker_id, so it can never steal work from another replica.
    """
    make_session = db_session_factory or worker_tasks._sync_session
    with make_session() as db:
        result = db.execute(
            sa_update(VideoTask)
            .where(
                VideoTask.worker_id == worker_id,
                VideoTask.status == TaskStatus.RUNNING,
            )
            .values(heartbeat_at=func.now())
        )
        db.commit()
        return result.rowcount


# --- Recovery (idempotent) --------------------------------------------------

def recover_lost_tasks(
    redis_client: _redis.Redis | None = None,
    db_session_factory: Callable | None = None,
    stale_after: int = STALE_AFTER,
) -> int:
    """Reclaim tasks whose owner is gone and heartbeat has gone stale.

    Idempotent: a single conditional UPDATE (row lock) flips at most one row,
    so concurrent beats / replicas cannot double-reclaim or double re-enqueue.
    Returns the number of tasks reclaimed.

    Skips entirely (returns 0) when the alive-worker set cannot be determined
    (Redis unreachable) to avoid a runaway reclaim of every stale task.
    """
    r = redis_client or worker_tasks._redis_client()
    make_session = db_session_factory or worker_tasks._sync_session

    alive = alive_worker_ids(r)
    if alive is None:
        return 0

    # Naive UTC cutoff — portable across sqlite (unit tests) and Postgres
    # (where a naive param is interpreted as UTC).
    cutoff = datetime.utcnow() - timedelta(seconds=stale_after)

    scan_conditions = [
        VideoTask.status == TaskStatus.RUNNING,
        # A running task whose heartbeat is NULL (never seeded/refreshed) is
        # also "lost" — treat it the same as a stale heartbeat. The owner-alive
        # guard below still prevents reclaiming tasks owned by a live worker.
        (VideoTask.heartbeat_at.is_(None) | (VideoTask.heartbeat_at < cutoff)),
    ]
    if alive:
        scan_conditions.append(VideoTask.worker_id.notin_(alive))

    reclaimed = 0
    with make_session() as db:
        rows = db.execute(select(VideoTask.id).where(*scan_conditions)).scalars().all()
        for tid in rows:
            flip_conditions = [
                VideoTask.id == tid,
                VideoTask.status == TaskStatus.RUNNING,
            ]
            if alive:
                flip_conditions.append(VideoTask.worker_id.notin_(alive))
            flip = (
                sa_update(VideoTask)
                .where(*flip_conditions)
                .values(
                    status=TaskStatus.RETRYING,
                    worker_id=None,
                    attempt=VideoTask.attempt + 1,
                )
            )
            res = db.execute(flip)
            db.commit()
            if res.rowcount == 1:
                reclaimed += 1
                try:
                    worker_tasks.generate_video_task.delay(str(tid))
                except Exception:
                    logger.warning("Failed to re-enqueue reclaimed task %s", tid)
    return reclaimed


# --- Background loops (run inside the worker process) -----------------------

def _liveness_loop(stop: threading.Event, worker_id: str, interval: int = WORKER_REGISTRY_INTERVAL) -> None:
    """Periodically register this worker and refresh its owned heartbeats."""
    r = worker_tasks._redis_client()
    while not stop.is_set():
        register_worker(r, worker_id)
        try:
            refresh_owned_heartbeats(worker_id)
        except Exception:
            logger.warning("Heartbeat refresh failed", exc_info=True)
        stop.wait(interval)


def start_liveness_thread(worker_id: str | None = None) -> threading.Thread:
    """Start the per-process liveness loop as a daemon thread."""
    wid = worker_id or get_worker_id()
    stop = threading.Event()
    t = threading.Thread(target=_liveness_loop, args=(stop, wid), daemon=True)
    t.start()
    return t


def _recover_loop(stop: threading.Event, interval: int = RECOVER_INTERVAL) -> None:
    """Periodically run recovery (used when beat is co-located with a worker)."""
    while not stop.is_set():
        try:
            recover_lost_tasks()
        except Exception:
            logger.warning("recover_lost_tasks failed", exc_info=True)
        stop.wait(interval)


# --- Celery wiring ----------------------------------------------------------

@_celery_signals.worker_process_init.connect
def _on_worker_process_init(**kwargs) -> None:
    """Each prefork worker process advertises its own liveness + heartbeats."""
    try:
        start_liveness_thread()
    except Exception:
        logger.warning("Failed to start liveness thread", exc_info=True)


@celery_app.task(name="recover_lost_tasks")
def recover_lost_tasks_task() -> int:
    """Celery periodic wrapper around :func:`recover_lost_tasks`."""
    return recover_lost_tasks()
