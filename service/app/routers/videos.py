"""/v1/videos/* API routes."""

from __future__ import annotations

import json
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import get_db, get_storage
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

router = APIRouter(prefix="/v1/videos", tags=["videos"])


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
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    # Enqueue Celery task
    from app.workers.tasks import generate_video_task

    generate_video_task.delay(str(task.id))

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


@router.get("/{task_id}/file")
async def download_video(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    storage: VideoStorage = Depends(get_storage),
):
    """Stream-download the generated video file."""
    from fastapi.responses import StreamingResponse

    task = await _get_task_or_404(task_id, db)
    if task.status != TaskStatus.SUCCEEDED:
        raise HTTPException(
            status_code=409,
            detail={"status": task.status, "message": "Video not ready"},
        )
    if not task.output_path:
        raise HTTPException(status_code=404, detail="Output file not found")

    try:
        fileobj, size = storage.open(task.output_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Output file not found on storage")

    async def _iterfile(chunk: int = 1024 * 1024) -> AsyncGenerator[bytes, None]:
        try:
            while True:
                data = fileobj.read(chunk)
                if not data:
                    break
                yield data
        finally:
            fileobj.close()

    return StreamingResponse(
        _iterfile(),
        media_type="video/mp4",
        headers={
            "Content-Length": str(size),
            "Content-Disposition": f'attachment; filename="{task_id}.mp4"',
            "Accept-Ranges": "bytes",
        },
    )


@router.get("/{task_id}/events")
async def video_events(task_id: uuid.UUID):
    """SSE endpoint for real-time task progress updates."""
    from sse_starlette.sse import EventSourceResponse

    async def _event_generator() -> AsyncGenerator[dict, None]:
        import asyncio

        try:
            import redis as redis_lib

            r = redis_lib.from_url(settings.broker_url)
        except Exception:
            # If Redis is unavailable, just end the stream
            yield {"event": "error", "data": json.dumps({"error": "Redis unavailable"})}
            return

        sid = str(task_id)
        channel = f"oh:channel:{sid}"
        log_key = f"oh:logs:{sid}"
        pubsub = r.pubsub()
        pubsub.subscribe(channel)

        # Replay existing log lines
        history = r.lrange(log_key, 0, -1)
        for line in reversed(history):
            decoded = line.decode("utf-8", errors="replace")
            yield {"event": "log", "data": decoded}

        try:
            # Listen for new events
            while True:
                message = pubsub.get_message(timeout=5.0)
                if message and message["type"] == "message":
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8", errors="replace")
                    if data == "__DONE__":
                        yield {"event": "done", "data": json.dumps({"status": "completed"})}
                        break
                    yield {"event": "log", "data": data}
                await asyncio.sleep(0.1)
        finally:
            pubsub.unsubscribe(channel)
            pubsub.close()
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
        task.status = TaskStatus.CANCELED
        await db.commit()
        return VideoDeleteResponse(
            task_id=task.id,
            status=task.status,
            message="Task canceled",
        )

    if task.status == TaskStatus.RUNNING:
        # Attempt to revoke running task
        if task.celery_task_id:
            celery_app.control.revoke(task.celery_task_id, terminate=True, signal="SIGTERM")
        task.status = TaskStatus.CANCELED
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
