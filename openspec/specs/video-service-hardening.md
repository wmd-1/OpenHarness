# Video Service Security & Correctness Specification

**Component:** `service/` (HyperFrames FastAPI video generation service)
**Baseline plan:** `.qoder/plans/FastAPI_Hyperframes_Video_Service_3217f912.md`
**Established by change:** `harden-hyperframes-video-service` (2026-07-09)

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

#### Scenario: concurrent requests stay responsive during a large download
- GIVEN a 200 MB video being streamed
- WHEN another request hits the same uvicorn process
- THEN the other request is served without blocking on the file read

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

## Deprecated

(None)
