"""WS-C: strict lease + fencing token tests (R20).

Covers the fencing guarantees introduced by the ``lease_token`` protocol:

* a stale owner's terminal write is fenced (``WHERE worker_id AND lease_token``
  matches 0 rows) — defense-in-depth at the DB layer;
* the object-store artifact write is fenced by the ``video_lease_fence`` mapping
  table — a stale token is discarded (no valid artifact survives);
* the render pipeline discards its artifact when it was reclaimed mid-render
  (Redis flap / false reclaim scenario);
* a reclaimed owner's heartbeat is rejected (it cannot falsely appear alive).

Drives the real ``claim`` / ``_mark_*`` / ``fence_artifact`` / ``refresh_owned_heartbeats``
helpers and the shared ``execute_video_render`` pipeline against sqlite (no
Postgres/Redis/temporal-server required).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, update as sa_update
from sqlalchemy.orm import Session

from app.models import Base, TaskStatus, VideoLeaseFence, VideoTask
from app.workers import beat, render_pipeline, tasks as worker_tasks
from app.workers.identity import set_worker_id
from app.workers.parser import VideoMeta


def _meta():
    return VideoMeta(file_size_bytes=100, duration_seconds=1.0, resolution="2x2", fps=1)


def _result(exit_code: int = 0):
    return type("RunResult", (), {"exit_code": exit_code, "stdout": ""})()


def _with_engine(eng):
    Base.metadata.create_all(eng)
    worker_tasks._sync_engine = eng
    render_pipeline._sync_engine = eng
    return eng


@pytest.fixture
def sync_db():
    eng = create_engine("sqlite://")
    _with_engine(eng)
    yield eng
    worker_tasks._sync_engine = None
    render_pipeline._sync_engine = None
    eng.dispose()


# ---------------------------------------------------------------------------
# Terminal-write fence (defense-in-depth at the DB layer)
# ---------------------------------------------------------------------------


def test_terminal_write_fenced_by_stale_token(sync_db):
    eng = sync_db
    tid = uuid.uuid4()
    with Session(eng) as s:
        s.add(VideoTask(id=tid, prompt="x", status=TaskStatus.QUEUED))
        s.commit()

    # First claim yields token 1.
    claimed, token = worker_tasks.claim(tid, "worker-A")
    assert claimed is True
    assert token == 1

    # Simulate reclaim: a NEW owner bumped the token and took over the row.
    with Session(eng) as s:
        s.execute(
            sa_update(VideoTask)
            .where(VideoTask.id == tid)
            .values(lease_token=2, worker_id="worker-B", status=TaskStatus.RUNNING)
        )
        s.commit()

    # The stale owner's writes must all be fenced (0 rows).
    assert worker_tasks._mark_succeeded(str(tid), "stale.mp4", _meta(), _result(0), worker_id="worker-A", token=1) is False
    assert worker_tasks._mark_failed(str(tid), RuntimeError("x"), worker_id="worker-A", token=1) is False
    assert worker_tasks._mark_canceled(str(tid), worker_id="worker-A", token=1) is False

    with Session(eng) as s:
        got = s.get(VideoTask, tid)
        assert got.status == TaskStatus.RUNNING  # unchanged
        assert got.output_path is None

    # The current owner (token 2) may write the terminal state.
    assert worker_tasks._mark_succeeded(str(tid), "ok.mp4", _meta(), _result(0), worker_id="worker-B", token=2) is True
    with Session(eng) as s:
        got = s.get(VideoTask, tid)
        assert got.status == TaskStatus.SUCCEEDED
        assert got.output_path == "ok.mp4"


# ---------------------------------------------------------------------------
# Object-store artifact fence (video_lease_fence mapping table)
# ---------------------------------------------------------------------------


def test_fence_artifact_rejects_stale_token(sync_db):
    tid = uuid.uuid4()
    # New owner writes with token 2 -> accepted.
    assert worker_tasks.fence_artifact(str(tid), 2, "ok.mp4") is True
    # Stale owner (token 1) is rejected.
    assert worker_tasks.fence_artifact(str(tid), 1, "stale.mp4") is False
    # A newer token (3) wins.
    assert worker_tasks.fence_artifact(str(tid), 3, "newer.mp4") is True

    with Session(sync_db) as s:
        row = s.get(VideoLeaseFence, tid)
        assert row is not None
        assert row.accepted_token == 3
        assert row.storage_key == "newer.mp4"


# ---------------------------------------------------------------------------
# Pipeline discards the artifact when reclaimed mid-render (Redis flap)
# ---------------------------------------------------------------------------


def test_pipeline_discards_artifact_when_reclaimed(sync_db, monkeypatch):
    eng = sync_db
    set_worker_id("worker-A")
    tid = uuid.uuid4()
    with Session(eng) as s:
        s.add(VideoTask(id=tid, prompt="say hello", status=TaskStatus.QUEUED, timeout_seconds=30))
        s.commit()

    # The render "completes" but a false reclaim bumped the lease token while
    # the (alive) worker was still rendering — the classic Redis-flap double-run.
    def _run_oh(**kwargs):
        cwd = kwargs["cwd"]
        mp4 = cwd / "out.mp4"
        mp4.write_text("fake-video-bytes")
        with Session(eng) as s:
            s.execute(
                sa_update(VideoTask)
                .where(VideoTask.id == tid)
                .values(lease_token=VideoTask.lease_token + 1, worker_id=None, status=TaskStatus.RETRYING)
            )
            s.commit()
        return type("RunResult", (), {"exit_code": 0, "stdout": "**输出文件:** `out.mp4`"})()

    monkeypatch.setattr(render_pipeline, "run_oh", _run_oh)
    monkeypatch.setattr(render_pipeline, "locate_output_file", lambda stdout, workspace: workspace / "out.mp4")
    monkeypatch.setattr(render_pipeline, "probe_mp4", lambda mp4: _meta())
    # storage_for_kind is reached only on success; the reclaim discards before
    # save, but patch it defensively.
    monkeypatch.setattr(render_pipeline, "storage_for_kind", lambda kind: worker_tasks.LocalVideoStorage())

    render_pipeline.execute_video_render(str(tid))

    with Session(eng) as s:
        got = s.get(VideoTask, tid)
        # No valid terminal state and no referenced artifact.
        assert got.status == TaskStatus.RETRYING
        assert got.output_path is None
        # The reclaim did bump the token + clear the owner.
        assert got.lease_token == 2
        assert got.worker_id is None


# ---------------------------------------------------------------------------
# Stale heartbeat rejected (prevents false-alive)
# ---------------------------------------------------------------------------


def test_stale_heartbeat_rejected(sync_db):
    eng = sync_db
    from datetime import datetime, timedelta

    tid = uuid.uuid4()
    old = datetime.utcnow() - timedelta(seconds=300)
    with Session(eng) as s:
        s.add(
            VideoTask(
                id=tid,
                prompt="x",
                status=TaskStatus.RUNNING,
                worker_id="worker-A",
                lease_token=1,
                heartbeat_at=old,
            )
        )
        s.commit()

    # Healthy owner: token matches -> heartbeat refreshed.
    n = beat.refresh_owned_heartbeats("worker-A", tokens={str(tid): 1})
    assert n == 1
    with Session(eng) as s:
        assert s.get(VideoTask, tid).heartbeat_at > old

    # Simulate reclaim: token bumped, ownership transferred.
    with Session(eng) as s:
        s.execute(
            sa_update(VideoTask)
            .where(VideoTask.id == tid)
            .values(lease_token=2, worker_id="worker-B", status=TaskStatus.RUNNING)
        )
        s.commit()

    # Stale owner's heartbeat (token 1) is rejected (0 rows) -> cannot appear alive.
    n_stale = beat.refresh_owned_heartbeats("worker-A", tokens={str(tid): 1})
    assert n_stale == 0

    # New owner (token 2) refreshes normally.
    n_new = beat.refresh_owned_heartbeats("worker-B", tokens={str(tid): 2})
    assert n_new == 1
