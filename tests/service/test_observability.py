"""Phase 5 observability tests: /metrics, /healthz, /readyz (scale-multi-instance R8).

Covers:
- /metrics exposes ``oh_render_inflight`` / ``oh_render_duration_seconds`` and
  reflects a real in-flight render.
- /readyz returns queue-consumption status (pending / running / heartbeat lag)
  with HTTP 200 while the process is up.
- /healthz reflects S3 reachability without ever becoming fatal (5xx): S3 down
  => status "degraded" + s3="error"; S3 up => s3="ok"; local storage => s3=None.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.deps import get_db
from app.main import app
from app.models import Base, TaskStatus, VideoTask
from app.observability.metrics import render_inflight, render_inflight_value


SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://"
engine = create_async_engine(SQLALCHEMY_DATABASE_URL, echo=False)
TestAsyncSession = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    """Isolated sqlite schema per test + clear any leaked dependency overrides."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


@pytest.fixture
async def db_session():
    async with TestAsyncSession() as session:
        yield session


@pytest.fixture
async def client(db_session):
    """Async HTTP client with ``get_db`` overridden to the test sqlite session."""

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# --- /metrics ---------------------------------------------------------------


async def test_metrics_exposes_render_inflight(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "oh_render_inflight" in body
    assert "oh_render_duration_seconds" in body


async def test_metrics_reflects_in_flight_render(client):
    """The gauge tracks a render opened via ``render_inflight()``."""
    assert render_inflight_value() == 0
    with render_inflight():
        assert render_inflight_value() == 1
        resp = await client.get("/metrics")
        assert "oh_render_inflight 1.0" in resp.text
    # Released in the contextmanager's finally branch.
    assert render_inflight_value() == 0


# --- /readyz ----------------------------------------------------------------


async def test_readyz_returns_queue_status(client, db_session):
    db_session.add(VideoTask(prompt="a", status=TaskStatus.QUEUED))
    db_session.add(VideoTask(prompt="b", status=TaskStatus.RETRYING))
    db_session.add(VideoTask(prompt="c", status=TaskStatus.RUNNING))
    db_session.add(VideoTask(prompt="d", status=TaskStatus.SUCCEEDED))
    db_session.add(VideoTask(prompt="e", status=TaskStatus.FAILED))
    await db_session.commit()

    resp = await client.get("/readyz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    # pending = QUEUED + RETRYING; running = RUNNING
    assert data["pending"] == 2
    assert data["running"] == 1
    # No running tasks in this fixture snapshot => heartbeat lag is None.
    assert data["heartbeat_lag_seconds"] is None


# --- /healthz ---------------------------------------------------------------


async def test_healthz_local_storage_has_no_s3_field(client, monkeypatch):
    """Local storage => ``s3`` omitted (None), never probed."""
    monkeypatch.setattr(settings, "storage_kind", "local")
    resp = await client.get("/healthz")
    # Always 200 — never fatal even if DB/Redis are down in the test env.
    assert resp.status_code == 200
    assert resp.json()["s3"] is None


async def test_healthz_s3_down_is_degraded_not_fatal(client, monkeypatch):
    """S3 unreachable => degraded + s3='error', but HTTP 200 (not 5xx)."""
    import app.routers.health as health_router

    monkeypatch.setattr(settings, "storage_kind", "s3")

    async def _s3_down():
        return False

    monkeypatch.setattr(health_router, "_s3_ok", _s3_down)

    resp = await client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["s3"] == "error"
    assert data["status"] == "degraded"


async def test_healthz_s3_up_reflected(client, monkeypatch):
    """S3 reachable => s3='ok' (overall status depends on DB/Redis too)."""
    import app.routers.health as health_router

    monkeypatch.setattr(settings, "storage_kind", "s3")

    async def _s3_up():
        return True

    monkeypatch.setattr(health_router, "_s3_ok", _s3_up)

    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["s3"] == "ok"
