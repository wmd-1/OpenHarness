"""Per-tenant rate limiting (Phase 3, WS-A).

Enforces a per-minute submit cap keyed by ``tenant_id``. The backend is the
``limits`` engine's async storage layer (``MemoryStorage`` / ``RedisStorage``)
— the same primitives ``slowapi`` is built on. A Redis backend is required in
production so the count is *globally* shared across all ``api`` replicas
(``api×N``); the in-process memory backend counts per-replica and would admit
``N × rate``, which we must avoid.

The limit is applied as a FastAPI **dependency** (``rate_limit``) rather than a
``@limiter.limit`` decorator so the submit route keeps its clean signature:
the decorator form requires a ``request`` parameter, which shifts FastAPI's
dependency injection for this route and regresses an existing positional test
caller (``create_video(body, db)``).

Per-tenant rates come from ``quotas.rate_per_min`` (default 10). The trusted
internal ``system`` tenant is exempt (it drives internal automation and is not
an external abuse surface).

Note: ``limits`` >= 3.7 removed the ``Limiter`` facade; this module builds the
fixed-window limiter directly on the async storage primitives, which are stable
across the 3.x line.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from limits.aio.storage import MemoryStorage, RedisStorage
from sqlalchemy import select

from app.config import settings
from app.models import Quota
from app.tenant_ctx import get_current_tenant

SYSTEM_TENANT = "system"
DEFAULT_RATE_PER_MIN = 10
# Fixed window length in seconds (matches RateLimitItemPerMinute semantics).
_WINDOW_SECONDS = 60

_storage: MemoryStorage | RedisStorage | None = None


def _get_storage() -> MemoryStorage | RedisStorage:
    """Lazily build the shared async storage from the configured backend.

    ``OH_RATE_LIMIT_STORAGE_URI`` selects the backend (``redis://…`` for the
    production global-shared count; ``memory://`` for tests / single replica).
    Falls back to the Redis broker URL when unset.
    """
    global _storage
    if _storage is None:
        uri = settings.rate_limit_storage_uri or settings.broker_url
        if uri.startswith(("redis://", "rediss://")):
            _storage = RedisStorage(uri)
        else:
            _storage = MemoryStorage()
    return _storage


async def rate_limit(request: Request) -> None:
    """FastAPI dependency: enforce the tenant's per-minute submit rate.

    Raises ``HTTPException(429)`` when the tenant is over its ``rate_per_min``.
    The ``system`` tenant is always allowed. A limiter-backend outage fails
    *open* (request allowed) so the limiter can never block legitimate traffic.
    """
    tenant = get_current_tenant() or SYSTEM_TENANT
    if tenant == SYSTEM_TENANT:
        return

    # Resolve the tenant's per-minute cap (best-effort; fall back to default).
    rate = DEFAULT_RATE_PER_MIN
    try:
        from app.db import async_session

        async with async_session() as s:
            row = (
                await s.execute(select(Quota).where(Quota.tenant_id == tenant))
            ).scalar_one_or_none()
            if row is not None:
                rate = row.rate_per_min
    except Exception:
        # Never block a request because the quota lookup failed.
        pass

    key = f"ws-a:ratelimit:{tenant}"
    try:
        count = await _get_storage().incr(key, expiry=_WINDOW_SECONDS, amount=1)
    except Exception:
        # Backend (Redis) unavailable: fail open rather than deny service.
        return

    if count > rate:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "tenant": tenant,
                "rate_per_min": rate,
                "current": count,
            },
            headers={"Retry-After": "60"},
        )
