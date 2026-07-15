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

---

## 9. WS-C — Strict lease + fencing token (R20)

**Goal:** upgrade R8 from a heartbeat/TTL *heuristic* (which could in rare cases let a
reclaimed owner still write a terminal state or a valid artifact) to a **strict lease**: a
preempted owner can produce **no valid side effect** — neither a terminal state nor a stored
artifact.

### 9.1 Lease token semantics (authoritative)

`video_tasks.lease_token BIGINT NOT NULL DEFAULT 0`. It denotes *task execution ownership*,
not workflow/local retry. It changes ONLY on an ownership transfer:

- first `claim` → `1` (column default 0, so `lease_token = lease_token + 1` yields 1 — no `NULL + 1`);
- `reclaim` (beat declares owner dead, re-dispatches) → bumps once;
- a *second* `claim` by the re-dispatched new owner → bumps once more.

The same owner's local Celery retry or a Temporal Activity retry (same workflow instance) does
**NOT** bump the token — it keeps the same token, so the fence never rejects the owner's own
writes. Both Celery and Temporal paths use the single `claim()` bump rule.

### 9.2 Components

- **`tasks.claim(task_id, worker_id, celery_task_id=None) -> (claimed, token)`** — a single
  conditional UPDATE (`WHERE status IN (QUEUED, RETRYING) AND (worker_id IS NULL OR worker_id = :wid)`)
  with `lease_token = lease_token + 1` and `RETURNING lease_token`. The render pipeline holds
  the returned token in memory and registers it in the process-global `_active_tokens[task_id]`
  so the liveness loop fences heartbeats correctly.
- **Terminal-write guards** (`_mark_succeeded/_mark_failed/_mark_canceled`) — gain a `token`
  parameter; when provided, the `UPDATE` adds `WHERE lease_token = :token` alongside the existing
  `worker_id` guard (R9 defense-in-depth). `token=None` preserves the prior worker_id-only guard
  (backward compatible for direct unit calls); the real effectful path always passes it.
- **`recover_lost_tasks` reclaim flip** — adds `lease_token = lease_token + 1` so the preempted
  owner's subsequent writes are fenced *immediately* (before the new claim even runs).
- **`refresh_owned_heartbeats`** — gains a `tokens` map; when supplied, each refresh is guarded by
  `lease_token = :token`, so a reclaimed/stale owner's heartbeat affects 0 rows and it cannot be
  mistaken for alive. The liveness loop passes `tasks._active_tokens`.
- **Object-store artifact fence** — `storage.save(task_id, src, lease_token=...)`; S3 records
  `x-amz-meta-lease-token`, Local ignores it. The authoritative fence lives in
  **`tasks.fence_artifact`** + the `video_lease_fence` mapping table: a worker's save is accepted
  only if its `lease_token` is strictly higher than the currently accepted one. The winning
  token's `storage_key` is recorded; the terminal `_mark_succeeded` (guarded by `worker_id` +
  `lease_token`) is the final authority pointing `output_path` at the artifact. A stale owner
  therefore produces no referenced (valid) artifact.
- **Best-effort early discard (§4.5)** — before `save`, the pipeline re-reads `lease_token` from PG;
  if it no longer matches the held token, the artifact is discarded and the render aborts without
  a terminal write. This shrinks the window in which a stale owner could reach storage.

### 9.3 Gaps / residual edges (consistent with R20)

- The preempted owner may still *waste compute* rendering locally; the guarantee is that no valid
  duplicate terminal state or artifact survives.
- A fully lost lease (no successful renewal) is itself detected and triggers reclaim; the only
  residual race is a reclaim landing *after* the stale owner's PG re-read but *before* its
  `save` — covered by the `video_lease_fence` mapping-table check + terminal-state guard.

### 9.4 Testing

`tests/service/test_ws_c_fencing.py`: stale token terminal-write fence (all three `_mark_*`);
`fence_artifact` rejects stale/accepts newer token; pipeline discards artifact on reclaim (Redis
flap); stale heartbeat rejected. Plus updated `test_phase1_statemachine` (claim tuple) and
`test_worker` (QUEUED dispatch + `storage_for_kind` patch). Full suite green target.
