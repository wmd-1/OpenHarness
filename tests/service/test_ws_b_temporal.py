"""WS-B: Real Temporal migration tests (sandbox-safe + docker/CI split).

The Temporal SDK's ``ActivityEnvironment`` lets us run an activity *without* a
running temporal-server, so the real render pipeline (shared with Celery) is
exercised in the sandbox. The full e2e (start temporal-server → enqueue/cancel
through a workflow) requires a server and is validated in the
``docker-compose.temporal.yml`` stack / CI (marked docker-only below).

See openspec change ``phase3-multitenancy-temporal-lease`` and its ``design.md``.
"""

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import Base, TaskStatus, VideoTask
from app.workers.render_pipeline import execute_video_render
from app.workers.scheduler import (
    CeleryScheduler,
    TemporalScheduler,
    get_scheduler,
)
from app.workers.temporal_worker import VideoGenerationActivity, VideoGenWorkflow


# ---------------------------------------------------------------------------
# Scheduler routing + fail-fast (no temporal-server needed)
# ---------------------------------------------------------------------------

def test_get_scheduler_routes_by_backend(monkeypatch):
    """get_scheduler returns the right backend class per OH_SCHEDULER_BACKEND."""
    monkeypatch.setattr(settings, "scheduler_backend", "celery")
    assert isinstance(get_scheduler(), CeleryScheduler)

    monkeypatch.setattr(settings, "scheduler_backend", "temporal")
    assert isinstance(get_scheduler(), TemporalScheduler)


@pytest.mark.asyncio
async def test_temporal_scheduler_enqueue_fails_fast_without_server(monkeypatch):
    """R19 scenario: an unreachable temporal-server must fail (not silently
    fall back to Celery). Use a closed port so it errors immediately."""
    monkeypatch.setattr(settings, "scheduler_backend", "temporal")
    monkeypatch.setattr(settings, "temporal_host", "127.0.0.1:1")

    scheduler = TemporalScheduler()
    with pytest.raises(Exception):
        await scheduler.enqueue(str(uuid.uuid4()))

    # restore default so other tests are unaffected
    monkeypatch.setattr(settings, "scheduler_backend", "celery")


def test_workflow_and_activity_definitions_valid():
    """The workflow/activity defs are decorated with the temporalio
    ``@workflow.defn`` / ``@activity.defn`` (validates registration without a
    server). The exact registered name is set via ``name=...`` in the decorator
    (VideoGenWorkflow / VideoGenerationActivity). The full activity execution
    is covered by ``test_activity_runs_shared_render_pipeline`` via
    ActivityEnvironment.
    """
    # temporalio marks decorated classes with these attributes; presence proves
    # the defs are registered (version-independent, avoids private name APIs).
    assert hasattr(VideoGenWorkflow, "__temporal_workflow_definition")
    assert hasattr(VideoGenerationActivity, "__temporal_activity_definition")
    assert callable(VideoGenerationActivity.run)
    assert asyncio.iscoroutinefunction(VideoGenerationActivity.run)


# ---------------------------------------------------------------------------
# Activity unit test via ActivityEnvironment (no temporal-server needed)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def sync_db():
    """Create the sync sqlite schema + a QUEUED task; tear down afterwards."""
    engine = create_engine(settings.db_sync_url, future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    task_id = uuid.uuid4()
    with Session() as db:
        task = VideoTask(
            id=task_id,
            prompt="say hello",
            status=TaskStatus.QUEUED,
            timeout_seconds=30,
            tenant_id="system",
            extra_oh_args=None,
        )
        db.add(task)
        db.commit()
    yield str(task_id)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _fake_run_oh(**kwargs):
    """Stand in for the blocking oh subprocess: write a dummy mp4 into cwd."""
    from app.workers.runner import RunResult

    cwd = kwargs["cwd"]
    mp4 = cwd / "output.mp4"
    mp4.write_text("fake-video-bytes")
    return RunResult(exit_code=0, stdout="")


@pytest.mark.asyncio
async def test_activity_runs_shared_render_pipeline(sync_db, monkeypatch):
    """VideoGenerationActivity.run executes the SAME pipeline as Celery and
    persists a SUCCEEDED terminal state + artifact (no temporal-server)."""
    from temporalio.testing import ActivityEnvironment

    monkeypatch.setattr(
        "app.workers.render_pipeline.run_oh", _fake_run_oh
    )

    env = ActivityEnvironment()
    heartbeats: list = []
    env.on_heartbeat = lambda *a, **k: heartbeats.append(a)

    # Runs the real execute_video_render (claim → run_oh → persist → artifact).
    # Pass a bound method so the activity's `self` is satisfied.
    await env.run(VideoGenerationActivity().run, sync_db)

    # Assert terminal state + artifact via the sync engine.
    engine = create_engine(settings.db_sync_url, future=True)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as db:
        task = db.get(VideoTask, uuid.UUID(sync_db))
        assert task is not None
        assert task.status == TaskStatus.SUCCEEDED, task.status
        assert task.output_path == f"{sync_db}.mp4"
        assert task.worker_id is not None
        # Artifact was written to the (sandbox) video dir.
        from app.storage.local import LocalVideoStorage

        assert LocalVideoStorage().exists(task.output_path)
    engine.dispose()


def test_shared_pipeline_importable():
    """Sanity: the shared pipeline is importable and Celery still delegates."""
    assert callable(execute_video_render)
    # Ensure no circular import at import time.
    from app.workers import tasks as tasks_mod

    assert hasattr(tasks_mod, "generate_video_task")
