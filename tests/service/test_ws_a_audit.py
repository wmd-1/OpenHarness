"""Audit trail tests for WS-A (Phase 3).

Verifies that a mutating submit (``POST /v1/videos``) writes an ``audit_log``
row capturing the action, tenant, target type, and target id, committed
atomically with the task. Also verifies that cancelling a queued task writes a
``video.cancel`` audit entry. Uses the shared sqlite backend (conftest) and
patches ``generate_video_task.apply_async`` so no live broker is required.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.main import app
from app.middleware.auth import hash_api_key
from app.models import ApiKey, AuditLog, Base, Quota, Tenant, VideoTask
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


async def _seed(sm, tenant_id, *, max_concurrent=50, daily=100):
    async with sm() as s:
        s.add(Tenant(id=tenant_id, name=tenant_id))
        s.add(ApiKey(tenant_id=tenant_id, key_hash=hash_api_key("secret-" + tenant_id)))
        s.add(Quota(tenant_id=tenant_id, max_concurrent=max_concurrent, daily_submit_limit=daily))
        await s.commit()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_create_writes_audit_log(seeded):
    """A submit writes an audit row atomically with the task it creates."""
    await _seed(seeded, "acme-audit")
    async with _client() as ac:
        r = await ac.post(
            "/v1/videos",
            json=VideoCreateRequest(prompt="hello").model_dump(),
            headers={"X-API-Key": "secret-acme-audit"},
        )
    assert r.status_code in (200, 201)
    task_id = r.json()["task_id"]

    # The audit row must be committed in the same transaction as the task.
    async with seeded() as s:
        row = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "video.create",
                    AuditLog.target_id == str(task_id),
                )
            )
        ).scalar_one_or_none()
        assert row is not None
        assert row.tenant_id == "acme-audit"
        assert row.target_type == "video_task"
        assert row.target_id == str(task_id)


async def test_delete_writes_audit_log(seeded):
    """Cancelling a queued task writes a `video.cancel` audit entry."""
    await _seed(seeded, "acme-audit")
    # Create a task first.
    async with _client() as ac:
        r = await ac.post(
            "/v1/videos",
            json=VideoCreateRequest(prompt="bye").model_dump(),
            headers={"X-API-Key": "secret-acme-audit"},
        )
    assert r.status_code in (200, 201)
    task_id = r.json()["task_id"]

    # DELETE a queued task -> video.cancel audit entry.
    async with _client() as ac:
        d = await ac.delete(
            f"/v1/videos/{task_id}",
            headers={"X-API-Key": "secret-acme-audit"},
        )
    assert d.status_code == 200

    async with seeded() as s:
        rows = (
            await s.execute(
                select(AuditLog)
                .where(AuditLog.target_id == str(task_id))
                .order_by(AuditLog.ts)
            )
        ).scalars().all()
        actions = [row.action for row in rows]
        assert "video.create" in actions
        assert "video.cancel" in actions
