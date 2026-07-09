# Implementation Tasks: Harden HyperFrames Video Service

**Change ID:** `harden-hyperframes-video-service`

All line references are from the 2026-07-09 source state and were re-verified.

---

## Phase 1: Security & Correctness (Must-fix 🔴)

- [ ] 1.1 **Whitelist `extra_oh_args`** — `service/app/schemas.py:18` only types the field.
      Add validation (schema or `runner.py:43-49`) that only permits a fixed allowlist of
      `--flag value` pairs, and **forbids** overriding safety-critical flags
      (`--permission-mode`, `--output`, etc.). Verify `runner.py:43-49` cannot be used to
      downgrade `--permission-mode full_auto` or redirect `--output`.
      **Quality Gate:** unit test rejecting dangerous args; integration check that
      `--permission-mode` stays `full_auto`.

- [ ] 1.2 **Make RUNNING cancellation effective** — `service/app/routers/videos.py:239-249`
      only `revoke(terminate=True)` + marks CANCELED. Because `runner.py:65` uses
      `preexec_fn=os.setsid`, the signal never reaches the `oh` process group.
      - Track `proc.pid` (session leader) in the worker; on cancel, `os.killpg(proc.pid, SIGTERM/KILL)`
        (mirror `runner.py:86,93`).
      - Add disk cleanup (workspace + stored video) for the RUNNING branch in `videos.py`.
      **Quality Gate:** test that DELETE on RUNNING leaves no orphan `oh`/chrome process and
      no leftover `/workspaces/<id>` / video file.

- [ ] 1.3 **Guard worker against overwriting a canceled task** — `service/app/workers/tasks.py:170-173`
      calls `_mark_succeeded` unconditionally after `run_oh` returns. Re-check
      `task.status == CANCELED` (or a `canceled_at` flag) right after `run_oh` returns and
      before locating/parsing/saving; if canceled, skip success and clean up.
      **Quality Gate:** test that a task canceled mid-run ends CANCELED, never SUCCEEDED.

- [ ] 1.4 **Offload file streaming from the event loop** — `service/app/routers/videos.py:147-165`
      does synchronous `fileobj.read(chunk)` inside the async `StreamingResponse` generator.
      Use `run_in_threadpool(fileobj.read, chunk)` or `aiofiles` for the read.
      **Quality Gate:** profiler/load check showing the loop stays responsive during a large
      download.

---

## Phase 2: Reliability & Ops (Should-fix 🟠)

- [ ] 2.1 **Schedule `cleanup_expired_tasks`** — task exists (`tasks.py:194`) but
      `service/docker/supervisord.conf` has only `api` + `worker` programs and
      `celery_app.py:13-23` has no `beat_schedule`. Add `[program:beat]` running
      `celery -A app.workers.celery_app.celery_app beat`, or add `beat_schedule` to
      `celery_app.py`.
      **Quality Gate:** `celery beat` starts in container; cleanup task runs on schedule.

- [ ] 2.2 **Pool Redis connections in `_append_log`** — `tasks.py:39-51` does
      `redis.from_url(...)` + `lpush` + `ltrim(0,9999)` + `publish` + `close` **per line**,
      and `ltrim` is O(N) per call. Introduce a module-level connection pool, batch
      `rpush`, and only `ltrim` periodically (or rely on a cap via `LTRIM` once per N lines).
      Apply the same pooling to `_update_log_tail` (`tasks.py:88-104`) and the done-publish
      (`tasks.py:176-183`).
      **Quality Gate:** long-task log path uses a constant number of connections; no per-line
      `ltrim`.

- [ ] 2.3 **Unify alembic on async driver** — `service/alembic/env.py:21` sets
      `sqlalchemy.url = settings.db_sync_url` (`postgresql+psycopg://`) while `:45` builds an
      **async** engine via `async_engine_from_config`. Switch the migration engine to
      `postgresql+asyncpg://` (or thread a dedicated `db_migrate_url`) and run
      `alembic upgrade head` to confirm.
      **Quality Gate:** `alembic upgrade head` succeeds on asyncpg.

- [ ] 2.4 **Restrict CORS** — `service/app/main.py:30-36` uses
      `allow_origins=["*"]` + `allow_credentials=True` (reflects Origin). Replace with an
      explicit origin list from settings, or drop `allow_credentials`.
      **Quality Gate:** preflight with arbitrary Origin is no longer reflected with
      credentials.

---

## Phase 3: Polish (Nice-to-fix 🟡)

- [ ] 3.1 **`Accept-Ranges` honesty** — `videos.py:163` advertises `Accept-Ranges: bytes`
      but no `Range` parsing / `206` (plan §8 marks optional). Either implement `Range`→`206`
      or remove the header.
- [ ] 3.2 **Idempotency race** — `videos.py:79-88` SELECT-then-INSERT can raise
      `IntegrityError` → 500 on concurrent duplicates (`models.py:44-46` has the unique
      constraint). Catch `IntegrityError` and return the existing task.
- [ ] 3.3 **Retry scope** — `tasks.py:188-191` re-`raise` on any generic exception. With
      `autoretry_for=(TransientError,)` only `TransientError` retries; deterministic failures
      (`OutputNotFoundError`) should be marked FAILED and `return` instead of `raise`.
- [ ] 3.4 **Test coverage** — `tests/service/test_videos_api.py` mocks
      `generate_video_task.delay` (enqueue), not `runner.run_oh` as plan §14 requires. Add
      tests that mock `runner.run_oh`, assert the RUNNING→SUCCEEDED state machine, a real
      `200` stream download, and SSE. Add a Postgres-native run (current suite uses
      `sqlite+aiosqlite`, line 18) to exercise native `Enum`.
- [ ] 3.5 **Drop unused dep** — `service/pyproject.toml:22` `ffmpeg-python` is unused
      (parser/runner call `ffprobe` via subprocess). Plan §10 Dockerfile also lists it; remove
      from both for cleanliness.
- [ ] 3.6 **SSE replay duplicate** — `videos.py:189-195` subscribes then `lrange`; lines
      between the two can appear both live and in replay. Use a single atomic
      subscribe+replay (e.g., `xread`/`pubsub` with a captured cursor, or replay-then-subscribe
      with dedup by line id).
- [ ] 3.7 **Stale `output_path` after cleanup** — `tasks.py:194-233` deletes artifacts but
      does not null `output_path`; later downloads 404. Null `output_path` (and status) for
      cleaned tasks.

---

## Completion Checklist

- [ ] All 🔴 Phase 1 tasks done and quality-gated
- [ ] All 🟠 Phase 2 tasks done and quality-gated
- [ ] Phase 3 tasks reviewed (at least 3.1–3.4 recommended)
- [ ] `openspec-archive` when ready
