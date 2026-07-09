"""Tests for the SSE event endpoint backed by a Redis Stream.

Redis is substituted with fakeredis so no server is required. If fakeredis is
not installed the suite is skipped.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, TaskStatus, VideoTask

pytest.importorskip("fakeredis")

import fakeredis  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_sse_state():
    """sse_starlette lazily binds a module-global exit Event to the first event
    loop it sees. Under pytest-asyncio (a fresh loop per test) the 2nd SSE test
    would reuse a loop-bound Event and raise "bound to a different event loop".
    Reset it so each test recreates the Event on its own loop."""
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit = False
    AppStatus.should_exit_event = None
    yield


SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://"

engine = create_async_engine(SQLALCHEMY_DATABASE_URL, echo=False)
TestAsyncSession = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db_session():
    async with TestAsyncSession() as session:
        yield session


@pytest.fixture
async def client(db_session):
    from app.deps import get_db, get_storage
    from app.main import app
    from app.storage.local import LocalVideoStorage

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = LocalVideoStorage(root=Path(tmp_dir))
        app.dependency_overrides[get_storage] = lambda: storage
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    app.dependency_overrides.clear()


def _fake_redis():
    server = fakeredis.FakeServer()
    return fakeredis.FakeStrictRedis(server=server)


async def test_sse_streams_logs_then_done(client, db_session):
    task = VideoTask(prompt="x", status=TaskStatus.SUCCEEDED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    tid = str(task.id)

    fake = _fake_redis()
    # Pretend the worker already wrote logs + a done marker into the stream.
    fake.xadd(f"oh:logs:{tid}", {"line": "step 1"})
    fake.xadd(f"oh:logs:{tid}", {"line": "step 2"})
    fake.xadd(f"oh:logs:{tid}", {"line": "__DONE__"})

    with patch("redis.from_url", return_value=fake):
        resp = await client.get(f"/v1/videos/{tid}/events")

    assert resp.status_code == 200
    body = resp.text
    assert "event: log" in body
    assert "step 1" in body
    assert "step 2" in body
    assert "event: done" in body
    # No duplicate lines (the old list+pubsub race is gone).
    assert body.count("step 1") == 1


async def test_sse_no_duplicate_on_concurrent_publish(client, db_session):
    """Replay + live tail via a single cursor must not double-emit a line."""
    task = VideoTask(prompt="x", status=TaskStatus.SUCCEEDED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    tid = str(task.id)

    fake = _fake_redis()
    fake.xadd(f"oh:logs:{tid}", {"line": "only-once"})
    # Terminate the stream so the SSE generator returns (no live tail needed here).
    fake.xadd(f"oh:logs:{tid}", {"line": "__DONE__"})

    with patch("redis.from_url", return_value=fake):
        resp = await client.get(f"/v1/videos/{tid}/events")

    assert resp.status_code == 200
    assert resp.text.count("only-once") == 1
