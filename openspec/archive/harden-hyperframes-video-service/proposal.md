# Proposal: Harden HyperFrames FastAPI Video Service

**Change ID:** `harden-hyperframes-video-service`
**Created:** 2026-07-09
**Status:** Draft
**Reviewer:** WorkBuddy — openspec + superpowers review
**Baseline:** `.qoder/plans/FastAPI_Hyperframes_Video_Service_3217f912.md` (plan) vs `service/` implementation

---

## Problem Statement

A code review was performed comparing the implementation under `service/` (and its
deployment/test scaffolding) against the approved plan
`.qoder/plans/FastAPI_Hyperframes_Video_Service_3217f912.md`.

The implementation's **structure** matches the plan (§2 directory, §3 models, §4
endpoints, §5 worker, §6 parser, §7 storage, §9 SSE, §10–11 Docker, §14 tests), and
the SSE Redis key naming is consistent across worker and API. **However**, several
behaviors deviate from the plan or carry production-grade security/robustness defects.

All 14 findings below were **independently re-verified** against the actual source on
2026-07-09 (line references captured in `tasks.md`). This proposal captures them as
tracked fix items. No implementation code is modified by this proposal.

### Severity summary
- 🔴 Must-fix (3): `extra_oh_args` no whitelist (security), RUNNING-cancel not effective
  (orphan process + status overwrite), blocking sync read in async streaming.
- 🟠 Should-fix (4): beat cleanup never scheduled, per-line Redis reconnect + `ltrim`,
  alembic async/sync driver mismatch, CORS `*` + credentials.
- 🟡 Nice-to-fix (7): `Accept-Ranges` without `Range`, idempotency race → 500,
  deterministic failure still `raise`, test-coverage gaps, unused `ffmpeg-python` dep,
  SSE replay duplicate, cleanup leaves stale `output_path`.

## Proposed Solution

Group the fixes into three phases (see `tasks.md`):

1. **Security & correctness (must-fix)** — validate/whitelist `extra_oh_args` at the
   schema/runner boundary; make RUNNING cancellation kill the `oh` process group via
   `os.killpg` + clean disk artifacts + guard the worker against overwriting a canceled
   task; move the file stream read off the event loop (`run_in_threadpool` / `aiofiles`).
2. **Reliability & ops (should-fix)** — schedule `cleanup_expired_tasks` via Celery beat
   (supervisord `[program:beat]` or `beat_schedule`); reuse a module-level Redis pool and
   batch `rpush` in `_append_log`; unify alembic on `postgresql+asyncpg://`; restrict CORS
   origins (or drop `allow_credentials`).
3. **Polish (nice-to-fix)** — implement real `Range`/206 or drop the `Accept-Ranges`
   header; make idempotency conflict return the existing task; only `TransientError`
   retries; extend tests to cover `runner.run_oh` mocking, SSE, real 200 stream, and a
   Postgres-native run; remove unused dep; fix SSE replay window; null `output_path` on
   cleanup.

## Scope

### In Scope
- `service/app/schemas.py`, `service/app/workers/runner.py`, `service/app/routers/videos.py`
- `service/app/workers/tasks.py`, `service/app/workers/celery_app.py`
- `service/alembic/env.py`, `service/app/main.py`
- `service/docker/../supervisord.conf` (repo: `docker/supervisord.conf`),
  `docker-compose.yml`, `service/pyproject.toml`
- `tests/service/*`

### Out of Scope
- HyperFrames / `oh` CLI internals.
- Replacing `LocalVideoStorage` with S3 (plan §15, future).
- Auth gateway / OAuth2 (plan §15, future).

## Impact Analysis

| Component | Change Required | Details |
|-----------|-----------------|---------|
| Security  | Yes | `extra_oh_args` whitelist; CORS restriction |
| Worker    | Yes | process-group kill on cancel; cancellation guard; `_append_log` pooling |
| API       | Yes | async streaming; idempotency conflict handling; `Range` |
| SSE       | Yes | replay-window fix |
| Deploy    | Yes | beat program in supervisord |
| DB/Migration | Yes | alembic async driver |
| Tests     | Yes | worker/SSE/stream/Postgres coverage |

## Architecture Considerations

- The plan already isolates the worker behind `app/workers/tasks.py` and the process
  spawn behind `runner.run_oh`; the fixes slot into those seams without restructuring.
- Celery `acks_late=True` means a re-raised non-`TransientError` is NOT retried; the
  cancellation guard must check status **after** `run_oh` returns, before marking success.
- Process-group kill must mirror the existing `setsid`/`killpg` timeout logic in
  `runner.py` (already correct for timeout — extend it for cancellation).

## Success Criteria

- [ ] `extra_oh_args` rejected/normalized against a strict allowlist; `--permission-mode`
      cannot be overridden.
- [ ] DELETE on RUNNING kills the `oh` process group, removes disk artifacts, and the
      worker never marks a canceled task SUCCEEDED.
- [ ] `GET /{id}/file` streams without blocking the uvicorn event loop.
- [ ] `cleanup_expired_tasks` fires on a real schedule (beat).
- [ ] `_append_log` uses one pooled Redis connection; `ltrim` no longer per-line.
- [ ] `alembic upgrade head` runs cleanly on `postgresql+asyncpg://`.
- [ ] CORS does not combine `*` with credentials.
- [ ] Test suite covers worker execution, SSE, 200-stream, and a Postgres-native run.

## Risks & Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Whitelist too strict breaks legitimate `oh` flags | Med | Low | Start with a documented allowlist + passthrough for safe value-flags; log rejected args |
| `killpg` on cancel races with normal completion | Low | Med | Worker re-checks `status==CANCELED` after `run_oh` returns before `_mark_succeeded` |
| Beat double-runs cleanup across scaled replicas | Low | Low | Query is idempotent (files may already be gone); safe to no-op |
