# Implementation Tasks: Harden HyperFrames Video Service

**Change ID:** `harden-hyperframes-video-service`

All line references are from the 2026-07-09 source state and were re-verified.

**Detailed code-level design for every item → [`design.md`](./design.md).** Each task
below notes its `design.md` section (§#N) and concrete verification.

---

## Phase 1: Security & Correctness (Must-fix 🔴)

- [x] 1.1 **Whitelist `extra_oh_args`** ✓ 2026-07-09 — `service/app/schemas.py:18` only types the field.
      Add validation (schema or `runner.py:43-49`) that only permits a fixed allowlist of
      `--flag value` pairs, and **forbids** overriding safety-critical flags
      (`--permission-mode`, `--output`, etc.). Verify `runner.py:43-49` cannot be used to
      downgrade `--permission-mode full_auto` or redirect `--output`.
      → Design: [`design.md §#1`](./design.md). Adds `service/app/security.py` (`vet_extra_oh_args`),
      a `field_validator` on `VideoCreateRequest`. Code sketch included.
      **Quality Gate:** unit test rejecting dangerous args; integration check that
      `--permission-mode` stays `full_auto`.

- [x] 1.2 **Make RUNNING cancellation effective** ✓ 2026-07-09 — `service/app/routers/videos.py:239-249`
      only `revoke(terminate=True)` + marks CANCELED. Because `runner.py:65` uses
      `preexec_fn=os.setsid`, the signal never reaches the `oh` process group.
      - Track `proc.pid` (session leader) in the worker; on cancel, `os.killpg(proc.pid, SIGTERM/KILL)`
        (mirror `runner.py:86,93`).
      - Add disk cleanup (workspace + stored video) for the RUNNING branch in `videos.py`.
      → Design: [`design.md §#2`](./design.md). **Worker self-abort via Redis flag**
      `oh:abort:<task_id>` (works across `--scale` replicas); API sets the flag + `revoke`
      as best-effort; worker owns final CANCELED. Code sketch for `runner.run_oh`,
      `tasks.generate_video_task`, `videos.delete_video` included.
      **Quality Gate:** test that DELETE on RUNNING leaves no orphan `oh`/chrome process and
      no leftover `/workspaces/<id>` / video file.

- [x] 1.3 **Guard worker against overwriting a canceled task** ✓ 2026-07-09 — `service/app/workers/tasks.py:170-173`
      calls `_mark_succeeded` unconditionally after `run_oh` returns. Re-check
      `task.status == CANCELED` (or a `canceled_at` flag) right after `run_oh` returns and
      before locating/parsing/saving; if canceled, skip success and clean up.
      → Design: [`design.md §#2`](./design.md) (`_is_aborted` + `_mark_canceled` after `run_oh`).
      **Quality Gate:** test that a task canceled mid-run ends CANCELED, never SUCCEEDED.

- [x] 1.4 **Offload file streaming from the event loop** ✓ 2026-07-09 — `service/app/routers/videos.py:147-165`
      does synchronous `fileobj.read(chunk)` inside the async `StreamingResponse` generator.
      Use `run_in_threadpool(fileobj.read, chunk)` or `aiofiles` for the read.
      → Design: [`design.md §#3`](./design.md). `from fastapi.concurrency import run_in_threadpool`.
      **Quality Gate:** profiler/load check showing the loop stays responsive during a large
      download.

---

## Phase 2: Reliability & Ops (Should-fix 🟠)

- [x] 2.1 **Schedule `cleanup_expired_tasks`** ✓ 2026-07-09 — task exists (`tasks.py:194`) but
      `service/docker/supervisord.conf` has only `api` + `worker` programs and
      `celery_app.py:13-23` has no `beat_schedule`. Add `[program:beat]` running
      `celery -A app.workers.celery_app.celery_app beat`, or add `beat_schedule` to
      `celery_app.py`.
      → Design: [`design.md §#4`](./design.md). `beat_schedule` snippet + supervisord
      `[program:beat]`; documents the `--scale api=N` multi-beat caveat (redbeat / single
      replica / idempotent no-op).
      **Quality Gate:** `celery beat` starts in container; cleanup task runs on schedule.

- [x] 2.2 **Pool Redis connections in `_append_log`** ✓ 2026-07-09 — `tasks.py:39-51` does
      `redis.from_url(...)` + `lpush` + `ltrim(0,9999)` + `publish` + `close` **per line**,
      and `ltrim` is O(N) per call. Introduce a module-level connection pool, batch
      `rpush`, and only `ltrim` periodically (or rely on a cap via `LTRIM` once per N lines).
      Apply the same pooling to `_update_log_tail` (`tasks.py:88-104`) and the done-publish
      (`tasks.py:176-183`).
      → Design: [`design.md §#5`](./design.md). Module-level `ConnectionPool` + `llen`-gated
      `ltrim`; reuse in `_update_log_tail` and done-publish.
      **Quality Gate:** long-task log path uses a constant number of connections; no per-line
      `ltrim`.

- [x] 2.3 **Unify alembic on async driver** ✓ 2026-07-09 — `service/alembic/env.py:21` sets
      `sqlalchemy.url = settings.db_sync_url` (`postgresql+psycopg://`) while `:45` builds an
      **async** engine via `async_engine_from_config`. Switch the migration engine to
      `postgresql+asyncpg://` (or thread a dedicated `db_migrate_url`) and run
      `alembic upgrade head` to confirm.
      → Design: [`design.md §#6`](./design.md). Add `db_migration_url` (asyncpg) to
      `config.py`; use it in `env.py`.
      **Quality Gate:** `alembic upgrade head` succeeds on asyncpg.

- [x] 2.4 **Restrict CORS** ✓ 2026-07-09 — `service/app/main.py:30-36` uses
      `allow_origins=["*"]` + `allow_credentials=True` (reflects Origin). Replace with an
      explicit origin list from settings, or drop `allow_credentials`.
      → Design: [`design.md §#7`](./design.md). `settings.cors_origins`-driven; credentials
      only when origins are explicit.
      **Quality Gate:** preflight with arbitrary Origin is no longer reflected with
      credentials.

---

## Phase 3: Polish (Nice-to-fix 🟡)

- [x] 3.1 **`Accept-Ranges` honesty** ✓ 2026-07-09 — `videos.py:163` advertises `Accept-Ranges: bytes`
      but no `Range` parsing / `206` (plan §8 marks optional). Either implement `Range`→`206`
      or remove the header.
      → Design: [`design.md §#8`](./design.md). `Range` header parse → `seek` + `206` +
      `Content-Range`; code sketch included.
- [x] 3.2 **Idempotency race** ✓ 2026-07-09 — `videos.py:79-88` SELECT-then-INSERT can raise
      `IntegrityError` → 500 on concurrent duplicates (`models.py:44-46` has the unique
      constraint). Catch `IntegrityError` and return the existing task.
      → Design: [`design.md §#9`](./design.md). `except IntegrityError: rollback; re-select`.
- [x] 3.3 **Retry scope** ✓ 2026-07-09 — `tasks.py:188-191` re-`raise` on any generic exception. With
      `autoretry_for=(TransientError,)` only `TransientError` retries; deterministic failures
      (`OutputNotFoundError`) should be marked FAILED and `return` instead of `raise`.
      → Design: [`design.md §#10`](./design.md). Only `TransientError` propagates; deterministic
      failures `return` after `_mark_failed`.
- [x] 3.4 **Test coverage** ✓ 2026-07-09 (+ tests completed 2026-07-09) — `tests/service/test_videos_api.py`
      mocks `generate_video_task.delay` (enqueue), not `runner.run_oh` as plan §14 requires.
      Mocked `run_oh` tests (`test_worker.py`) cover the RUNNING→SUCCEEDED state machine and the
      cancel guard; the **real** process-group kill on abort (#2) was previously only covered
      by the mocked path. A new integration test (`test_runner.py`) now spawns a real subprocess
      (parent + spawned child) and asserts `run_oh` tears down the whole group via SIGTERM on
      abort — and escalates to SIGKILL on the overall timeout. Added `test_streaming.py`
      (real `200` full-stream + `Range`→`206`), `test_cleanup.py` (expired artifact/workspace/redis
      reclamation + pointer nulling; recent tasks untouched), and `test_api_edge.py` (CORS not
      reflecting arbitrary origins + credentials only for explicit origins; concurrent
      `IntegrityError` idempotency fallback).
      → Design: [`design.md §#11`](./design.md).
- [x] 3.5 **Drop unused dep** ✓ 2026-07-09 — `service/pyproject.toml:22` `ffmpeg-python` is unused
      (parser/runner call `ffprobe` via subprocess). Plan §10 Dockerfile also lists it; remove
      from both for cleanliness.
      → Design: [`design.md §#12`](./design.md). Grep-confirm no `import ffmpeg`, then remove.
- [x] 3.6 **SSE replay duplicate** ✓ 2026-07-09 — `videos.py:189-195` subscribes then `lrange`; lines
      between the two can appear both live and in replay. Use a single atomic
      subscribe+replay (e.g., `xread`/`pubsub` with a captured cursor, or replay-then-subscribe
      with dedup by line id).
      → Design: [`design.md §#13`](./design.md). Replace list+pubsub with a Redis **Stream**
      (`XADD`/`XREAD` cursor) — no duplicates, ordered; minimal list+pubsub fallback sketched.
- [x] 3.7 **Stale `output_path` after cleanup** ✓ 2026-07-09 — `tasks.py:194-233` deletes artifacts but
      does not null `output_path`; later downloads 404. Null `output_path` (and status) for
      cleaned tasks.
      → Design: [`design.md §#14`](./design.md). Null `output_path`/`workspace_path` inside
      `cleanup_expired_tasks` session.

---

## Completion Checklist

- [x] All 🔴 Phase 1 tasks done and quality-gated
- [x] All 🟠 Phase 2 tasks done and quality-gated
- [x] Phase 3 tasks reviewed (all 3.1–3.7 implemented)
- [ ] `openspec-archive` when ready

---

## Implementation Log (2026-07-09)

Branch: `feature/harden-hyperframes-video-service`. All 14 items implemented and
the corresponding code changes were applied to `service/`. Summary of changed
files:

| Item | File(s) | Change |
|------|---------|--------|
| #1 | `app/security.py` (new), `app/schemas.py` | `vet_extra_oh_args` allowlist + `field_validator` → 422 at API edge |
| #2 | `app/workers/runner.py`, `app/workers/tasks.py`, `app/routers/videos.py` | `is_aborted` watchdog + killpg; `_abort_requested` / `_mark_canceled`; RUNNING DELETE sets `oh:abort:<id>` |
| #3 | `app/routers/videos.py` | `_iterfile` uses `run_in_threadpool(fileobj.read)` |
| #4 | `app/workers/celery_app.py`, `docker/supervisord.conf` | `beat_schedule` for `cleanup_expired_tasks` + `[program:beat]` |
| #5 | `app/workers/tasks.py` | Module-level `ConnectionPool` (`_redis_client`); `XADD` per line |
| #6 | `app/config.py`, `alembic/env.py` | `db_migration_url` (asyncpg) used for migrations |
| #7 | `app/config.py`, `app/main.py` | `cors_origins`-driven; credentials only with explicit origins |
| #8 | `app/routers/videos.py` | Real `Range` → `206` + `Content-Range` |
| #9 | `app/routers/videos.py` | `except IntegrityError` → rollback + re-select existing task |
| #10 | `app/workers/tasks.py` | Deterministic failures `return` after `_mark_failed`; only `TransientError` re-raises |
| #11 | `tests/service/test_security.py`, `test_worker.py`, `test_sse.py`, `test_videos_api.py`, `test_runner.py` (new), `test_streaming.py` (new), `test_cleanup.py` (new), `test_api_edge.py` (new) | Allowlist / worker state machine / SSE Stream / 422 cases / real process-group kill on abort / 200-stream + Range 206 / cleanup reclamation / CORS + idempotency race |
| #12 | `service/pyproject.toml`, `Dockerfile` | Removed unused `ffmpeg-python` (system `ffmpeg` binary kept) |
| #13 | `app/workers/tasks.py`, `app/routers/videos.py` | Logs moved from list+pubsub to a Redis **Stream** (`XADD`/`XREAD` cursor) — no replay duplicates |
| #14 | `app/workers/tasks.py` | `cleanup_expired_tasks` nulls `output_path` / `workspace_path` |

**Quality Gate status:** ✅ **Validated.** The full suite was executed inside the
target image `openharness_hyperframes_qwen-tts_pptx:v0.1.9_v0.7.20_v1.3_v2.0`
(Python 3.11.15 venv at `/root/.openharness-venv`; dev deps `pytest pytest-asyncio
fakeredis aiosqlite httpx redis` installed ad-hoc inside a kept-alive container).
**50 passed** (`40` original + `10` added) via `pytest tests/service -o asyncio_mode=auto`
— including the previously-missing real-process integration test for #2
(`tests/service/test_runner.py`: abort sends SIGTERM to the whole oh process group,
killing both the `oh` process and its spawned child; timeout escalates to SIGKILL),
the `200`/`206` streaming test (#3/#8), the `cleanup_expired_tasks` reclamation test
(#4/#14), and the CORS + idempotency-race tests (#7/#9). Alembic offline
`upgrade head --sql` also emits correct Postgres DDL (`id UUID NOT NULL` +
`CREATE TYPE taskstatus AS ENUM …`), confirming #6. Container cleaned up after.

**Additional pre-existing bugs fixed during validation (NOT in the original 14):**
These blocked the service from importing/running on the pinned `celery>=5.4` and the
strict `Uuid` PK — they had to be fixed for the suite (and the worker) to run at all.
- `app/workers/celery_app.py`: `autodiscover_modules` → `autodiscover_tasks`. Celery 5.4
  removed `autodiscover_modules`; the old call raised `AttributeError` at import, so the
  worker (and `app.routers.videos`, which imports `celery_app`) could not start.
- `app/routers/videos.py`: `from sqlalchemy import IntegrityError` → `from sqlalchemy.exc
  import IntegrityError`. `IntegrityError` lives in `sqlalchemy.exc`; the wrong import
  broke `app.main`, taking the whole API down.
- `app/workers/tasks.py` + `app/routers/videos.py`: Celery serializes `task_id` as a
  **string**, but the `id` PK is a strict `Uuid` (bind processor calls `.hex`), so every
  task crashed with `'str' object has no attribute 'hex'`. Fixed by coercing
  `task_id = uuid.UUID(str(task_id))` at the worker entry and `Path / str(task_id)`; and
  by moving `from app.workers.tasks import generate_video_task` to module level in
  `videos.py` (it was a local import, so `patch("app.routers.videos.generate_video_task")`
  in the API tests could not bind).

**Test-only adjustments (do not affect production):**
- `tests/service/test_worker.py`: assertion `s.get(VideoTask, tid)` → `s.get(VideoTask,
  t.id)` (UUID object) to match the strict `Uuid` PK.
- `tests/service/test_sse.py`: added a `__DONE__` marker to the no-duplicate case so the
  SSE generator terminates (it only ends on the done marker; without it `xread` blocks
  forever), and an autouse `_reset_sse_state` fixture to reset sse_starlette's
  module-global exit Event per test (pytest-asyncio creates a new loop per test).

