"""Shared video-render pipeline (WS-B).

Extracts the render body previously embedded in ``tasks.generate_video_task`` so
that **both** the Celery task and the Temporal ``VideoGenerationActivity`` execute
identical render logic (single source of truth for the DB guards: ``claim`` /
``_mark_*`` / ``_abort_requested`` / ``_append_log`` / ``render_semaphore``).

The function is synchronous because it runs inside a Celery task body and inside a
Temporal Activity (which we run in a thread so the blocking ``run_oh`` subprocess
call does not block the event loop).
"""

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
from app.deps import storage_for_kind
from app.workers.identity import get_worker_id
from app.workers.parser import OutputNotFoundError, locate_output_file, probe_mp4
from app.workers.runner import run_oh
from app.observability.metrics import render_inflight

logger = logging.getLogger(__name__)

# Sync DB engine for render workers (they can't use async). Mirrors the engine
# used by the Celery task so behavior is identical.
_sync_engine = None


def _get_sync_engine():
    global _sync_engine
    if _sync_engine is None:
        _sync_engine = create_engine(settings.db_sync_url, pool_size=5, max_overflow=10)
    return _sync_engine


def _sync_session() -> Session:
    engine = _get_sync_engine()
    return Session(engine)


def execute_video_render(task_id: str, celery_task_id: str | None = None) -> None:
    """Run the full render pipeline for ``task_id`` and persist results.

    This is the single implementation shared by the Celery task and the Temporal
    Activity. Behavior is 1:1 with the former ``generate_video_task`` body.

    Args:
        task_id: The video task UUID (string form).
        celery_task_id: Optional Celery async-result id to store on the row
            (Celery path only; Temporal path passes ``None``).
    """
    # Lazy import to break the circular dependency with ``tasks``: ``tasks``
    # imports ``execute_video_render`` at module bottom, and ``tasks`` may be the
    # first module imported (e.g. when this pipeline is reached via the Temporal
    # worker). Importing the shared helpers here, at call time, avoids a
    # partially-initialized-module ImportError.
    from app.workers.tasks import (
        TransientError,
        _abort_requested,
        _active_tokens,
        _append_log,
        _mark_canceled,
        _mark_failed,
        _mark_succeeded,
        _read_lease_token,
        _redis_client,
        _update_log_tail,
        claim,
        fence_artifact,
        render_semaphore as _render_semaphore,
    )

    task_id = uuid.UUID(str(task_id))
    wid = get_worker_id()

    # Claim ownership for this worker process (scale-multi-instance R7) and
    # capture the strict-lease token we now hold (WS-C / R20). The token is
    # bumped atomically by the claim and must travel with every effectful write
    # so a reclaimed/stale owner's writes are fenced.
    claimed, token = claim(task_id, wid, celery_task_id=celery_task_id)
    if not claimed:
        logger.warning("Task %s already claimed by another worker; skipping", task_id)
        return
    _active_tokens[str(task_id)] = token

    try:
        with _sync_session() as db:
            task = db.get(VideoTask, task_id)
            if task is None:
                logger.error("Task %s not found in DB after claim", task_id)
                return
            if task.status == TaskStatus.CANCELED:
                return

            prompt = task.prompt
            timeout = task.timeout_seconds
            extra_oh_args = json.loads(task.extra_oh_args) if task.extra_oh_args else []
            storage_kind = task.storage_kind

        workspace = Path(settings.workspace_root) / str(task_id)
        workspace.mkdir(parents=True, exist_ok=True)

        with _sync_session() as db:
            task = db.get(VideoTask, task_id)
            if task is not None:
                task.workspace_path = str(workspace)
                db.commit()

        try:
            # Track an in-flight render so Grafana can see per-replica concurrency
            # (Phase 5 / R8). The per-process semaphore caps concurrent oh processes.
            with render_inflight():
                with _render_semaphore:
                    result = run_oh(
                        prompt=prompt,
                        cwd=workspace,
                        timeout=timeout,
                        on_log_line=lambda line: _append_log(task_id, line),
                        extra_args=extra_oh_args,
                        is_aborted=lambda: _abort_requested(task_id),
                        oh_bin=settings.oh_bin,
                        headless_shell_path=settings.headless_shell_path,
                    )

            # Guard: if the user canceled while running, do NOT overwrite the
            # status back to SUCCEEDED/FAILED. The worker is authoritative here.
            if _abort_requested(task_id):
                _mark_canceled(task_id, RuntimeError("canceled by user"), worker_id=wid, token=token)
                return

            _update_log_tail(task_id)

            if result.exit_code != 0:
                _mark_failed(
                    task_id,
                    RuntimeError(f"oh exited with code {result.exit_code}"),
                    exit_code=result.exit_code,
                    worker_id=wid,
                    token=token,
                )
                return

            mp4 = locate_output_file(result.stdout, workspace)
            meta = probe_mp4(mp4)

            # Best-effort fence (R20 §4.5): if we were reclaimed while rendering,
            # our held token no longer matches the DB row. Abandon the artifact
            # rather than producing a stale, valid side effect. The authoritative
            # artifact fence below is the backstop for the race window.
            if _read_lease_token(task_id) != token:
                logger.warning(
                    "Task %s reclaimed during render (db token != held %s); discarding artifact",
                    task_id,
                    token,
                )
                return

            storage = storage_for_kind(storage_kind)
            final_key = storage.save(task_id, mp4, lease_token=token)

            # Primary artifact fence (R20): only accept the write if our token is
            # the strictly highest seen. A stale owner's artifact is discarded.
            if not fence_artifact(task_id, token, final_key):
                logger.warning("Task %s artifact fenced (stale token %s); discarding", task_id, token)
                return

            if _mark_succeeded(task_id, final_key, meta, result, worker_id=wid, token=token):
                # Publish done marker into the log stream (consumed by SSE).
                try:
                    _redis_client().xadd(f"oh:logs:{task_id}", {"line": "__DONE__"})
                except Exception:
                    logger.warning("Failed to publish done marker for task %s", task_id)
            else:
                logger.warning("Task %s terminal write fenced (stale token %s)", task_id, token)

        except OutputNotFoundError as exc:
            # Deterministic failure — record and stop (do NOT re-raise, so the
            # message is acked rather than infinitely redelivered).
            _update_log_tail(task_id)
            _mark_failed(task_id, exc, worker_id=wid, token=token)
        except TransientError:
            # Transient infrastructure errors must propagate so the Celery task's
            # autoretry_for retries them. (Temporal path relies on the activity
            # retry_policy instead — it re-raises the same way.)
            raise
        except Exception as exc:
            # Deterministic failure — record and stop (no re-raise).
            _update_log_tail(task_id)
            _mark_failed(task_id, exc, worker_id=wid, token=token)
            return
    finally:
        # Always drop our token registration so the liveness loop stops
        # refreshing (and cannot falsely keep alive) a finished/reclaimed task.
        _active_tokens.pop(str(task_id), None)
