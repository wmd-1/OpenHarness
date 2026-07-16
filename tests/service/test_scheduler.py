"""Phase 6 scheduler abstraction tests (scale-multi-instance).

Covers:
- ``queue_for_priority`` maps the numeric ``priority`` column (1-10) to a
  queue tier (high/normal/low) that Phase 7 workers consume.
- ``CeleryScheduler.enqueue`` routes to the correct queue tier.
- ``get_scheduler`` switches backend via ``OH_SCHEDULER_BACKEND``; the Temporal
  backend is a disabled placeholder that raises ``NotImplementedError``.
"""

from __future__ import annotations

import pytest

import app.workers.scheduler as scheduler_mod
from app.workers import tasks as tasks_mod
from app.workers.scheduler import (
    CeleryScheduler,
    TemporalScheduler,
    get_scheduler,
    queue_for_priority,
)


def test_queue_for_priority_tiers():
    # high tier
    assert queue_for_priority(10) == "high"
    assert queue_for_priority(9) == "high"
    assert queue_for_priority(7) == "high"
    # normal tier
    assert queue_for_priority(6) == "normal"
    assert queue_for_priority(5) == "normal"
    assert queue_for_priority(4) == "normal"
    # low tier
    assert queue_for_priority(3) == "low"
    assert queue_for_priority(1) == "low"


async def test_celery_scheduler_enqueue_selects_queue(monkeypatch):
    captured = {}

    class _Res:
        id = "celery-resp-id"

    def fake_apply_async(args, queue=None, **kwargs):
        captured["args"] = args
        captured["queue"] = queue
        return _Res()

    monkeypatch.setattr(tasks_mod.generate_video_task, "apply_async", fake_apply_async)

    sched = CeleryScheduler()
    rid = await sched.enqueue("task-1", priority=9)
    assert rid == "celery-resp-id"
    assert captured["args"] == ("task-1",)
    assert captured["queue"] == "high"

    await sched.enqueue("task-2", priority=5)
    assert captured["queue"] == "normal"

    await sched.enqueue("task-3", priority=2)
    assert captured["queue"] == "low"


def test_get_scheduler_backend_switch(monkeypatch):
    class _Settings:
        scheduler_backend = "celery"

    s = _Settings()
    monkeypatch.setattr(scheduler_mod, "settings", s)

    assert isinstance(get_scheduler(), CeleryScheduler)
    # Flip to the Temporal placeholder without touching the global Settings.
    s.scheduler_backend = "temporal"
    assert isinstance(get_scheduler(), TemporalScheduler)


async def test_temporal_scheduler_requires_reachable_server(monkeypatch):
    """WS-B: TemporalScheduler is now a real implementation. With no reachable
    temporal-server it must fail fast (R19) instead of silently falling back to
    Celery. Use a closed port so it errors immediately."""
    monkeypatch.setattr(scheduler_mod.settings, "temporal_host", "127.0.0.1:1")
    sched = TemporalScheduler()
    with pytest.raises(Exception):
        await sched.enqueue("tid")
    with pytest.raises(Exception):
        await sched.cancel("cid")
