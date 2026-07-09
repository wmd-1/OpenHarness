# Detailed Design — Harden HyperFrames Video Service

**Change ID:** `harden-hyperframes-video-service`
**Status:** Draft (design only — no implementation code is modified)

All code blocks below are **intended diffs / sketches**, not applied changes. File
references use the current `service/` layout. Line numbers reflect the code read on
2026-07-09 and may drift as the repo evolves — verify before applying.

Conventions used in sketches:
- `settings.*` = `app/config.py` (pydantic-settings).
- `redis` connection helper = the pooled client described in §5.

---

## 🔴 #1 `extra_oh_args` allowlist validation

**Where:** `service/app/schemas.py:18`, `service/app/workers/runner.py:43-49`.

**Why:** Plan §4 marks `extra_oh_args` "受白名单约束". Today the list is concatenated
raw into the `oh` argv (`*(extra_args or [])`). Because `--permission-mode full_auto`
is emitted *before* `extra_args`, a caller can append `--permission-mode something_else`
(argparse keeps the last occurrence) or `--output /evil` to redirect artifacts.

**Design:**
1. Introduce a vetting module `service/app/security.py` with an explicit allowlist and a
   blocklist of safety-critical flags.
2. Validate in the Pydantic model so bad input fails fast with **422** at the API edge
   (do not push validation into the worker).
3. Keep `runner.py` forwarding `*extra_args` unchanged — it is now pre-vetted.

```python
# service/app/security.py
from __future__ import annotations
from dataclasses import dataclass

# flag -> takes_a_value?
ALLOWED_OH_FLAGS: dict[str, bool] = {
    "--temperature": True,
    "--max-turns": True,
    "--model": True,
    "--no-cache": False,
    # ⚠️ only add flags that are provably safe to expose
}

# flags that must never be caller-controlled
FORBIDDEN_OH_FLAGS = {
    "--permission-mode", "--permission_mode",
    "--output", "--output-format",
    "-p", "--prompt",
    "--workspace", "--cwd", "--root",
    "--headed", "--no-headless",  # could pop a GUI / change browser behavior
}

class InvalidOhArgError(ValueError):
    pass

def vet_extra_oh_args(raw: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(raw):
        tok = raw[i]
        if not tok.startswith("--"):
            raise InvalidOhArgError(f"only --flags allowed, got {tok!r}")
        if tok in FORBIDDEN_OH_FLAGS:
            raise InvalidOhArgError(f"flag {tok} is not caller-controllable")
        if tok not in ALLOWED_OH_FLAGS:
            raise InvalidOhArgError(f"flag {tok} is not in the allowlist")
        out.append(tok)
        if ALLOWED_OH_FLAGS[tok]:
            if i + 1 >= len(raw):
                raise InvalidOhArgError(f"flag {tok} requires a value")
            out.append(raw[i + 1])
            i += 2
        else:
            i += 1
    return out
```

```python
# service/app/schemas.py
from pydantic import field_validator
from app.security import vet_extra_oh_args

class VideoCreateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    timeout_seconds: int = Field(default=900, ge=30, le=3600)
    extra_oh_args: list[str] = Field(default_factory=list)  # validated below
    idempotency_key: str | None = None

    @field_validator("extra_oh_args")
    @classmethod
    def _vet(cls, v: list[str]) -> list[str]:
        return vet_extra_oh_args(v)
```

**Verification:** unit-test `vet_extra_oh_args` with `--permission-mode evil` (raises),
`["--output","/x"]` (raises), `["--bogus"]` (raises), `["--temperature","0.7"]` (ok).
Add API test: `POST /v1/videos` with forbidden flag → 422.

**Risk:** allowlist is conservative; legitimate future flags require a code change.
Acceptable — security over convenience.

---

## 🔴 #2 RUNNING cancel must kill the `oh` process group + never overwrite to SUCCEEDED

**Where:** `service/app/routers/videos.py:239-249`, `service/app/workers/runner.py:65`,
`service/app/workers/tasks.py:149-173`.

**Why (two distinct defects):**
- (a) `runner.py:65` calls `preexec_fn=os.setsid`, so `oh` runs in its own session /
  process group. `celery_app.control.revoke(terminate=True)` signals the *Celery worker*
  process, which does **not** propagate to `oh` or its chrome children → orphan process +
  leaked disk, and the task is only optimistically marked CANCELED.
- (b) `tasks.py` unconditionally calls `_mark_succeeded` after `run_oh` returns, with no
  re-check of cancellation. If `oh` finishes (or the worker ignores the revoke), the task
  is overwritten back to SUCCEEDED despite the user's DELETE.

**Why not just signal from the API:** with `docker compose up --scale api=N`, the task
may run on a *different* replica's worker, so the API process cannot `killpg` the child
(reliable only within the same PID namespace). Therefore the **worker must cancel itself**.

**Design — worker self-abort via a shared Redis flag:**
1. `runner.run_oh` accepts `is_aborted: Callable[[], bool]`. Inside the reader loop (or a
   separate watchdog thread), it polls the flag; when set it does
   `os.killpg(proc.pid, SIGTERM)`, waits, then `SIGKILL`, then returns normally.
   (pgid == `proc.pid` because `setsid` made `oh` its own group leader.)
2. `tasks.generate_video_task` builds `is_aborted` from Redis key `oh:abort:<task_id>`
   and, **after** `run_oh` returns, checks it before `_mark_succeeded`:
   if aborted → `_mark_canceled(task_id)` and `return`.
3. `DELETE /v1/videos/{id}` (RUNNING branch) sets `oh:abort:<task_id>` in Redis, keeps
   `revoke(terminate=True)` as a best-effort nudge, and marks CANCELED. The worker is the
   authoritative one to finalize cancellation.

```python
# service/app/workers/runner.py (excerpt)
def run_oh(..., is_aborted: Callable[[], bool] | None = None, ...) -> RunResult:
    ...
    proc = Popen(cmd, ..., preexec_fn=os.setsid)
    pgid = proc.pid  # setsid -> oh is its own process-group leader

    def _maybe_abort() -> bool:
        if is_aborted is not None and is_aborted():
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                pass
            return True
        return False

    # inside reader loop / a watchdog:
    #   if _maybe_abort(): break
    # after proc.wait(timeout): if aborted flag, ensure killpg(SIGKILL)
```

```python
# service/app/workers/tasks.py (excerpt)
def _mark_canceled(task_id, exc=None):
    with _sync_session() as db:
        t = db.get(VideoTask, task_id)
        if t is None: return
        t.status = TaskStatus.CANCELED
        t.finished_at = datetime.now(timezone.utc)
        if exc: t.error_message = str(exc)[:4000]
        db.commit()

@celery_app.task(...)
def generate_video_task(self, task_id):
    ...
    def _is_aborted():
        try:
            import redis
            r = redis.from_url(settings.broker_url)
            return r.get(f"oh:abort:{task_id}") is not None
        except Exception:
            return False

    try:
        result = run_oh(..., is_aborted=_is_aborted, ...)
        if _is_aborted():
            _mark_canceled(task_id, RuntimeError("canceled by user"))
            return
        ...  # existing locate/probe/save/_mark_succeeded
```

```python
# service/app/routers/videos.py (RUNNING branch)
if task.status == TaskStatus.RUNNING:
    try:
        import redis
        r = redis.from_url(settings.broker_url)
        r.set(f"oh:abort:{task.id}", "1", ex=3600)
    except Exception:
        pass
    if task.celery_task_id:
        celery_app.control.revoke(task.celery_task_id, terminate=True, signal="SIGTERM")
    task.status = TaskStatus.CANCELED
    await db.commit()
    return VideoDeleteResponse(...)
```

**Verification:** integration test — start a fake `oh` that sleeps; POST a task; call
DELETE while RUNNING; assert `oh` process group is gone (no orphan), workspace removed,
status stays CANCELED; assert a late "finish" does not flip it to SUCCEEDED.

**Risk:** multi-replica: only the owning worker sees its own child; the Redis abort flag
works across replicas. `revoke` is best-effort. Good.

---

## 🔴 #3 Non-blocking streaming download

**Where:** `service/app/routers/videos.py:147-165`.

**Why:** `fileobj.read(chunk)` is synchronous inside an `async def` generator. A 100 MB+
`video/mp4` read blocks the single event loop, stalling every other request on that
uvicorn worker. (Plan §8 pseudocode had the same pattern — known limitation, must fix
for prod.)

**Design:** off-load the blocking read to a threadpool (no new dependency) or use
`aiofiles`. Recommend `run_in_threadpool` for zero new deps.

```python
# service/app/routers/videos.py
from fastapi.concurrency import run_in_threadpool

async def _iterfile(fileobj, chunk: int = 1 << 20):
    try:
        while True:
            data = await run_in_threadpool(fileobj.read, chunk)
            if not data:
                break
            yield data
    finally:
        fileobj.close()
```

(Alternative: `import aiofiles; async with aiofiles.open(...) as f: async for ...`.)

**Verification:** load-test — stream a large file while a second concurrent request is
inflight; assert the second responds without waiting for the full download.

**Risk:** minimal. `run_in_threadpool` is standard FastAPI guidance.

---

## 🟠 #4 `cleanup_expired_tasks` never runs (no beat)

**Where:** `service/app/workers/celery_app.py:13-23`, `docker/supervisord.conf` (only
`api` + `worker`), plan §13.

**Why:** the task is defined but nothing schedules it.

**Design:** register `beat_schedule` in the Celery app **and** run a beat process in
supervisord.

```python
# service/app/workers/celery_app.py
celery_app.conf.beat_schedule = {
    "cleanup-expired-tasks": {
        "task": "cleanup_expired_tasks",
        "schedule": 86400.0,   # daily
    },
}
```

```ini
# docker/supervisord.conf  (add)
[program:beat]
command=/root/.openharness-venv/bin/celery -A app.workers.celery_app.celery_app beat -l info
directory=/opt/oh-service
autostart=true
autorestart=true
environment=PYTHONPATH="/app/src:/opt/oh-service"
```

**Multi-replica caveat:** with `--scale api=N`, each replica starts its own beat → N
redundant schedulers all firing `cleanup_expired_tasks`. Mitigations, in order of
preference:
- (recommended) use `redbeat` (Redis-backed beat) so only one scheduler is active, or
- run beat on a single designated replica (document the constraint), or
- accept redundancy: `cleanup_expired_tasks` is idempotent (deleting an already-deleted
  path is a no-op), so duplicate runs are harmless.

**Verification:** `celery -A ... beat` starts; after lowering `schedule` in a test,
confirm expired rows are cleaned and Redis `oh:logs:*` keys removed.

---

## 🟠 #5 `_append_log` connection + per-line `ltrim`

**Where:** `service/app/workers/tasks.py:39-51`.

**Why:** a new Redis client + `lpush` + `ltrim(0,9999)` + `publish` + `close` per stdout
line → thousands of TCP connect/teardown and O(N) `ltrim` calls for long tasks.

**Design:** module-level pooled client; `ltrim` only when the list actually exceeds the
cap; keep `publish` per line (needed for live SSE).

```python
# service/app/workers/tasks.py
import redis as _redis
_LOG_POOL = None
def _redis_client():
    global _LOG_POOL
    if _LOG_POOL is None:
        _LOG_POOL = _redis.ConnectionPool.from_url(settings.broker_url)
    return _redis.Redis(connection_pool=_LOG_POOL)

def _append_log(task_id, line):
    try:
        r = _redis_client()
        r.lpush(f"oh:logs:{task_id}", line)
        if r.llen(f"oh:logs:{task_id}") > 10000:
            r.ltrim(f"oh:logs:{task_id}", 0, 9999)
        r.publish(f"oh:channel:{task_id}", line)
    except Exception:
        logger.warning("log push failed for %s", task_id)
```

(Reuse the same `_redis_client()` in `_update_log_tail`, the done-publish, and
`cleanup_expired_tasks`.)

**Verification:** unit-test with a fakeredis client; assert one connection is reused and
`ltrim` is called only when length > cap.

**Risk:** minor; shared pool is process-global, fine for a worker.

---

## 🟠 #6 Alembic mixes sync URL with async engine

**Where:** `service/alembic/env.py:21` (`settings.db_sync_url` → `postgresql+psycopg://`)
and `:45` (`async_engine_from_config` → async engine).

**Why:** psycopg v3 *can* drive an async engine, but the convention is inconsistent and
fragile. Use an async-native URL for migrations.

**Design:** add an async migration URL and use it in `env.py`.

```python
# service/app/config.py — add
db_migration_url: str = Field(
    default="postgresql+asyncpg://oh:oh@postgres:5432/oh",
    alias="OH_DB_MIGRATION_URL",
)
```

```python
# service/alembic/env.py
config.set_main_option("sqlalchemy.url", settings.db_migration_url)
```

**Verification:** `alembic upgrade head` against a fresh Postgres; confirm it applies and
`alembic downgrade base` works. (Run in the deployed container, not locally.)

**Risk:** requires `asyncpg` present (it is, per `pyproject.toml:14`).

---

## 🟠 #7 CORS `*` + credentials

**Where:** `service/app/main.py:30-36`.

**Why:** `allow_origins=["*"]` + `allow_credentials=True` makes Starlette echo the
request `Origin` with `Access-Control-Allow-Credentials: true` — i.e. any site can call
the API with the user's credentials.

**Design:** drive origins from config; if credentials are on, the list must be explicit.

```python
# service/app/main.py
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(",") if settings.cors_origins else [],
    allow_credentials=bool(settings.cors_origins),  # only with explicit origins
    allow_methods=["*"],
    allow_headers=["*"],
)
```

For pure dev convenience with no credentials: `allow_origins=["*"]`,
`allow_credentials=False`.

**Verification:** curl with a random `Origin` + `Authorization`; assert the response does
not return `Access-Control-Allow-Origin: <that origin>` together with
`Access-Control-Allow-Credentials: true`.

---

## 🟡 #8 `Accept-Ranges` advertised but unsupported

**Where:** `service/app/routers/videos.py:163`.

**Why:** header claims byte-range support, but no `Range` parsing / `206` response exists.
Plan §8 marks it optional — implement it or stop advertising.

**Design (recommended — implement Range):**

```python
# service/app/routers/videos.py
from fastapi.responses import StreamingResponse

range_hdr = request.headers.get("Range")
start = 0
if range_hdr and range_hdr.startswith("bytes="):
    try:
        start = int(range_hdr[len("bytes="):].split("-")[0])
    except ValueError:
        start = 0

fileobj, size = storage.open(task.output_path)
fileobj.seek(start)
status = 206 if start else 200

async def _iterfile(chunk=1 << 20):
    try:
        while True:
            data = await run_in_threadpool(fileobj.read, chunk)
            if not data: break
            yield data
    finally:
        fileobj.close()

return StreamingResponse(
    _iterfile(),
    status_code=status,
    media_type="video/mp4",
    headers={
        "Content-Length": str(size - start),
        "Content-Range": f"bytes {start}-{size-1}/{size}" if start else "",
        "Content-Disposition": f'attachment; filename="{task_id}.mp4"',
        "Accept-Ranges": "bytes",
    },
)
```

**Verification:** `curl -r 0-1023` returns 206 with correct `Content-Range`.

---

## 🟡 #9 Idempotency race → 500

**Where:** `service/app/routers/videos.py:79-88` (SELECT-then-INSERT), model
`service/app/models.py:44-46` (`unique=True`).

**Why:** concurrent duplicate submissions both pass the SELECT, then one INSERT hits
`IntegrityError` → unhandled 500 instead of returning the existing task.

**Design:** catch `IntegrityError`, roll back, re-query, return existing.

```python
# service/app/routers/videos.py
from sqlalchemy.exc import IntegrityError

try:
    db.add(task); await db.commit(); await db.refresh(task)
except IntegrityError:
    await db.rollback()
    if body.idempotency_key is not None:
        existing = (await db.execute(
            select(VideoTask).where(VideoTask.idempotency_key == body.idempotency_key)
        )).scalar_one_or_none()
        if existing is not None:
            return VideoCreateResponse(task_id=existing.id, status=existing.status,
                                       links=_task_links(existing.id))
        raise
```

**Verification:** two concurrent `POST` with the same `idempotency_key` → both 201
returning the *same* `task_id`; no 500.

---

## 🟡 #10 Deterministic failure still `raise`

**Where:** `service/app/workers/tasks.py:188-191`.

**Why:** after `_mark_failed`, the generic `except` re-`raise`s. With `acks_late=True`
the message is not acknowledged, and since the exception is not `TransientError` it is
not retried — so it is *harmless* but noisy (the task still "fails"). Plan pseudocode also
raises, so this is acceptable; optional refinement below.

**Design (optional):** do not re-raise deterministic failures; only `TransientError`
should propagate to trigger the `autoretry_for` retry.

```python
except OutputNotFoundError as exc:
    _update_log_tail(task_id); _mark_failed(task_id, exc)          # no raise
except TransientError:
    raise                                                        # retry
except Exception as exc:
    _update_log_tail(task_id); _mark_failed(task_id, exc); return # no raise
```

**Verification:** a deterministic failure marks FAILED and the Celery task exits 0
(no infinite redelivery); a `TransientError` still retries up to `max_retries`.

---

## 🟡 #11 Test coverage gaps

**Where:** `tests/service/test_videos_api.py` (mocks `generate_video_task.delay`, not
`runner.run_oh`), `tests/service/test_parser.py` (good). Plan §14 wants worker + SSE +
real-download tests.

**Design — add `tests/service/test_worker.py`:**
Drive the *real* task path by patching `app.workers.runner.run_oh` (not the Celery
enqueue), then assert state transitions, download 200, and SSE chunks.

```python
import pytest
from unittest.mock import patch
from app.workers import tasks as worker_tasks
from app.workers.parser import VideoMeta

@pytest.mark.asyncio
async def test_full_happy_path(client, db_session):
    # create a queued task directly
    task = VideoTask(prompt="x"); db_session.add(task); await db_session.commit()
    await db_session.refresh(task)

    fake_meta = VideoMeta(file_size_bytes=10, duration_seconds=1.0,
                          resolution="2x2", fps=1)
    with patch.object(worker_tasks, "run_oh") as m:
        m.return_value = type("R", (), {"exit_code": 0, "stdout": "**输出文件:** `/tmp/o.mp4`"})()
        with patch.object(worker_tasks, "locate_output_file", return_value=Path("/tmp/o.mp4")), \
             patch.object(worker_tasks, "probe_mp4", return_value=fake_meta), \
             patch.object(worker_tasks, "LocalVideoStorage") as LS:
            LS.return_value.save.return_value = "k"
            worker_tasks.generate_video_task.run(task_id=str(task.id))

    got = await db_session.get(VideoTask, task.id)
    assert got.status == "succeeded"
```

Add SSE test (subscribe to `oh:channel:<id>`, publish a line + `__DONE__`, assert events)
and a download-200 test (pre-populate storage, assert 200 + bytes). Note these still use
`sqlite+aiosqlite`; add at least one Postgres-native test (or document that Enum behavior
is only exercised in CI against Postgres).

**Verification:** `pytest tests/service` green; coverage includes worker state machine.

---

## 🟡 #12 Unused `ffmpeg-python` dependency

**Where:** `service/pyproject.toml:22`. Also present in plan §10 Dockerfile deps.

**Why:** `parser.py` / `runner.py` invoke `ffprobe` / `oh` via `subprocess`, never
`ffmpeg-python`. The dep is dead weight.

**Design:** remove `ffmpeg-python>=0.2.0,<1` from `pyproject.toml`. (Plan §10 Dockerfile
should also drop it, but that is a separate doc edit — note it.)

**Risk:** none, provided no hidden import exists. Grep for `import ffmpeg` to confirm.

---

## 🟡 #13 SSE replay race (duplicate lines)

**Where:** `service/app/routers/videos.py:189-195` (subscribe **then** lrange).

**Why:** between `subscribe(channel)` and `lrange(log_key)`, a freshly published line can
be delivered both by pubsub (live) and by the replay → duplicate `log` event.

**Design (recommended):** replace list+pubsub with a single Redis **Stream**
(`oh:logs:<id>`). One `XADD` per line; SSE uses `XREAD` with a cursor for both replay and
live tail — no duplicates, naturally ordered.

Minimal fix if staying on list+pubsub: capture history **before** subscribing, then in the
live loop skip any message already emitted from history (track a set). The window still
exists; the Stream approach is the real fix.

```python
# worker side (tasks._append_log): r.xadd(f"oh:logs:{task_id}", {"line": line})
# API side (videos.video_events):
last_id = "0"
history = r.xrange(f"oh:logs:{sid}")          # replay from beginning
for _id, fields in history:
    yield {"event": "log", "data": fields[b"line"].decode()}
    last_id = _id
while True:
    resp = r.xread({f"oh:logs:{sid}": last_id}, block=5000)
    if not resp: continue
    for _id, fields in resp[0][1]:
        if fields.get(b"line") == b"__DONE__":
            yield {"event": "done", "data": json.dumps({"status": "completed"})}; return
        yield {"event": "log", "data": fields[b"line"].decode()}
        last_id = _id
```

**Verification:** race test — publish rapidly while a client replays; assert no duplicate
`log` events.

---

## 🟡 #14 Cleanup leaves DB pointing at deleted file

**Where:** `service/app/workers/tasks.py:194-233` (`cleanup_expired_tasks`).

**Why:** it deletes the video + workspace + Redis logs but never updates the `VideoTask`
row, so `output_path` still points at a gone file → later `GET /file` → 404 with a stale
pointer.

**Design:** null the paths (and optionally set status) when cleaning.

```python
# inside cleanup_expired_tasks loop, after deletions:
task.output_path = None
task.workspace_path = None
# keep status (SUCCEEDED/FAILED/CANCELED) for history; or set CANCELED
db.commit()   # within the same _sync_session() block
```

(The `DELETE /v1/videos/{id}` handler already sets `output_path=None` at
`videos.py:265` — good; align the cleanup task with it.)

**Verification:** after cleanup, `GET /v1/videos/{id}/file` returns 404 cleanly (no
orphan pointer); `GET /v1/videos/{id}` shows `output_path=null`.

---

## Summary of files to touch (when implemented)

| File | Items |
|------|-------|
| `service/app/security.py` (new) | #1 |
| `service/app/schemas.py` | #1 |
| `service/app/workers/runner.py` | #2 |
| `service/app/workers/tasks.py` | #2, #5, #10, #14 |
| `service/app/routers/videos.py` | #2, #3, #8, #9 |
| `service/app/workers/celery_app.py` | #4 |
| `docker/supervisord.conf` | #4 |
| `service/alembic/env.py` | #6 |
| `service/app/config.py` | #6, #7 |
| `service/app/main.py` | #7 |
| `service/pyproject.toml` | #12 |
| `tests/service/test_worker.py` (new) | #11 |
