# Video Service Security & Correctness Specification

**Component:** `service/` (HyperFrames FastAPI video generation service)
**Baseline plan:** `.qoder/plans/FastAPI_Hyperframes_Video_Service_3217f912.md`
**Established by change:** `harden-hyperframes-video-service` (2026-07-09)
**Extended by change:** `scale-multi-instance` (2026-07-13) — MODIFY download (S3 302) + ADD R7–R13 (ownership/reclaim/object-storage/observability/horizontal-scaling/concurrency)

**Extended by change:** `phase3-multitenancy-temporal-lease` (2026-07-15) — ADD R14–R20 (multi-tenancy / API-key auth / quota / audit / rate-limit / pluggable Temporal scheduler / strict lease+fencing) + MODIFY R8 (upgraded from non-lease heartbeat to strict lease via `lease_token` fencing)

These are the source-of-truth invariants for the video service. They capture
behaviors the plan implied but the initial implementation violated, and are
enforced by the test suite under `tests/service/`.

---

## Requirements

### Requirement: `extra_oh_args` MUST be constrained by an allowlist

Forwarded `oh` CLI flags MUST be validated against a fixed allowlist of safe
`--flag value` pairs; safety-critical flags (`--permission-mode`, `--output`, and any
flag that changes execution trust or artifact location) MUST NOT be overridable by the
caller.

#### Scenario: safe flag passes through
- GIVEN a request with `extra_oh_args: ["--some-safe-flag", "value"]`
- WHEN the task is enqueued
- THEN the flag is forwarded to `oh` unchanged

#### Scenario: permission-mode override is rejected
- GIVEN a request with `extra_oh_args: ["--permission-mode", "not_full_auto"]`
- WHEN validation runs
- THEN the request is rejected (422) and `oh` is always invoked with `--permission-mode full_auto`

#### Scenario: output redirection is rejected
- GIVEN a request with `extra_oh_args: ["--output", "/evil/path"]`
- WHEN validation runs
- THEN the request is rejected (422)

---

### Requirement: Canceling a RUNNING task MUST terminate the `oh` process group and clean artifacts

A DELETE on a RUNNING task MUST send termination to the `oh` process group (not just the
Celery worker), remove the workspace and stored video, and the worker MUST NOT mark the
task SUCCEEDED afterward.

#### Scenario: DELETE RUNNING kills the generator
- GIVEN a task in `RUNNING` state with a live `oh` subprocess in its own session
- WHEN `DELETE /v1/videos/{id}` is called
- THEN the `oh` process group is killed, workspace + video files are removed, and the task is `CANCELED`

#### Scenario: worker does not overwrite a canceled task
- GIVEN a task was canceled while `oh` was still running
- WHEN `run_oh` eventually returns
- THEN the worker skips `_mark_succeeded` and leaves the task `CANCELED`

---

### Requirement: Video download MUST NOT block the event loop

`GET /v1/videos/{id}/file` MUST read the file using off-loop I/O
(`run_in_threadpool` / `aiofiles`) so large files do not stall other requests.
Additionally, when the task is stored in object storage (`storage_kind=s3`),
the endpoint MUST default to a `302` redirect to a presigned URL instead of
streaming through the API process.

#### Scenario: concurrent requests stay responsive during a large download
- GIVEN a 200 MB video being streamed
- WHEN another request hits the same uvicorn process
- THEN the other request is served without blocking on the file read

#### Scenario: default response redirects to presigned URL for S3 storage
- GIVEN a finished task with `storage_kind=s3`
- WHEN `GET /v1/videos/{id}/file` is requested without `?mode=`
- THEN the response is `302` with `Location: <presigned_url>`

#### Scenario: explicit stream mode falls back to streaming
- GIVEN a finished task with `storage_kind=s3`
- WHEN `GET /v1/videos/{id}/file?mode=stream` is requested
- THEN the response is `200` with a `StreamingResponse` (off-loop I/O)

#### Scenario: local storage falls back to streaming
- GIVEN a finished task with `storage_kind=local` (or `presigned_url` is `None`)
- WHEN `GET /v1/videos/{id}/file` is requested
- THEN the response is `200` streaming (no redirect)

---

### Requirement: Expired-task cleanup MUST run on a schedule

`cleanup_expired_tasks` MUST be invoked periodically by Celery beat (supervisord
`[program:beat]` or `beat_schedule`), not only defined.

#### Scenario: beat triggers cleanup
- GIVEN the service is deployed via supervisord
- WHEN the configured retention interval elapses
- THEN `cleanup_expired_tasks` runs and removes expired workspaces/videos/Redis logs

---

### Requirement: Log appending MUST reuse a connection pool

Worker log lines (`_append_log`) MUST use a shared Redis connection pool and avoid
per-line `ltrim`; truncation MUST be amortized.

#### Scenario: high-volume logs stay cheap
- GIVEN a task emitting thousands of stdout lines
- WHEN logs are appended
- THEN a bounded number of Redis connections is used and `ltrim` is not called per line

---

### Requirement: CORS MUST NOT pair wildcard origin with credentials

`allow_origins=["*"]` MUST NOT be combined with `allow_credentials=True`; use an explicit
origin list or disable credentials.

#### Scenario: credentials not reflected for arbitrary origin
- GIVEN CORS is configured
- WHEN a request arrives with an unknown `Origin` and credentials
- THEN the response does not echo that Origin with `Access-Control-Allow-Credentials: true`

---

### Requirement: R7 — Task ownership via atomic conditional UPDATE

Concurrent workers claiming the same task MUST be serialized by a single
atomic PostgreSQL conditional `UPDATE` (row lock) so that exactly one worker
becomes the owner (`worker_id` unique) for a given `task_id`.

#### Scenario: only one worker claims a queued task
- GIVEN two workers concurrently call `claim(task_id, worker_id)` for the same `queued` task
- WHEN each executes the conditional `UPDATE ... WHERE status IN ('queued','retrying') RETURNING id`
- THEN exactly one `UPDATE` hits the row, and `worker_id` is set to exactly one worker

#### Scenario: a running task is not re-claimed
- GIVEN a task already in `running` state
- WHEN another worker calls `claim` for it
- THEN the conditional `UPDATE` affects 0 rows (no second claim)

---

### Requirement: R8 — Strict lease via heartbeat + fencing token (was: non-lease)

A worker MUST register its liveness in Redis (`oh:worker:{worker_id}`, TTL 20s, refreshed every
10s) and refresh `video_tasks.heartbeat_at` every 10s, AND hold a monotonically increasing
`lease_token` (see R20). Reclaim SHOULD only fire when BOTH the worker registration is missing
AND the task heartbeat is stale (> 60s); on reclaim the `lease_token` is atomically incremented
so the preempted owner's subsequent writes are fenced (R20).

> **NOTE (strict guarantee, upgraded from Phase 2):** Phase 2 used heartbeat + TTL as a
> *non-lease* mechanism that significantly reduced double-run but did not prove "never
> double-run" (§11.7 residual risk). Phase 3 adds `lease_token` fencing so a preempted owner
> can produce **no valid side effect** — terminal state AND object-storage artifact are both
> fenced. Under normal operation this is a strict "never double-run"; the only residual edge is
> a fully lost lease (no successful renewal) which is itself detected and triggers reclaim.

#### Scenario: alive worker is not reclaimed (normal Redis)
- GIVEN a worker process is alive and refreshes `oh:worker:{worker_id}` every 10s (Redis available)
- WHEN beat scans a `running` task whose `heartbeat_at` is stale
- THEN the task is NOT reclaimed (registration key present ⇒ owner judged alive)

#### Scenario: reclaim bumps lease_token (fencing setup)
- GIVEN an owner worker is dead (registration missing) and `heartbeat_at` is stale
- WHEN beat reclaims the task
- THEN `lease_token` is incremented and the new owner holds the higher token

---

### Requirement: R9 — Idempotent reclaim + terminal-state guard

`recover_lost_tasks` reclaim MUST be idempotent (row-lock conditional `UPDATE`,
multiple beats cannot double-flip or double-re-enqueue). Terminal-state writes
MUST be guarded so a non-current owner cannot overwrite the result.

#### Scenario: only one beat reclaims and re-enqueues
- GIVEN an owner worker is dead (registration missing) and `heartbeat_at` is stale
- WHEN multiple beats concurrently run `recover_lost_tasks`
- THEN exactly one flips `running→retrying` and re-enqueues exactly once (row-lock idempotent, strong guarantee)

#### Scenario: stale owner cannot overwrite terminal state
- GIVEN a task has been reclaimed / owner changed
- WHEN the old `worker_id` later attempts `_mark_succeeded`
- THEN the `UPDATE ... WHERE status='running' AND worker_id=:current_wid` affects 0 rows, and the terminal state is NOT overwritten (success guard, does not depend on Redis, strong guarantee)

---

### Requirement: R10 — Object storage abstraction with presigned URLs

`VideoStorage` MUST support `presigned_url(key, expires) -> str | None`. A new
`S3VideoStorage` MUST implement `save`/`open`/`delete`/`exists`/`presigned_url`;
`LocalVideoStorage.presigned_url` MUST return `None` (caller falls back to streaming).

#### Scenario: S3 storage implements all methods
- GIVEN `S3VideoStorage`
- WHEN `delete` / `exists` / `presigned_url` are called
- THEN all are implemented (no `AttributeError` from `cleanup_expired_tasks`)

#### Scenario: local storage returns None for presigned
- GIVEN `LocalVideoStorage`
- WHEN `presigned_url` is called
- THEN it returns `None` and the API falls back to streaming

---

### Requirement: R11 — Observability and readiness probe

The service MUST expose metrics (Prometheus), traces (OpenTelemetry, optional),
structured logs (structlog JSON with `task_id`/`worker_id`), and a `/readyz`
endpoint reporting queue-consumption status. `/healthz` SHOULD include an S3 ping
when `storage_kind=s3`.

#### Scenario: readiness probe reports consumption status
- GIVEN the service is running
- WHEN `GET /readyz` is called
- THEN it returns queue-consumption status (e.g., stale-heartbeat / pending counts)

#### Scenario: health probe reflects S3 degradation
- GIVEN `storage_kind=s3`
- WHEN `GET /healthz` is called and S3 is unreachable
- THEN the probe reflects S3 degradation without being fatal to core API

---

### Requirement: R12 — Horizontal scaling

The deployment MUST support `api×N` + `worker×M` replicas. Task distribution
across replicas MUST be safe (no loss, no double-run under normal operation),
using `--scale` (non-swarm) for replica counts.

#### Scenario: concurrent load across replicas
- GIVEN `docker compose ... --scale worker=N --scale api=M`
- WHEN 100 concurrent submissions are made
- THEN tasks are safely consumed by multiple workers, with no loss; under normal
  operation no double-run; under anomalous conditions (§11.7) the worst case is a
  brief duplicate render whose terminal state is still not overwritten by a stale owner.

---

### Requirement: R13 — Worker concurrency control (queue tiering + global semaphore)

The worker MUST constrain in-instance concurrency to protect downstream
resources (Chrome / ffmpeg memory). Tasks SHOULD be routed to priority-tiered
Celery queues by `priority`, and a global concurrency semaphore
(`MAX_CONCURRENT_RENDERS`) MUST cap the number of simultaneously running `oh`
render processes so a single replica does not OOM under load.

#### Scenario: render concurrency stays within the semaphore cap
- GIVEN `MAX_CONCURRENT_RENDERS = K`
- WHEN more than K tasks are submitted concurrently to a single replica
- THEN at most K `oh` render processes run at once; the remainder wait in queue and start as slots free, without OOM

#### Scenario: higher-priority tasks are consumed first
- GIVEN tasks with mixed `priority` values routed to tiered queues
- WHEN workers drain the queues
- THEN higher-priority tasks are picked up before lower-priority ones

---

### Requirement: R14 — Tenant isolation

All video-task operations (list / get / create / cancel / delete / download) MUST be scoped
to the caller's `tenant_id`. `video_tasks` MUST carry a `tenant_id` column. A request MUST
NOT be able to read or mutate a task owned by a different tenant; cross-tenant access MUST be
rejected with `403` (or `404` to avoid revealing existence).

#### Scenario: cross-tenant task is invisible
- GIVEN tenant A owns task `t1` and tenant B presents a valid API key
- WHEN tenant B calls `GET /v1/videos/t1`
- THEN the response is `403` (or `404`) and no task data is returned

#### Scenario: cross-tenant delete is rejected
- GIVEN tenant A owns task `t1`
- WHEN tenant B calls `DELETE /v1/videos/t1`
- THEN the response is `403` and the task is unchanged

#### Scenario: cross-tenant list is scoped
- GIVEN tenant A owns `t1` and tenant B owns `t2`
- WHEN tenant B calls `GET /v1/videos` (list)
- THEN the response contains only tenant B's tasks (no `t1`) and no task data from other tenants

> **Worker async path:** HTTP requests set `app.current_tenant` via middleware, but workers have
> no request context. After a worker `claim`s / reads a task, it MUST issue `SET LOCAL
> app.current_tenant = :task.tenant_id` (the `tenant_id` carried on the claimed task row itself,
> never a global) on its DB connection before any access (terminal write, audit_log, artifact
> metadata). This keeps RLS valid on the async execution path; the same binding applies if the
> fallback "centralized query layer" is used instead of RLS.

---

### Requirement: R15 — API Key authentication

Mutating and reading endpoints MUST require a valid API key via the `X-API-Key` header. The
key is resolved (by hashed lookup) to a `tenant_id`. A missing, invalid, revoked, or expired
key MUST be rejected with `401`. Internal service-to-service calls MAY use a trusted header to
assume `tenant_id=system`.

#### Scenario: missing key is rejected
- GIVEN a request with no `X-API-Key`
- WHEN any protected endpoint is called
- THEN the response is `401`

#### Scenario: revoked key is rejected
- GIVEN an API key marked `revoked`
- WHEN a request presents it
- THEN the response is `401`

---

### Requirement: R16 — Per-tenant quota

Each tenant MUST have a configured quota (`max_concurrent` running+pending tasks,
`daily_submit_limit`). A submission that would exceed either limit MUST be rejected with `429`.

#### Scenario: concurrent quota exceeded
- GIVEN tenant T already has `max_concurrent` tasks in running/pending
- WHEN T submits a new task
- THEN the response is `429` and no task is created

#### Scenario: daily submit limit exceeded
- GIVEN tenant T has already submitted `daily_submit_limit` tasks today
- WHEN T submits another
- THEN the response is `429`

---

### Requirement: R17 — Audit logging

Every mutating operation (create / cancel / delete / terminal-state transition) MUST emit an
audit record: `tenant_id`, `actor_key_id`, `action`, `target_type`, `target_id`, `ts`,
`meta_json`. Audit writes SHOULD be asynchronous to avoid slowing the primary path.

#### Scenario: cancel is audited
- GIVEN a valid request cancels task `t1`
- WHEN the cancel completes
- THEN an `audit_log` row exists for action=`cancel`, target=`t1`, with the caller's tenant/key

---

### Requirement: R18 — Per-tenant rate limiting

The API MUST enforce a per-tenant request rate limit (`rate_per_min` from `quotas`). A tenant
exceeding its rate MUST receive `429`.

#### Scenario: rate limit tripped
- GIVEN tenant T's `rate_per_min` is N
- WHEN T issues > N requests within the window
- THEN the excess requests receive `429`

---

### Requirement: R19 — Pluggable scheduler with working Temporal backend

The scheduler MUST be pluggable via `OH_SCHEDULER_BACKEND`. `CeleryScheduler` remains the default.
`TemporalScheduler` MUST be a real implementation: with `OH_SCHEDULER_BACKEND=temporal` and a
reachable `temporal-server`, task enqueue/cancel/retry MUST execute through a Temporal workflow
(`VideoGenWorkflow` + `VideoGenerationActivity` with activity heartbeat and declarative retry
policy). If `temporal-server` is unreachable, startup MUST fail explicitly rather than silently
fall back to Celery.

#### Scenario: temporal backend enqueues via workflow
- GIVEN `OH_SCHEDULER_BACKEND=temporal` and a running `temporal-server`
- WHEN a task is submitted
- THEN it executes via the Temporal workflow/activity (not Celery)

#### Scenario: unreachable temporal fails fast
- GIVEN `OH_SCHEDULER_BACKEND=temporal` but no `temporal-server`
- WHEN the service starts
- THEN startup fails with a clear error (no silent Celery fallback)

---

### Requirement: R20 — Strict lease with fencing token (no valid duplicate side effect, including storage)

`video_tasks` MUST carry a `lease_token`. `claim` / `reclaim` MUST atomically increment
`lease_token` and the owning worker MUST hold the current token in memory. Every effectful
write MUST carry the current token and be rejected if stale:
- DB terminal-state write: guard `WHERE worker_id=:wid AND lease_token=:token` (stale token → 0 rows).
  This is **defense-in-depth**: Phase 2's `recover_lost_tasks` already nulls `worker_id` and
  re-dispatches on reclaim, so the prior `WHERE status=RUNNING AND worker_id=:wid` guard (R9)
  already blocks a stale owner at the DB layer. The `lease_token` guard adds a second,
  token-explicit check.
- Object-storage artifact write (the **primary new guarantee** of this requirement): the write
  MUST be fenced by token (e.g., `x-amz-meta-lease-token` compared via an intermediate map; a
  stale-token write is discarded, producing no valid artifact). R9 does NOT cover the artifact
  store, so this is the genuine fix for "double-run that lands on disk".

> **Lease semantics (authoritative):** `lease_token` denotes *task execution ownership*, not
> workflow/local retry. It changes ONLY on ownership transfer — first `claim` (new owner) or
> `reclaim` (owner declared dead, redispatched). The same owner's local retry or a Temporal
> Activity retry (same workflow instance) does NOT bump the token; it keeps the same token, so
> the fence never rejects the owner's own writes. Both Celery and Temporal paths MUST follow this
> single bump rule. Migration sets `lease_token BIGINT NOT NULL DEFAULT 0`; first claim yields
> `1`, avoiding any `NULL + 1` ambiguity.

This upgrades R8 from a heartbeat/TTL (non-lease) mechanism to a strict lease: a preempted owner
can produce **NO valid side effect** (neither terminal state nor stored artifact). Note: the
preempted owner may still *waste compute* rendering locally; the guarantee is that no valid
duplicate terminal state or artifact survives.

#### Scenario: stale owner cannot write terminal state (defense-in-depth)
- GIVEN task reclaimed (lease_token bumped) to a new owner
- WHEN the old owner later attempts `_mark_succeeded` with its stale token
- THEN the guarded `UPDATE ... WHERE lease_token=:old_token` affects 0 rows; terminal state unchanged
- AND the same holds for `_mark_failed` / `_mark_canceled` (all three terminal writes carry the token)

#### Scenario: stale owner cannot produce a valid artifact
- GIVEN task reclaimed to a new owner
- WHEN the old owner finishes rendering and attempts to `save` the artifact with its stale token
- THEN the storage write is fenced (discarded); the stored artifact belongs to the new token only

#### Scenario: Redis flap does not yield a valid duplicate
- GIVEN a worker is alive but its Redis registration briefly drops (false reclaim)
- WHEN the preempted owner later writes with its (now stale) token
- THEN all its writes are fenced; no valid duplicate terminal state or artifact survives

#### Scenario: stale owner heartbeat is rejected (prevents false-alive)
- GIVEN task reclaimed (lease_token bumped) to a new owner
- WHEN the old (preempted) owner issues a heartbeat with its stale token
- THEN the guarded heartbeat `UPDATE ... WHERE lease_token=:old_token` affects 0 rows
- AND beat still reclaims on stale `heartbeat_at` (the old owner is not mistaken for alive)

---

## Deprecated

(None)
