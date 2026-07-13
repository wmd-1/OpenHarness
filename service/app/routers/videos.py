"""/v1/videos/* API routes."""

from __future__ import annotations

import json
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import get_db, get_storage, storage_for_kind
from app.models import TaskStatus, VideoTask
from app.schemas import (
    TaskLinks,
    VideoCreateRequest,
    VideoCreateResponse,
    VideoDeleteResponse,
    VideoTaskResponse,
)
from app.storage.base import VideoStorage
from app.workers.celery_app import celery_app
from app.workers.scheduler import get_scheduler

router = APIRouter(prefix="/v1/videos", tags=["videos"])

# Marker written into the log stream when generation finishes.
_DONE_MARKER = "__DONE__"


# ---- Helpers ----


def _task_links(task_id: uuid.UUID) -> TaskLinks:
    sid = str(task_id)
    return TaskLinks(
        self_=f"/v1/videos/{sid}",
        file=f"/v1/videos/{sid}/file",
        events=f"/v1/videos/{sid}/events",
    )


def _to_response(task: VideoTask) -> VideoTaskResponse:
    return VideoTaskResponse(
        task_id=task.id,
        prompt=task.prompt,
        skill=task.skill,
        status=task.status,
        timeout_seconds=task.timeout_seconds,
        output_path=task.output_path,
        file_size_bytes=task.file_size_bytes,
        duration_seconds=task.duration_seconds,
        resolution=task.resolution,
        fps=task.fps,
        exit_code=task.exit_code,
        error_message=task.error_message,
        log_tail=task.log_tail,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


def _set_abort_flag(task_id: uuid.UUID) -> None:
    """Best-effort cross-replica abort flag (mirrors the RUNNING path).

    The worker polls this via ``is_aborted`` and tears down the ``oh`` process
    group (scale-multi-instance R9).
    """
    try:
        import redis as redis_lib

        rr = redis_lib.from_url(settings.broker_url)
        rr.set(f"oh:abort:{task_id}", "1", ex=3600)
        rr.close()
    except Exception:
        pass


async def _get_task_or_404(task_id: uuid.UUID, db: AsyncSession) -> VideoTask:
    task = await db.get(VideoTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# ---- Endpoints ----


@router.post("", response_model=VideoCreateResponse, status_code=201)
async def create_video(
    body: VideoCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> VideoCreateResponse:
    """Submit a new video generation task."""
    # Idempotency check
    if body.idempotency_key is not None:
        stmt = select(VideoTask).where(VideoTask.idempotency_key == body.idempotency_key)
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing is not None:
            return VideoCreateResponse(
                task_id=existing.id,
                status=existing.status,
                links=_task_links(existing.id),
            )

    task = VideoTask(
        prompt=body.prompt,
        skill="hyperframes",
        status=TaskStatus.QUEUED,
        timeout_seconds=body.timeout_seconds,
        extra_oh_args=json.dumps(body.extra_oh_args) if body.extra_oh_args else None,
        idempotency_key=body.idempotency_key,
        storage_kind=settings.storage_kind,
    )
    try:
        db.add(task)
        await db.commit()
        await db.refresh(task)
    except IntegrityError:
        # Concurrent duplicate submission: the SELECT above passed, but another
        # request inserted the same idempotency_key first. Roll back and return
        # the existing task instead of 500-ing.
        await db.rollback()
        if body.idempotency_key is not None:
            existing = (
                await db.execute(
                    select(VideoTask).where(
                        VideoTask.idempotency_key == body.idempotency_key
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return VideoCreateResponse(
                    task_id=existing.id,
                    status=existing.status,
                    links=_task_links(existing.id),
                )
        raise

    # Enqueue render via the configured scheduler (Phase 6). Priority drives
    # the queue tier (high/normal/low) for Phase 7 priority consumption.
    get_scheduler().enqueue(str(task.id), priority=task.priority)

    return VideoCreateResponse(
        task_id=task.id,
        status=task.status,
        links=_task_links(task.id),
    )


@router.get("/{task_id}", response_model=VideoTaskResponse)
async def get_video(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> VideoTaskResponse:
    """Return details for a specific task."""
    task = await _get_task_or_404(task_id, db)
    return _to_response(task)


async def _iterfile(fileobj, start: int = 0, chunk: int = 1024 * 1024) -> AsyncGenerator[bytes, None]:
    """Yield file contents from ``start`` onward without blocking the event loop.

    The blocking ``fileobj.read`` is offloaded to a threadpool so a large
    video does not stall other requests on the same uvicorn worker.
    """
    try:
        if start:
            fileobj.seek(start)
        while True:
            data = await run_in_threadpool(fileobj.read, chunk)
            if not data:
                break
            yield data
    finally:
        fileobj.close()


@router.get("/{task_id}/file")
async def download_video(
    task_id: uuid.UUID,
    request: Request,
    mode: str = Query(default="redirect"),
    db: AsyncSession = Depends(get_db),
):
    """Download the generated video file (supports HTTP Range).

    Default ``mode=redirect`` returns a 302 to a presigned URL when the task's
    artifact lives in S3 (``storage_kind='s3'``) and a URL can be built
    (MODIFY R3). Otherwise — local/NFS backend, ``?mode=stream``, or when no
    presigned URL is available — it streams the bytes directly (backward
    compatible with the single-instance behavior).
    """
    task = await _get_task_or_404(task_id, db)
    if task.status != TaskStatus.SUCCEEDED:
        raise HTTPException(
            status_code=409,
            detail={"status": task.status, "message": "Video not ready"},
        )
    if not task.output_path:
        raise HTTPException(status_code=404, detail="Output file not found")

    storage = storage_for_kind(task.storage_kind)

    # Default redirect mode: hand S3 artifacts off to a presigned URL so the
    # API box never proxies the object body (scale-multi-instance R4).
    if mode != "stream" and task.storage_kind == "s3":
        presigned = storage.presigned_url(task.output_path)
        if presigned is not None:
            return RedirectResponse(url=presigned, status_code=302)

    try:
        fileobj, size = storage.open(task.output_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Output file not found on storage")

    # Parse Range header (single range only).
    start = 0
    range_header = request.headers.get("Range")
    if range_header and range_header.startswith("bytes="):
        try:
            start = int(range_header[len("bytes=") :].split("-")[0])
        except (ValueError, IndexError):
            start = 0
    start = max(0, min(start, size - 1)) if size else 0

    status_code = 206 if start > 0 else 200
    headers = {
        "Content-Type": "video/mp4",
        "Content-Disposition": f'attachment; filename="{task_id}.mp4"',
        "Accept-Ranges": "bytes",
        "Content-Length": str(size - start),
    }
    if start > 0:
        headers["Content-Range"] = f"bytes {start}-{size - 1}/{size}"

    return StreamingResponse(
        _iterfile(fileobj, start=start),
        status_code=status_code,
        media_type="video/mp4",
        headers=headers,
    )


@router.get("/{task_id}/events")
async def video_events(task_id: uuid.UUID):
    """SSE endpoint for real-time task progress updates.

    Backed by a Redis Stream (``oh:logs:<id>``): a single cursor replays
    history and tails new entries, so a line can never be delivered both as a
    replay and as a live message (the old list+pubsub race).
    """
    from sse_starlette.sse import EventSourceResponse

    async def _event_generator() -> AsyncGenerator[dict, None]:
        try:
            import redis as redis_lib

            r = redis_lib.from_url(settings.broker_url)
        except Exception:
            # If Redis is unavailable, just end the stream
            yield {"event": "error", "data": json.dumps({"error": "Redis unavailable"})}
            return

        sid = str(task_id)
        log_key = f"oh:logs:{sid}"

        def _line_of(fields) -> str:
            val = fields.get(b"line") if isinstance(fields, dict) else None
            if val is None:
                return ""
            return val.decode("utf-8", errors="replace") if isinstance(val, bytes) else str(val)

        try:
            # Replay existing log entries (oldest -> newest).
            history = await run_in_threadpool(r.xrange, log_key, min="-", max="+")
            last_id = "0-0"
            for entry_id, fields in history:
                last_id = entry_id
                line = _line_of(fields)
                if line == _DONE_MARKER:
                    yield {"event": "done", "data": json.dumps({"status": "completed"})}
                    return
                yield {"event": "log", "data": line}

            # Tail new entries until the done marker arrives.
            # xread is a blocking call, so offload it to the threadpool to keep
            # the event loop responsive.
            while True:
                resp = await run_in_threadpool(r.xread, {log_key: last_id}, block=5000)
                if not resp:
                    continue
                for _stream, messages in resp:
                    for entry_id, fields in messages:
                        last_id = entry_id
                        line = _line_of(fields)
                        if line == _DONE_MARKER:
                            yield {
                                "event": "done",
                                "data": json.dumps({"status": "completed"}),
                            }
                            return
                        yield {"event": "log", "data": line}
        finally:
            r.close()

    return EventSourceResponse(_event_generator())


@router.delete("/{task_id}", response_model=VideoDeleteResponse)
async def delete_video(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    storage: VideoStorage = Depends(get_storage),
) -> VideoDeleteResponse:
    """Cancel a queued task or delete a completed one."""
    task = await _get_task_or_404(task_id, db)

    if task.status == TaskStatus.QUEUED:
        # Revoke Celery task if it hasn't started
        if task.celery_task_id:
            celery_app.control.revoke(task.celery_task_id, terminate=True, signal="SIGTERM")
        # Durable cancellation flag + cross-replica abort key (scale-multi-instance R9).
        task.status = TaskStatus.CANCELED
        task.cancellation_requested = True
        _set_abort_flag(task.id)
        await db.commit()
        return VideoDeleteResponse(
            task_id=task.id,
            status=task.status,
            message="Task canceled",
        )

    if task.status == TaskStatus.RUNNING:
        # Signal the worker to kill the oh process group. The worker is the
        # authoritative party (revoke may not reach a different replica's
        # child), so we set a cross-replica Redis flag it polls via is_aborted.
        _set_abort_flag(task.id)
        # Best-effort nudge to the Celery worker as well.
        if task.celery_task_id:
            celery_app.control.revoke(task.celery_task_id, terminate=True, signal="SIGTERM")
        # Durable cancellation flag (scale-multi-instance R9): survives Redis
        # blips and is readable by any replica that later owns the task.
        task.status = TaskStatus.CANCELED
        task.cancellation_requested = True
        await db.commit()
        return VideoDeleteResponse(
            task_id=task.id,
            status=task.status,
            message="Task termination requested",
        )

    # For completed / failed / canceled tasks: delete resources
    if task.output_path:
        storage.delete(task.output_path)

    # Clean up workspace
    if task.workspace_path:
        from pathlib import Path
        import shutil

        wp = Path(task.workspace_path)
        if wp.exists():
            shutil.rmtree(wp, ignore_errors=True)

    task.status = TaskStatus.CANCELED
    task.output_path = None
    await db.commit()

    return VideoDeleteResponse(
        task_id=task.id,
        status=task.status,
        message="Task and resources deleted",
    )
