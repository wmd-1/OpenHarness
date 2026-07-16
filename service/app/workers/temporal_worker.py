"""Temporal worker, workflow, and activity (WS-B).

Runs as a standalone process (``python -m app.workers.temporal_worker``),
registered in ``docker/supervisord.temporal.conf``. When
``OH_SCHEDULER_BACKEND=temporal``, the API enqueues renders as
``VideoGenWorkflow`` instances executed by this worker's
``VideoGenerationActivity``.

The render logic itself lives in :func:`app.workers.render_pipeline.execute_video_render`
so the Celery task and this activity run identical code (single source of truth).

The Temporal SDK is imported lazily so the Celery path never depends on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from app.config import settings

logger = logging.getLogger(__name__)


@workflow.defn(name="VideoGenWorkflow")
class VideoGenWorkflow:
    """Single-activity workflow that renders one video task."""

    @workflow.run
    async def run(self, task_id: str) -> None:
        # Activity heartbeats let a dead worker be detected within
        # heartbeat_timeout and retried. retry_policy declares the retry
        # behavior (Temporal owns retries instead of Celery's autoretry).
        await workflow.execute_activity(
            video_generation_activity,
            task_id,
            start_to_close_timeout=timedelta(minutes=45),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                initial_interval=timedelta(seconds=10),
                backoff_coefficient=2.0,
            ),
            task_queue=settings.temporal_task_queue,
        )


@activity.defn(name="VideoGenerationActivity")
async def video_generation_activity(task_id: str) -> None:
    """Runs the shared render pipeline for one task.

    ``execute_video_render`` is a blocking subprocess call, so it runs in a
    worker thread (``asyncio.to_thread``). A heartbeat loop runs in the
    activity's event loop (which holds the Temporal activity context) and calls
    the public ``activity.heartbeat`` every 10s, so a dead/hung worker is
    detected within ``heartbeat_timeout`` and the activity is retried.
    """
    async def _heartbeat_loop() -> None:
        while True:
            await asyncio.sleep(10)
            activity.heartbeat({"task_id": task_id, "watchdog": True})

    hb = asyncio.create_task(_heartbeat_loop())
    try:
        # Blocking render off the event loop. Lazy import keeps the heavy
        # render_pipeline (subprocess / filesystem) out of the Temporal
        # workflow sandbox, which otherwise fails validation.
        from app.workers.render_pipeline import execute_video_render
        await asyncio.to_thread(execute_video_render, task_id)
    finally:
        hb.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb


async def main() -> None:
    """Connect to temporal-server and run the worker (foreground)."""
    # Imported here so the Celery path never loads temporalio.
    from temporalio.client import Client
    from temporalio.worker import Worker, UnsandboxedWorkflowRunner

    # Client.connect raises (RPCError) if the server is unreachable — that makes
    # the process exit non-zero, satisfying the R19 "fail fast" scenario (no
    # silent Celery fallback). supervisord restarts per its policy.
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[VideoGenWorkflow],
        activities=[video_generation_activity],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    logger.info(
        "Temporal worker started (task_queue=%s, namespace=%s)",
        settings.temporal_task_queue,
        settings.temporal_namespace,
    )
    await worker.run()


if __name__ == "__main__":
    import sys

    try:
        asyncio.run(main())
    except Exception as exc:  # pragma: no cover - process entrypoint
        logger.error("Temporal worker failed to start: %s", exc)
        sys.exit(1)
