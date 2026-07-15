"""Tests for the Celery worker task state machine (mocking run_oh).

These drive the *real* ``generate_video_task`` body (not just the Celery
enqueue) by patching ``run_oh`` and the storage/parser helpers, plus a sqlite
sync engine so no Postgres/Redis is required for the happy path.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import tempfile
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, TaskStatus, VideoTask
from app.storage.local import LocalVideoStorage
from app.workers import render_pipeline, tasks as worker_tasks
from app.workers.parser import VideoMeta


@pytest.fixture
def sync_db():
    """Point the worker's sync engine at a fresh in-memory sqlite DB.

    The render body now lives in ``render_pipeline`` (shared by Celery + Temporal),
    so both ``worker_tasks`` and ``render_pipeline`` engines are pointed here.
    """
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    worker_tasks._sync_engine = eng
    render_pipeline._sync_engine = eng
    yield eng
    worker_tasks._sync_engine = None
    render_pipeline._sync_engine = None
    eng.dispose()


def _class_with(**attrs):
    return type("Stub", (), attrs)


def test_happy_path_marks_succeeded(sync_db):
    import tempfile

    with Session(sync_db) as s:
        t = VideoTask(prompt="make a video", status=TaskStatus.QUEUED)
        s.add(t)
        s.commit()
        tid = str(t.id)

    with tempfile.TemporaryDirectory() as tmp:
        mp4 = Path(tmp) / "out.mp4"
        mp4.write_bytes(b"\x00" * 2048)  # real file so storage.save can copy it
        meta = VideoMeta(
            file_size_bytes=2048,
            duration_seconds=1.0,
            resolution="2x2",
            fps=1,
        )
        with patch.object(render_pipeline, "run_oh") as m_run, patch.object(
            render_pipeline, "locate_output_file"
        ) as m_locate, patch.object(render_pipeline, "probe_mp4") as m_probe, patch.object(
            render_pipeline, "storage_for_kind"
        ) as m_storage:
            m_run.return_value = _class_with(
                exit_code=0, stdout="**输出文件:** `out.mp4`"
            )
            m_locate.return_value = mp4
            m_probe.return_value = meta
            m_storage.return_value = LocalVideoStorage(root=Path(tmp) / "store")

            worker_tasks.generate_video_task.run(task_id=tid)

    with Session(sync_db) as s:
        got = s.get(VideoTask, t.id)
        assert got.status == TaskStatus.SUCCEEDED
        assert got.output_path is not None
        assert got.output_path.endswith(".mp4")
        assert got.file_size_bytes == 2048


def test_nonzero_exit_marks_failed(sync_db):
    with Session(sync_db) as s:
        t = VideoTask(prompt="x", status=TaskStatus.QUEUED)
        s.add(t)
        s.commit()
        tid = str(t.id)

    with patch.object(render_pipeline, "run_oh") as m_run:
        m_run.return_value = _class_with(exit_code=3, stdout="boom")
        worker_tasks.generate_video_task.run(task_id=tid)

    with Session(sync_db) as s:
        got = s.get(VideoTask, t.id)
        assert got.status == TaskStatus.FAILED
        assert got.exit_code == 3


def test_cancel_guard_prevents_overwrite_to_succeeded(sync_db):
    """If the user cancels mid-run, the task must stay CANCELED, never flip to
    SUCCEEDED (the original bug)."""
    with Session(sync_db) as s:
        t = VideoTask(prompt="x", status=TaskStatus.QUEUED)
        s.add(t)
        s.commit()
        tid = str(t.id)

    with patch.object(render_pipeline, "run_oh") as m_run, patch.object(
        render_pipeline, "storage_for_kind"
    ) as m_storage, patch.object(worker_tasks, "_abort_requested", return_value=True):
        m_storage.return_value = LocalVideoStorage(root=Path(tempfile.gettempdir()) / "store_ws_c")
        m_run.return_value = _class_with(
            exit_code=0, stdout="**输出文件:** `ghost.mp4`"
        )
        worker_tasks.generate_video_task.run(task_id=tid)
        m_run.return_value = _class_with(
            exit_code=0, stdout="**输出文件:** `ghost.mp4`"
        )
        worker_tasks.generate_video_task.run(task_id=tid)

    with Session(sync_db) as s:
        got = s.get(VideoTask, t.id)
        assert got.status == TaskStatus.CANCELED
        # locate/probe/save must NOT have run for a canceled task
        assert got.output_path is None


def test_vet_rejects_dangerous_args():
    """Sanity that the allowlist lives in the worker path's validator."""
    from app.security import InvalidOhArgError, vet_extra_oh_args

    with pytest.raises(InvalidOhArgError):
        vet_extra_oh_args(["--permission-mode", "evil"])
