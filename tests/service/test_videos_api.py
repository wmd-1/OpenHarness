"""Tests for the /v1/videos API endpoints."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, TaskStatus, VideoTask


# ---- Fixtures ----

SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite://"

engine = create_async_engine(SQLALCHEMY_DATABASE_URL, echo=False)
TestAsyncSession = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    """Create tables before each test and drop them after."""
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
    """Create a test client with DB session override."""
    from app.deps import get_db, get_storage
    from app.main import app
    from app.storage.local import LocalVideoStorage

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db

    # Use a temp dir for storage
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = LocalVideoStorage(root=Path(tmp_dir))
        app.dependency_overrides[get_storage] = lambda: storage

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    app.dependency_overrides.clear()


# ---- Tests ----


class TestCreateVideo:
    """POST /v1/videos"""

    @patch("app.routers.videos.generate_video_task")
    async def test_create_video_success(self, mock_celery, client: AsyncClient):
        """Should create a task and enqueue it."""
        mock_celery.delay = MagicMock()
        response = await client.post(
            "/v1/videos",
            json={"prompt": "Make a video"},
        )
        assert response.status_code == 201
        data = response.json()
        assert "task_id" in data
        assert data["status"] == "queued"
        assert "links" in data
        assert data["links"]["self"].startswith("/v1/videos/")
        assert data["links"]["file"].endswith("/file")
        assert data["links"]["events"].endswith("/events")

    @patch("app.routers.videos.generate_video_task")
    async def test_create_video_with_idempotency(self, mock_celery, client: AsyncClient, db_session):
        """Should return existing task for same idempotency key."""
        mock_celery.delay = MagicMock()

        # Create first task
        r1 = await client.post(
            "/v1/videos",
            json={"prompt": "Test", "idempotency_key": "key-1"},
        )
        assert r1.status_code == 201
        task_id_1 = r1.json()["task_id"]

        # Create second task with same key
        r2 = await client.post(
            "/v1/videos",
            json={"prompt": "Test", "idempotency_key": "key-1"},
        )
        assert r2.status_code == 201
        assert r2.json()["task_id"] == task_id_1

    async def test_create_video_empty_prompt(self, client: AsyncClient):
        """Should reject empty prompt."""
        response = await client.post(
            "/v1/videos",
            json={"prompt": ""},
        )
        assert response.status_code == 422

    async def test_create_video_invalid_timeout(self, client: AsyncClient):
        """Should reject timeout outside allowed range."""
        response = await client.post(
            "/v1/videos",
            json={"prompt": "Test", "timeout_seconds": 10},
        )
        assert response.status_code == 422

    @patch("app.routers.videos.generate_video_task")
    async def test_create_video_rejects_forbidden_oh_arg(self, mock_celery, client: AsyncClient):
        """Should reject disallowed extra_oh_args with 422 at the API edge."""
        mock_celery.delay = MagicMock()
        response = await client.post(
            "/v1/videos",
            json={
                "prompt": "Test",
                "extra_oh_args": ["--permission-mode", "evil"],
            },
        )
        assert response.status_code == 422


class TestGetVideo:
    """GET /v1/videos/{task_id}"""

    @patch("app.routers.videos.generate_video_task")
    async def test_get_existing_task(self, mock_celery, client: AsyncClient, db_session):
        """Should return task details."""
        mock_celery.delay = MagicMock()
        create_resp = await client.post(
            "/v1/videos",
            json={"prompt": "Make a video"},
        )
        task_id = create_resp.json()["task_id"]

        get_resp = await client.get(f"/v1/videos/{task_id}")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["task_id"] == task_id
        assert data["status"] == "queued"
        assert data["prompt"] == "Make a video"

    async def test_get_nonexistent_task(self, client: AsyncClient):
        """Should return 404 for unknown task ID."""
        fake_id = str(uuid.uuid4())
        response = await client.get(f"/v1/videos/{fake_id}")
        assert response.status_code == 404


class TestDownloadVideo:
    """GET /v1/videos/{task_id}/file"""

    async def test_download_not_ready(self, client: AsyncClient, db_session):
        """Should return 409 when video not yet ready."""
        task = VideoTask(
            prompt="test",
            status=TaskStatus.RUNNING,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        response = await client.get(f"/v1/videos/{task.id}/file")
        assert response.status_code == 409


class TestDeleteVideo:
    """DELETE /v1/videos/{task_id}"""

    async def test_delete_queued_task(self, client: AsyncClient, db_session):
        """Should cancel a queued task."""
        task = VideoTask(
            prompt="test",
            status=TaskStatus.QUEUED,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        with patch("app.routers.videos.celery_app") as mock_celery:
            mock_celery.control.revoke = MagicMock()
            response = await client.delete(f"/v1/videos/{task.id}")

        assert response.status_code == 200
        assert response.json()["status"] == "canceled"

    async def test_delete_nonexistent_task(self, client: AsyncClient):
        """Should return 404 for unknown task."""
        fake_id = str(uuid.uuid4())
        response = await client.delete(f"/v1/videos/{fake_id}")
        assert response.status_code == 404


class TestHealthCheck:
    """GET /healthz"""

    async def test_health_endpoint_exists(self, client: AsyncClient):
        """Should respond to health check."""
        response = await client.get("/healthz")
        # May return 200 or 500 depending on DB/Redis availability,
        # but the endpoint should exist and not return 404
        assert response.status_code != 404
