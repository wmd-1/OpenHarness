# Video Service Security & Correctness Specification

**Component:** `service/` (HyperFrames FastAPI video generation service)
**Baseline plan:** `.qoder/plans/FastAPI_Hyperframes_Video_Service_3217f912.md`
**Established by change:** `harden-hyperframes-video-service` (2026-07-09)
**Extended by change:** `scale-multi-instance` (2026-07-13) — MODIFY download (S3 302) + ADD R7–R13 (ownership/reclaim/object-storage/observability/horizontal-scaling/concurrency)

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

### Requirement: R8 — Worker liveness via heartbeat registration (non-lease)

A worker SHOULD register its liveness in Redis (`oh:worker:{worker_id}`, TTL 20s,
refreshed every 10s) and refresh `video_tasks.heartbeat_at` every 10s. Reclaim
SHOULD only fire when BOTH the worker registration is missing AND the task
heartbeat is stale (> 60s).

> **NOTE (not a hard guarantee):** This is a heartbeat + TTL mechanism, **not** a
> strict distributed lease/fencing. It significantly reduces double-execution risk
> and (with R9) reliably prevents terminal-state clobber, but does **not** prove
> "never double-run". Under Redis network partition, process long GC/STW pause, or
> Redis failover losing keys (design source §11.7 B1–B4), the registration key may
> disappear while the process is alive, causing a false reclaim and brief double
> render. This residual risk is accepted for Phase 2 and is NOT asserted as a gate.

#### Scenario: alive worker is not reclaimed (normal Redis)
- GIVEN a worker process is alive and refreshes `oh:worker:{worker_id}` every 10s (Redis available)
- WHEN beat scans a `running` task whose `heartbeat_at` is stale
- THEN the task is NOT reclaimed (registration key present ⇒ owner judged alive)

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

## Deprecated

(None)
