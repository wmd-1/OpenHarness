"""Tenant isolation tests for WS-A (Phase 3).

Verifies that tenant-scoped reads (``_get_task_or_404``) cannot cross tenant
boundaries: a tenant sees only its own tasks, and the trusted ``system`` tenant
may read any task (mirrors the RLS exemption in the PG policy).

Uses an isolated aiosqlite engine so it does not depend on Postgres.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, Tenant, VideoTask, TaskStatus
from app.routers.videos import _get_task_or_404

SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://"


@pytest_asyncio.fixture
async def sessionmaker():
    engine = create_async_engine(SQLALCHEMY_DATABASE_URL, echo=False)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield sm
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed(sm):
    t_a = uuid.uuid4()
    t_b = uuid.uuid4()
    async with sm() as s:
        s.add(Tenant(id="A", name="A"))
        s.add(Tenant(id="B", name="B"))
        s.add(
            VideoTask(
                id=t_a,
                tenant_id="A",
                prompt="a",
                status=TaskStatus.QUEUED,
                storage_kind="local",
            )
        )
        s.add(
            VideoTask(
                id=t_b,
                tenant_id="B",
                prompt="b",
                status=TaskStatus.QUEUED,
                storage_kind="local",
            )
        )
        await s.commit()
    return t_a, t_b


async def test_tenant_sees_own_task(sessionmaker):
    t_a, _ = await _seed(sessionmaker)
    async with sessionmaker() as s:
        task = await _get_task_or_404(t_a, s, tenant_id="A")
        assert task.tenant_id == "A"


async def test_cross_tenant_read_is_404(sessionmaker):
    _, t_b = await _seed(sessionmaker)
    async with sessionmaker() as s:
        with pytest.raises(HTTPException) as exc:
            await _get_task_or_404(t_b, s, tenant_id="A")
        assert exc.value.status_code == 404


async def test_system_tenant_sees_any_task(sessionmaker):
    _, t_b = await _seed(sessionmaker)
    async with sessionmaker() as s:
        # The trusted internal `system` scope is RLS-exempt and may read any row.
        task = await _get_task_or_404(t_b, s, tenant_id="system")
        assert task.tenant_id == "B"
