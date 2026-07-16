"""Per-tenant quota enforcement (Phase 3, WS-A).

``check_quota`` is called from the submit path (``create_video``) before a
new :class:`VideoTask` is persisted. It enforces two limits derived from the
tenant's ``quotas`` row (or sensible defaults when no row exists):

* **concurrency** — number of tasks in a non-terminal state
  (``queued`` / ``running`` / ``retrying``). This bounds how many renders a
  tenant can have in flight at once.
* **daily submit** — number of tasks created since UTC midnight today.

Exceeding either raises ``HTTPException(429)``. The trusted internal ``system``
tenant is exempt (it is the admin scope that drives internal automation).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Quota, TaskStatus, VideoTask

# Applied when a tenant has no explicit ``quotas`` row.
DEFAULT_MAX_CONCURRENT = 2
DEFAULT_DAILY_SUBMIT_LIMIT = 100

# Non-terminal states that count toward the concurrency quota.
_ACTIVE_STATUSES = (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.RETRYING)


async def check_quota(tenant_id: str, db: AsyncSession) -> None:
    """Raise ``HTTPException(429)`` if ``tenant_id`` is over quota; else return.

    The ``system`` tenant is always allowed (internal/admin scope).
    """
    if tenant_id == "system":
        return

    quota = (
        await db.execute(select(Quota).where(Quota.tenant_id == tenant_id))
    ).scalar_one_or_none()

    max_concurrent = quota.max_concurrent if quota else DEFAULT_MAX_CONCURRENT
    daily_limit = quota.daily_submit_limit if quota else DEFAULT_DAILY_SUBMIT_LIMIT

    # --- Concurrency ---
    active = (
        await db.execute(
            select(func.count())
            .select_from(VideoTask)
            .where(VideoTask.tenant_id == tenant_id)
            .where(VideoTask.status.in_(_ACTIVE_STATUSES))
        )
    ).scalar_one()

    if active >= max_concurrent:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "quota_exceeded",
                "reason": "concurrency",
                "max_concurrent": max_concurrent,
                "current": active,
            },
        )

    # --- Daily submit ---
    now = datetime.now(timezone.utc)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    submitted_today = (
        await db.execute(
            select(func.count())
            .select_from(VideoTask)
            .where(VideoTask.tenant_id == tenant_id)
            .where(VideoTask.created_at >= day_start)
        )
    ).scalar_one()

    if submitted_today >= daily_limit:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "quota_exceeded",
                "reason": "daily_submit",
                "daily_submit_limit": daily_limit,
                "current": submitted_today,
            },
        )
