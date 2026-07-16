"""Quota enforcement tests for WS-A (Phase 3).

Verifies that a tenant over its ``quotas`` concurrency / daily-submit limits
receives ``429``, while the trusted ``system`` tenant is exempt. Uses the
shared sqlite backend configured by ``tests/service/conftest.py`` so the
submit path hits the same rows the test seeds.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db import async_session
from app.main import app
from app.middleware.auth import hash_api_key
from app.models import ApiKey, Base, Quota, TaskStatus, Tenant, VideoTask
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


async def _seed(sm, tenant_id: str, *, max_concurrent=2, daily=100, active=0, api_key=True):
    async with sm() as s:
        s.add(Tenant(id=tenant_id, name=tenant_id))
        if api_key:
            s.add(ApiKey(tenant_id=tenant_id, key_hash=hash_api_key("secret-" + tenant_id)))
        s.add(Quota(tenant_id=tenant_id, max_concurrent=max_concurrent, daily_submit_limit=daily))
        for _ in range(active):
            s.add(
                VideoTask(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    prompt="active",
                    status=TaskStatus.RUNNING,
                    storage_kind="local",
                )
            )
        await s.commit()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _post(ac, tenant_id="acme"):
    return ac.post(
        "/v1/videos",
        json=VideoCreateRequest(prompt="x").model_dump(),
        headers={"X-API-Key": "secret-" + tenant_id},
    )


async def test_concurrency_quota_returns_429(seeded):
    await _seed(seeded, "acme", max_concurrent=2, active=2)
    async with _client() as ac:
        r = await _post(ac)
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "quota_exceeded"
    assert r.json()["detail"]["reason"] == "concurrency"


async def test_daily_quota_returns_429(seeded):
    await _seed(seeded, "acme", max_concurrent=10, daily=1, active=0)
    # One submit already happened today for this tenant.
    async with seeded() as s:
        s.add(
            VideoTask(
                id=uuid.uuid4(),
                tenant_id="acme",
                prompt="prior",
                status=TaskStatus.SUCCEEDED,
                storage_kind="local",
            )
        )
        await s.commit()
    async with _client() as ac:
        r = await _post(ac)
    assert r.status_code == 429
    assert r.json()["detail"]["reason"] == "daily_submit"


async def test_system_tenant_exempt_from_quota(seeded):
    """The trusted `system` tenant may submit even when over the default quota.

    Fill `system`'s concurrency with the default max (2); an unkeyed submit
    (system tenant, require_keys defaults to False) still succeeds.
    """
    async with seeded() as s:
        s.add(Tenant(id="system", name="system"))
        for _ in range(2):
            s.add(
                VideoTask(
                    id=uuid.uuid4(),
                    tenant_id="system",
                    prompt="active",
                    status=TaskStatus.RUNNING,
                    storage_kind="local",
                )
            )
        await s.commit()
    async with _client() as ac:
        r = await ac.post("/v1/videos", json=VideoCreateRequest(prompt="x").model_dump())
    assert r.status_code in (200, 201)
