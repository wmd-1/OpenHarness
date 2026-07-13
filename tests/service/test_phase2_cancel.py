"""Phase 2 Task 7: DELETE dual-writes cancellation_requested + Redis abort key.

Exercises the real ``DELETE /v1/videos/{id}`` endpoint via FastAPI's TestClient
against an in-file sqlite DB (override of ``get_db``) and a fakeredis server
(patched ``redis.from_url``). Verifies the durable ``cancellation_requested``
flag and the cross-replica ``oh:abort:{id}`` key are both written.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.deps import get_db
from app.main import app
from app.models import Base, TaskStatus, VideoTask


@pytest.fixture
def client_and_redis():
    db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    make_session = async_sessionmaker(eng, expire_on_commit=False)

    async def _override_get_db():
        async with make_session() as s:
            yield s

    # One explicit fakeredis server shared by the patched from_url (app writes)
    # and our assertions (reads) so they agree.
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeStrictRedis(server=server)

    import redis as _redis_mod

    orig_from_url = _redis_mod.from_url

    def _fake_from_url(url, *args, **kwargs):
        return fakeredis.FakeStrictRedis(server=server)

    _redis_mod.from_url = _fake_from_url

    from app.workers.celery_app import celery_app

    orig_revoke = celery_app.control.revoke
    celery_app.control.revoke = lambda *a, **k: None

    app.dependency_overrides[get_db] = _override_get_db

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _seed():
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with make_session() as s:
            q = VideoTask(
                id=uuid.uuid4(),
                prompt="q",
                status=TaskStatus.QUEUED,
                celery_task_id="cq",
            )
            r = VideoTask(
                id=uuid.uuid4(),
                prompt="r",
                status=TaskStatus.RUNNING,
                celery_task_id="cr",
            )
            s.add_all([q, r])
            await s.commit()
            return q.id, r.id

    q_id, r_id = loop.run_until_complete(_seed())

    with TestClient(app) as client:
        yield client, server, q_id, r_id, make_session

    app.dependency_overrides.clear()
    _redis_mod.from_url = orig_from_url
    celery_app.control.revoke = orig_revoke
    loop.close()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _read(make_session, tid):
    async def _go():
        async with make_session() as s:
            return await s.get(VideoTask, tid)

    return asyncio.get_event_loop().run_until_complete(_go())


def test_delete_running_sets_cancellation_and_abort(client_and_redis):
    client, server, q_id, r_id, make_session = client_and_redis
    resp = client.delete(f"/v1/videos/{r_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "canceled"

    row = _read(make_session, r_id)
    assert row.status == TaskStatus.CANCELED
    assert row.cancellation_requested is True
    # Cross-replica abort key written to Redis (same shared server).
    assert fakeredis.FakeStrictRedis(server=server).get(f"oh:abort:{r_id}") is not None


def test_delete_queued_sets_cancellation_and_abort(client_and_redis):
    client, server, q_id, r_id, make_session = client_and_redis
    resp = client.delete(f"/v1/videos/{q_id}")
    assert resp.status_code == 200

    row = _read(make_session, q_id)
    assert row.status == TaskStatus.CANCELED
    assert row.cancellation_requested is True
    assert fakeredis.FakeStrictRedis(server=server).get(f"oh:abort:{q_id}") is not None
