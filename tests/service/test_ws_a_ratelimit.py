"""Per-tenant rate limiting tests for WS-A (Phase 3).

Verifies that a tenant over its ``quotas.rate_per_min`` submit cap receives
``429`` (``error=rate_limited``), while the trusted ``system`` tenant is
exempt. Uses the shared sqlite backend configured by ``conftest.py`` and an
in-process ``MemoryStorage`` limiter (``OH_RATE_LIMIT_STORAGE_URI=memory://``),
and patches ``generate_video_task.apply_async`` so the submit path does not
need a live Celery broker.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.main import app
from app.middleware.auth import hash_api_key
from app.models import ApiKey, Base, Quota, Tenant, VideoTask
from app.schemas import VideoCreateRequest


@pytest_asyncio.fixture
async def seeded():
    """Create tables on the shared engine and tear them down afterwards."""
    engine = create_async_engine(settings.db_url, echo=False)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield sm
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed(sm, tenant_id, *, rate_per_min=3, max_concurrent=100, daily=100):
    async with sm() as s:
        s.add(Tenant(id=tenant_id, name=tenant_id))
        s.add(ApiKey(tenant_id=tenant_id, key_hash=hash_api_key("secret-" + tenant_id)))
        s.add(
            Quota(
                tenant_id=tenant_id,
                max_concurrent=max_concurrent,
                daily_submit_limit=daily,
                rate_per_min=rate_per_min,
            )
        )
        await s.commit()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _post(ac, tenant_id):
    return ac.post(
        "/v1/videos",
        json=VideoCreateRequest(prompt="x").model_dump(),
        headers={"X-API-Key": "secret-" + tenant_id},
    )


async def test_per_tenant_rate_limit_returns_429(seeded):
    """A tenant over its per-minute submit cap is blocked with 429/rate_limited.

    Quota (concurrency/daily) is set generous so the rate limiter — not quota —
    is the gate that trips first.
    """
    await _seed(seeded, "acme-rl", rate_per_min=3, max_concurrent=100, daily=100)
    async with _client() as ac:
        r1 = await _post(ac, "acme-rl")
        r2 = await _post(ac, "acme-rl")
        r3 = await _post(ac, "acme-rl")
        r4 = await _post(ac, "acme-rl")
    # The first three submits are within the per-minute cap.
    assert r1.status_code in (200, 201)
    assert r2.status_code in (200, 201)
    assert r3.status_code in (200, 201)
    # The fourth submit in the same minute is rate-limited.
    assert r4.status_code == 429
    assert r4.json()["detail"]["error"] == "rate_limited"
    assert r4.json()["detail"]["tenant"] == "acme-rl"


async def test_system_tenant_exempt_from_rate_limit(seeded):
    """The trusted `system` tenant is never rate-limited (internal automation)."""
    async with seeded() as s:
        s.add(Tenant(id="system", name="system"))
    async with _client() as ac:
        # Unkeyed requests are scoped to `system`; many submits must all pass.
        codes = []
        for _ in range(5):
            r = await ac.post(
                "/v1/videos", json=VideoCreateRequest(prompt="x").model_dump()
            )
            codes.append(r.status_code)
    assert all(c in (200, 201) for c in codes)
