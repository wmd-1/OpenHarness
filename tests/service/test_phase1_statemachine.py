"""Phase 1 state-machine tests: atomic claim (R7) + success guard (R9).

Drives the real ``claim`` / ``_mark_succeeded`` functions against a sqlite
sync engine (same harness as test_worker.py) so no Postgres/Redis is needed.
"""
from __future__ import annotations

import os
import tempfile
import threading
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, TaskStatus, VideoTask
from app.workers import tasks as worker_tasks
from app.workers.parser import VideoMeta


def _result(exit_code: int = 0):
    return type("RunResult", (), {"exit_code": exit_code})()


def _with_engine(eng):
    Base.metadata.create_all(eng)
    worker_tasks._sync_engine = eng
    return eng


def test_claim_only_one_worker_wins():
    eng = _with_engine(create_engine("sqlite://"))
    try:
        tid = uuid.uuid4()
        with Session(eng) as s:
            s.add(VideoTask(id=tid, prompt="x", status=TaskStatus.QUEUED))
            s.commit()

        assert worker_tasks.claim(tid, "worker-A") is True
        # A second claim by a different worker must not win (already RUNNING).
        assert worker_tasks.claim(tid, "worker-B") is False

        with Session(eng) as s:
            got = s.get(VideoTask, tid)
            assert got.status == TaskStatus.RUNNING
            assert got.worker_id == "worker-A"
            assert got.attempt == 1
    finally:
        worker_tasks._sync_engine = None
        eng.dispose()


def test_claim_rejects_non_queued_task():
    eng = _with_engine(create_engine("sqlite://"))
    try:
        tid = uuid.uuid4()
        with Session(eng) as s:
            s.add(
                VideoTask(
                    id=tid,
                    prompt="x",
                    status=TaskStatus.RUNNING,
                    worker_id="worker-A",
                )
            )
            s.commit()
        # A running task must not be re-claimed by another worker.
        assert worker_tasks.claim(tid, "worker-B") is False
    finally:
        worker_tasks._sync_engine = None
        eng.dispose()


def test_claim_is_atomic_under_concurrent_workers():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    eng = _with_engine(create_engine(f"sqlite:///{tmp.name}"))
    try:
        tid = uuid.uuid4()
        with Session(eng) as s:
            s.add(VideoTask(id=tid, prompt="x", status=TaskStatus.QUEUED))
            s.commit()

        wins: list[tuple[str, bool]] = []
        barrier = threading.Barrier(2)

        def contender(wid: str):
            barrier.wait()
            wins.append((wid, worker_tasks.claim(tid, wid)))

        t1 = threading.Thread(target=contender, args=("worker-A",))
        t2 = threading.Thread(target=contender, args=("worker-B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        winners = [wid for wid, ok in wins if ok]
        assert len(winners) == 1, f"expected exactly one winner, got {winners}"

        with Session(eng) as s:
            got = s.get(VideoTask, tid)
            assert got.worker_id in ("worker-A", "worker-B")
            assert got.status == TaskStatus.RUNNING
            assert got.attempt == 1
    finally:
        worker_tasks._sync_engine = None
        eng.dispose()
        os.unlink(tmp.name)


def test_success_guard_rejects_stale_owner():
    eng = _with_engine(create_engine("sqlite://"))
    try:
        tid = uuid.uuid4()
        with Session(eng) as s:
            # Simulate the task having been reclaimed by a NEW owner.
            s.add(
                VideoTask(
                    id=tid,
                    prompt="x",
                    status=TaskStatus.RUNNING,
                    worker_id="new-worker",
                )
            )
            s.commit()

        meta = VideoMeta(
            file_size_bytes=100, duration_seconds=1.0, resolution="2x2", fps=1
        )

        # Stale (previous) owner tries to write the terminal state.
        ok = worker_tasks._mark_succeeded(
            str(tid), "stale.mp4", meta, _result(0), worker_id="old-worker"
        )
        assert ok is False

        with Session(eng) as s:
            got = s.get(VideoTask, tid)
            assert got.status == TaskStatus.RUNNING  # not overwritten
            assert got.output_path is None

        # The current owner may write it.
        ok2 = worker_tasks._mark_succeeded(
            str(tid), "ok.mp4", meta, _result(0), worker_id="new-worker"
        )
        assert ok2 is True

        with Session(eng) as s:
            got = s.get(VideoTask, tid)
            assert got.status == TaskStatus.SUCCEEDED
            assert got.output_path == "ok.mp4"
    finally:
        worker_tasks._sync_engine = None
        eng.dispose()
