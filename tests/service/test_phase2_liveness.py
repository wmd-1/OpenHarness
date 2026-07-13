"""Phase 2 liveness: registry, heartbeat refresh, idempotent reclaim.

Drives the real ``recover_lost_tasks`` / ``refresh_owned_heartbeats`` / registry
helpers against sqlite (DB) + fakeredis (registry). No Postgres/Redis required.

This covers the strong, testable guarantee of R8/R9: a stale task whose owner
is no longer advertised in Redis is reclaimed exactly once (row-lock UPDATE
serializes concurrent beats), and a task whose owner is still alive is never
reclaimed.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import fakeredis
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, TaskStatus, VideoTask
from app.workers import tasks as worker_tasks
from app.workers import beat


def _with_engine(eng):
    Base.metadata.create_all(eng)
    worker_tasks._sync_engine = eng
    return eng


@pytest.fixture
def sync_db():
    eng = create_engine("sqlite://")
    _with_engine(eng)
    yield eng
    worker_tasks._sync_engine = None
    eng.dispose()


@pytest.fixture
def redis_client():
    return fakeredis.FakeStrictRedis()


def _running(eng, wid, heartbeat_at):
    tid = uuid.uuid4()
    with Session(eng) as s:
        s.add(
            VideoTask(
                id=tid,
                prompt="x",
                status=TaskStatus.RUNNING,
                worker_id=wid,
                heartbeat_at=heartbeat_at,
            )
        )
        s.commit()
    return tid


def test_register_and_alive_set(redis_client):
    wid = "worker-X"
    beat.register_worker(redis_client, wid)
    assert wid in beat.alive_worker_ids(redis_client)
    assert "worker-Y" not in beat.alive_worker_ids(redis_client)


def test_recover_reclaims_only_stale_unowned(sync_db, redis_client, monkeypatch):
    eng = sync_db
    now = datetime.utcnow().replace(microsecond=0)
    stale = now - timedelta(seconds=120)
    alive_wid = "worker-live"
    dead_wid = "worker-dead"

    t_dead = _running(eng, dead_wid, stale)  # stale + dead owner -> reclaim
    t_live = _running(eng, alive_wid, stale)  # stale but owner alive -> keep
    t_fresh = _running(eng, dead_wid, now)  # fresh heartbeat -> keep

    # The "live" owner must be advertised in Redis so recover excludes it.
    beat.register_worker(redis_client, alive_wid)

    enqueued = []
    monkeypatch.setattr(
        worker_tasks.generate_video_task, "delay", lambda tid: enqueued.append(tid)
    )

    n = beat.recover_lost_tasks(redis_client, stale_after=60)
    assert n == 1
    assert enqueued == [str(t_dead)]

    with Session(eng) as s:
        assert s.get(VideoTask, t_dead).status == TaskStatus.RETRYING
        assert s.get(VideoTask, t_dead).worker_id is None
        assert s.get(VideoTask, t_live).status == TaskStatus.RUNNING
        assert s.get(VideoTask, t_fresh).status == TaskStatus.RUNNING


def test_recover_is_idempotent(sync_db, redis_client, monkeypatch):
    eng = sync_db
    stale = datetime.utcnow().replace(microsecond=0) - timedelta(seconds=120)
    t = _running(eng, "worker-dead", stale)

    enqueued = []
    monkeypatch.setattr(
        worker_tasks.generate_video_task, "delay", lambda tid: enqueued.append(tid)
    )

    n1 = beat.recover_lost_tasks(redis_client, stale_after=60)
    n2 = beat.recover_lost_tasks(redis_client, stale_after=60)
    assert n1 == 1
    assert n2 == 0  # already RETRYING -> nothing left to reclaim
    assert enqueued == [str(t)]  # re-enqueued exactly once


def test_recover_skips_when_owner_alive(sync_db, redis_client, monkeypatch):
    eng = sync_db
    stale = datetime.utcnow().replace(microsecond=0) - timedelta(seconds=120)
    wid = "worker-alive"
    t = _running(eng, wid, stale)
    beat.register_worker(redis_client, wid)  # owner still advertised

    enqueued = []
    monkeypatch.setattr(
        worker_tasks.generate_video_task, "delay", lambda tid: enqueued.append(tid)
    )

    n = beat.recover_lost_tasks(redis_client, stale_after=60)
    assert n == 0
    assert enqueued == []
    with Session(eng) as s:
        assert s.get(VideoTask, t).status == TaskStatus.RUNNING


def test_refresh_owned_heartbeats(sync_db, redis_client):
    eng = sync_db
    now = datetime.utcnow().replace(microsecond=0)
    old = now - timedelta(seconds=300)
    wid = "worker-W"
    t = _running(eng, wid, old)  # owned by wid -> should refresh
    t_other = _running(eng, "worker-Z", old)  # owned by someone else -> untouched

    n = beat.refresh_owned_heartbeats(wid)
    assert n == 1

    with Session(eng) as s:
        got = s.get(VideoTask, t)
        got_other = s.get(VideoTask, t_other)
        assert got.heartbeat_at > old
        assert got_other.heartbeat_at == old  # never touched
