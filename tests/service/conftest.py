"""Service-test shared fixtures/config.

Runs before any ``tests/service`` module is imported, so it can configure the
app environment *before* ``app.config.settings`` (and the DB engine / rate
limiter it builds) are materialised.

* ``OH_DB_URL`` → a file-backed sqlite DB shared by the app engine and the
  tests, so submit-path code hits the same rows the tests seed (the sandbox
  has no Postgres).
* ``OH_RATE_LIMIT_STORAGE_URI`` → an in-process ``memory://`` backend so
  per-tenant rate-limit behaviour is exercised without a Redis server.
  Production uses the Redis broker URL for a globally-shared count across
  ``api×N`` replicas.

The ``_stub_celery_apply_async`` autouse fixture stubs
``generate_video_task.apply_async`` so the submit path can be exercised
without a live Celery broker (the sandbox has no Redis). Scheduler routing
itself is covered by ``test_scheduler.py``.
"""

import os

os.environ.setdefault("OH_DB_URL", "sqlite+aiosqlite:////tmp/oh_service_tests.db")
os.environ.setdefault("OH_RATE_LIMIT_STORAGE_URI", "memory://")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_celery_apply_async(monkeypatch):
    """Stub the Celery enqueue so submit tests need no live broker.

    ``create_video`` enqueues ``generate_video_task`` via the scheduler; in the
    brokerless test environment we replace ``apply_async`` with a no-op that
    returns a fake result. The scheduler routing/queue-tier logic is tested
    directly in ``test_scheduler.py``.
    """
    from app.workers import tasks as tasks_mod

    class _FakeAsyncResult:
        id = "fake-celery-id"

    monkeypatch.setattr(
        tasks_mod.generate_video_task,
        "apply_async",
        lambda *a, **k: _FakeAsyncResult(),
    )
