"""/healthz and /readyz endpoints (scale-multi-instance Phase 5)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import engine
from app.deps import get_db
from app.models import TaskStatus, VideoTask
from app.schemas import HealthResponse, ReadyResponse

router = APIRouter(tags=["health"])


async def _db_ok() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _redis_ok() -> bool:
    try:
        import redis as redis_lib

        r = redis_lib.from_url(settings.broker_url)
        r.ping()
        r.close()
        return True
    except Exception:
        return False


async def _s3_ok() -> bool | None:
    """Return None when not on S3; True/False when an S3 probe succeeds/fails.

    A failing probe degrades ``/healthz`` but never makes it fatal (R8).
    """
    if settings.storage_kind != "s3":
        return None
    try:
        from app.deps import storage_for_kind

        storage = storage_for_kind("s3")
        # A head-style probe: existence check on a sentinel key exercises the
        # S3 client without requiring the key to actually exist.
        storage.exists("__health_probe__")
        return True
    except Exception:
        return False


@router.get("/healthz", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Liveness + dependency check. S3 unreachable => degraded, not 5xx."""
    db_status = "ok" if await _db_ok() else "error"
    redis_status = "ok" if await _redis_ok() else "error"
    s3_status = await _s3_ok()
    s3_field = None if s3_status is None else ("ok" if s3_status else "error")

    overall = "ok"
    if db_status == "error" or redis_status == "error" or s3_field == "error":
        overall = "degraded"

    return HealthResponse(status=overall, db=db_status, redis=redis_status, s3=s3_field)


@router.get("/readyz", response_model=ReadyResponse)
async def ready_check(db: AsyncSession = Depends(get_db)) -> ReadyResponse:
    """Readiness: queue-consumption status (pending / running / heartbeat lag).

    Always 200 while the service process is up — readiness reflects whether the
    replica can consume work, not whether upstream deps are healthy.
    """
    pending = await db.scalar(
        select(func.count()).where(VideoTask.status.in_([TaskStatus.QUEUED, TaskStatus.RETRYING]))
    )
    running = await db.scalar(
        select(func.count()).where(VideoTask.status == TaskStatus.RUNNING)
    )
    last_beat = await db.scalar(
        select(func.max(VideoTask.heartbeat_at)).where(VideoTask.status == TaskStatus.RUNNING)
    )

    lag: float | None = None
    if last_beat is not None:
        lb = last_beat
        if lb.tzinfo is None:
            lb = lb.replace(tzinfo=timezone.utc)
        lag = (datetime.now(timezone.utc) - lb).total_seconds()

    return ReadyResponse(
        status="ok",
        pending=int(pending or 0),
        running=int(running or 0),
        heartbeat_lag_seconds=float(lag) if lag is not None else None,
    )
