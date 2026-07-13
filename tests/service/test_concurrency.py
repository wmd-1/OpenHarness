"""Phase 7 worker concurrency-cap tests (scale-multi-instance).

Verifies the per-worker render semaphore limits concurrently running ``oh``
subprocesses to ``MAX_CONCURRENT_RENDERS``, protecting Chrome/ffmpeg memory
under horizontal scale-out. Cross-queue priority consumption itself is covered
at the routing level by ``queue_for_priority`` (test_scheduler.py) and the
end-to-end acceptance in design source §5.
"""

from __future__ import annotations

import threading
import time

from app.workers import tasks as tasks_mod


def test_render_semaphore_caps_concurrent_renders():
    cap = tasks_mod.MAX_CONCURRENT_RENDERS
    sem = tasks_mod.render_semaphore
    held = 0
    max_seen = 0
    lock = threading.Lock()

    def worker():
        nonlocal held, max_seen
        with sem:
            with lock:
                held += 1
                if held > max_seen:
                    max_seen = held
            # Hold the slot long enough that more threads queue up.
            time.sleep(0.1)
            with lock:
                held -= 1

    # Launch more threads than the cap so the excess must wait.
    threads = [threading.Thread(target=worker) for _ in range(cap + 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # At most `cap` renders run at once, and the cap is actually exercised.
    assert max_seen <= cap
    assert max_seen == cap


def test_render_semaphore_is_bounded_type():
    assert isinstance(tasks_mod.render_semaphore, threading.BoundedSemaphore)
    assert tasks_mod.MAX_CONCURRENT_RENDERS >= 1
