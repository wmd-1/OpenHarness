"""Prometheus metrics for the HyperFrames video service.

Exposes process-agnostic custom metrics (render in-flight, render duration)
plus a ``/metrics`` scrape endpoint. Uses ``prometheus-client`` directly so it
stays compatible with the project-pinned ``fastapi<0.116`` (the higher-level
``prometheus-fastapi-instrumentator`` line pulls a newer Starlette that
conflicts with that pin).

Scale-multi-instance Phase 5, design source R8.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, Histogram, generate_latest

# --- Custom metrics (registered on the default registry) -------------------

RENDER_INFLIGHT = Gauge(
    "oh_render_inflight",
    "Number of oh render processes currently executing on this worker.",
)

RENDER_DURATION = Histogram(
    "oh_render_duration_seconds",
    "Wall-clock duration of a single oh render execution, in seconds.",
    buckets=(5, 15, 30, 60, 120, 300, 600, 900, 1800),
)


@contextmanager
def render_inflight():
    """Context manager that tracks an in-flight render in ``oh_render_inflight``.

    Wrap the synchronous ``run_oh`` call in the Celery worker so Grafana can
    see how many renders are concurrently running per replica (Phase 7 caps
    this with a global semaphore).
    """
    RENDER_INFLIGHT.inc()
    start = time.monotonic()
    try:
        yield
    finally:
        RENDER_INFLIGHT.dec()
        RENDER_DURATION.observe(time.monotonic() - start)


def render_inflight_value() -> float:
    """Return the current in-flight render count (used by tests)."""
    return RENDER_INFLIGHT._value.get()


# --- Scrape endpoint -------------------------------------------------------

metrics_router = APIRouter(tags=["metrics"])


@metrics_router.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint (text exposition format)."""
    body = generate_latest()
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)
