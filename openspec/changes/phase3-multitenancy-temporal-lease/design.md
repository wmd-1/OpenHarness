# Design: WS-B — Real Temporal Migration (Phase 3)

**Change:** `phase3-multitenancy-temporal-lease`
**Scope:** WS-B only. Celery stays default; Temporal is opt-in via `OH_SCHEDULER_BACKEND=temporal`.

---

## 1. Goal

Replace the `TemporalScheduler` placeholder (currently `raise NotImplementedError`) with a
real Temporal backend so that, when `OH_SCHEDULER_BACKEND=temporal` and a reachable
`temporal-server` is present, task enqueue / cancel / retry execute through a Temporal
workflow (`VideoGenWorkflow` + `VideoGenerationActivity`) with activity heartbeats and a
declarative retry policy. Celery remains the default backend and its behavior is unchanged.

This satisfies **R19** (pluggable scheduler with working Temporal backend) and the
"temporal backend enqueues via workflow" + "unreachable temporal fails fast" scenarios.

## 2. Architecture

```
                         OH_SCHEDULER_BACKEND
                                  │
              ┌───────────────────┴───────────────────┐
           "celery"                                 "temporal"
              │                                          │
     CeleryScheduler                       TemporalScheduler (real)
     enqueue→ broker                       enqueue→ client.start_workflow(
     cancel → broker revoke                            VideoGenWorkflow, id=...,
                                                         task_queue=video-gen)
                                                   cancel → handle.cancel()

   celery worker runs                     temporal-worker process runs
   generate_video_task                     Worker(client, video-gen,
                                                 workflows=[VideoGenWorkflow],
   (render body)                           activities=[VideoGenerationActivity])
        │                                          │
        └──────────► shared ◄──────────────────────┘
              execute_video_render(task_id)
              (claim → run_oh → persist → abort check)
```

### 2.1 Shared render pipeline (single source of truth)

The Celery task body and the Temporal Activity must run **identical** render logic.
Extract the render body of `generate_video_task` into a standalone callable:

```python
# app/workers/render_pipeline.py
def execute_video_render(task_id: str) -> None:
    """Synchronous render pipeline shared by Celery task and Temporal Activity.

    Mirrors the current `generate_video_task` body 1:1: claim ownership,
    run_oh, persist terminal state / artifact / log tail, honor abort key.
    """
    ...  # same code as today's generate_video_task (minus the @task decorator)
```

- `tasks.generate_video_task` becomes a thin Celery wrapper: `execute_video_render(task_id)`.
- `VideoGenerationActivity.run` becomes: `execute_video_render(task_id)` with heartbeat
  wrapping `run_oh`'s `on_log_line`.

This keeps one implementation of the DB guards (`claim`, `_mark_*`, `_abort_requested`,
`_append_log`, `render_semaphore`) for both backends — no drift.

### 2.2 Workflow / Activity contract

```python
# app/workers/temporal_worker.py
from temporalio import workflow, activity
from temporalio.client import Client, WorkflowHandle
from temporalio.worker import Worker
from datetime import timedelta
from app.config import settings
from app.workers.render_pipeline import execute_video_render

@workflow.defn(name="VideoGenWorkflow")
class VideoGenWorkflow:
    @workflow.run
    async def run(self, task_id: str) -> None:
        await workflow.execute_activity(
            VideoGenerationActivity.run,
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
class VideoGenerationActivity:
    async def run(self, task_id: str) -> None:
        # Heartbeat on each log line so a dead worker is detected within
        # heartbeat_timeout and the activity is retried (not silently stuck).
        def _on_line(line: str) -> None:
            activity.heartbeat({"task_id": task_id, "line": line[:200]})
        # run_oh is synchronous; run it in a thread so we can heartbeat.
        await asyncio.to_thread(execute_video_render_heartbeating, task_id, _on_line)
```

Notes:
- `run_oh` is a blocking subprocess call. To keep heartbeating alive we run it in a thread
  (`asyncio.to_thread`) and feed `on_log_line` from that thread into `activity.heartbeat`.
  (Heartbeat is thread-safe in the Temporal SDK; if a threading concern arises we fall back
  to a watchdog thread calling `activity.heartbeat` on a timer — see §4.)
- The workflow id is deterministic: `video-gen-{task_id}`. `TemporalScheduler.cancel` maps
  the enqueue-returned workflow id back to `client.get_workflow_handle(workflow_id).cancel()`.
- Cancellation signal: keep the Redis `oh:abort:{task_id}` key (cross-replica safe). The
  activity reads it via `execute_video_render`'s existing `_abort_requested` and `run_oh`
  terminates the `oh` process group on abort. Temporal `handle.cancel()` additionally
  requests workflow cancellation.

## 3. Scheduler wiring

```python
# app/workers/scheduler.py (TemporalScheduler real)
class TemporalScheduler:
    backend = "temporal"
    _client: Client | None = None

    def _get_client(self) -> Client:
        if self._client is None:
            # Client.connect is async; enqueue/cancel are async in the API path.
            ...
        return self._client

    async def enqueue(self, task_id, *, priority=5) -> str:
        client = await self._get_client()
        handle = await client.start_workflow(
            VideoGenWorkflow.run, task_id,
            id=f"video-gen-{task_id}",
            task_queue=settings.temporal_task_queue,
        )
        return handle.id

    async def cancel(self, workflow_id: str) -> None:
        handle = self._get_client().get_workflow_handle(workflow_id)
        await handle.cancel()
```

`videos.py` calls `get_scheduler().enqueue(...)` (already async-friendly in the API path —
`create_video` already `await`s the scheduler). The current `create_video` does
`await get_scheduler().enqueue(...)`? Verify: in `create_video`, the call was
`get_scheduler().enqueue(...)` (sync) earlier. **Action:** make `create_video` `await` it and
have `CeleryScheduler.enqueue` stay sync (it's fine to await a sync function-returning coroutine
wrapper, or make `Scheduler.enqueue` return `Awaitable[str]` and have Celery's return the id
directly). Decision: `Scheduler.enqueue`/`cancel` become **async**; `CeleryScheduler.enqueue`
returns `async def ... return async_result.id` (trivial). Minimal change in `create_video`.

## 4. Heartbeat threading approach

`run_oh` blocks the calling thread and invokes `on_log_line` synchronously per line. The
Temporal Activity must heartbeat on a timer even between log lines (so a hung `oh` is still
detected). Two options:
- **(A) `asyncio.to_thread` + per-line heartbeat** — simple; heartbeat only fires when a log
  line arrives. If `oh` hangs without output, no heartbeat → activity times out via
  `heartbeat_timeout` anyway (timeout still triggers). Acceptable.
- **(B) watchdog thread** — a background thread calls `activity.heartbeat` every ~10s
  regardless of log output. More robust; slightly more code.

**Chosen: (B)** — a heartbeat watchdog thread, because `heartbeat_timeout=30s` should be
satisfied even during silent `oh` phases (e.g. large Chrome render with no stdout). The
watchdog is stopped when `execute_video_render` returns.

## 5. Fail-fast on unreachable Temporal (R19 scenario)

- `temporal_worker.py`: `Client.connect(...)` at process start; on `temporalio.service.RPCError`
  / connection failure → `sys.exit(1)` (supervisord restarts per policy; but the *requirement*
  is explicit failure, not silent Celery fallback — satisfied because we never fall back).
- `app/main.py`: add `@app.on_event("startup")` (or lifespan) that, when
  `settings.scheduler_backend == "temporal"`, attempts
  `await temporalio.client.Client.connect(host, namespace=...)` with a short timeout; on
  failure raises so the API container fails to start. When backend is `celery`, no Temporal
  code runs at all.

## 6. Deployment

`docker-compose.temporal.yml` (extends base `docker-compose.yml`):
- `temporal` service: `temporalio/auto-setup:latest` (+ optional `temporalio/ui`).
- `openharness` override: `OH_SCHEDULER_BACKEND=temporal`, and supervisord switched to
  `docker/supervisord.temporal.conf` which runs `api` + `temporal-worker`
  (`python -m app.workers.temporal_worker`) and **not** `worker`/`beat` (the Temporal worker
  owns execution; reclaim/watch-dog abstraction for WS-C is future work and out of WS-B scope).
- Celery path unchanged: default `docker-compose.yml` still runs `api`+`worker`+`beat`.

## 7. Testing strategy (sandbox-safe + docker/CI split)

Sandbox has **no temporal-server** binary. To keep `pytest tests/service` green without a
server:

1. **ActivityEnvironment unit test** — `temporalio.testing.ActivityEnvironment` runs an
   activity *without* a server. Patch `run_oh` (and point DB at sqlite) then:
   `await ActivityEnvironment().run(VideoGenerationActivity.run, task_id)`. Asserts terminal
   state / artifact / log written. This exercises the real Activity + shared pipeline code.
2. **Scheduler routing + fail-fast** — `get_scheduler()` returns the right class per
   `scheduler_backend`; `TemporalScheduler.enqueue` against an unreachable server raises a
   clear error (proves fail-fast wiring without needing a live server).
3. **Full e2e** (start temporal-server → enqueue/cancel through Temporal) is marked
   `pytest.mark.docker` / skipped without `TEMPORAL_SERVER_URL`, validated in the
   `docker-compose.temporal.yml` stack + CI. Same DEFERRED convention as Phase 2 e2e.

## 8. Out of scope (WS-B)

- Temporal cluster HA, complex multi-Activity topologies, scheduler hot-swap.
- WS-C lease/fencing coupling (reclaim/watch-dog as backend-agnostic) — tracked in Phase 4.
- Temporal as the cancellation *sole* source of truth — Redis abort key retained.
