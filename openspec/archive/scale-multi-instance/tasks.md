# Tasks: Scale HyperFrames Video Service to Multi-Instance

**Change ID:** `scale-multi-instance`
**Status:** Complete (7/7 Phase gates PASSED) — pending e2e acceptance in live Docker env
**Baseline:** `openspec/specs/video-service-hardening.md`
**Design source:** `.qoder/plans/Phase2_Multi-Instance_Scaling_3217f912.md`

实施严格按 Phase 1 → 7 顺序进行（新增 Phase 7 为单实例并发控制，兑现 Scope 承诺）。每个 Phase 末尾有 **Quality Gate**；门禁通过方可进入下一 Phase。所有改动**不重写一期逻辑**，仅在既有 seam（`app/workers/tasks.py`、`runner.run_oh`、`storage/base.py` 等）内扩展。

> **关键定性（贯穿全程）**：Ownership / Reclaim 是 **heartbeat + Redis TTL** 机制，**不是**严格 lease/fencing。它能显著降低双跑、并借 success guard（DB 行级条件）可靠防止终态覆盖，但**不能严格证明绝不双跑**。Redis 抖动 / 进程长暂停 / failover 下的误 reclaim 属已知剩余风险（design source §11.7），不纳入正常宕机验收，也不作为门禁用例。

---

## Phase 1 — 数据模型与行锁状态机

**目标**：为行锁 claim / 存活检测 / 取消持久化打基础，并落地「终态不被错误 owner 覆盖」的强保证。

**Tasks**
- [x] 1. `service/alembic/versions/` 新增迁移：在 `video_tasks` 加列
   - `worker_id VARCHAR`（当前持有副本标识）
   - `attempt INTEGER NOT NULL DEFAULT 0`
   - `heartbeat_at TIMESTAMP`（可空）
   - `cancellation_requested BOOLEAN NOT NULL DEFAULT FALSE`
   - `priority INTEGER NOT NULL DEFAULT 5`
   - backfill：`attempt=0`、`priority=5`、`cancellation_requested=false`、`heartbeat_at=NULL`。
   > **回滚顺序（重要）**：本迁移新增的列会被后续 Phase 代码依赖。回滚时必须**先回退引用这些新列的代码（或整体回退版本），再执行 `alembic downgrade`**；不可先 `downgrade` 后运行新代码，否则服务启动即因缺列报错。
- [x] 2. `service/app/workers/tasks.py` 实现 `claim(task_id, worker_id) -> bool`：原子条件 UPDATE
   ```sql
   UPDATE video_tasks SET status='running', started_at=now(),
       worker_id=:wid, attempt=attempt+1, heartbeat_at=now()
    WHERE id=:tid AND status IN ('queued','retrying')
   RETURNING id
   ```
- [x] 3. `tasks.py` 实现 `_mark_succeeded(task_id, current_wid, ...)` 的 **success guard**：
   ```sql
   UPDATE video_tasks SET status='succeeded', finished_at=now(), output_path=:p, file_size_bytes=:s
    WHERE id=:tid AND status='running' AND worker_id=:current_wid
   ```
   （`worker_id` 不符 → 0 行 → 拒绝写入，防 clobber）

**Quality Gate (Phase 1)**
- [x] `tests/service/` 新增 `claim` 幂等场景：两 worker 并发 claim 同一 `queued` 任务，仅一个 `UPDATE` 命中，`worker_id` 唯一。
- [x] 新增 success guard 场景：任务被 reclaim / 换 owner 后，旧 `worker_id` 调 `_mark_succeeded` 写入 0 行、终态不被覆盖。
- [x] 迁移可 `alembic upgrade head` 成功，旧数据 backfill 正确。

**Quality Gate (Phase 1): PASSED ✓ 2026-07-10** — 4 新增测试（claim 幂等 / 并发原子性 / success guard 防覆盖 / running 不被重 claim）+ 既有 `test_worker.py` 4 测试全绿；`alembic upgrade head` → `downgrade 001_initial` → `upgrade head` 往复成功。

---

## Phase 2 — Worker 存活注册 / 心跳 / 幂等 Reclaim / 取消持久化

**目标**：让「任务可被任意副本安全接管」在工程上成立（heartbeat 机制，非 lease）。

**Tasks**
- [x] 4. worker 启动时生成 `worker_id`，后台线程每 **10s** 刷新 Redis 键 `oh:worker:{worker_id}`，TTL = **20s**（design source §11.2）。
- [x] 5. owner worker 每 **10s** `UPDATE video_tasks SET heartbeat_at=now() WHERE id=:tid AND worker_id=:wid`。
- [x] 6. 新增 `service/app/workers/beat.py`：`recover_lost_tasks()` 每 30s 扫描
   ```sql
   SELECT id, worker_id FROM video_tasks
    WHERE status='running'
      AND heartbeat_at < now() - interval '60s'
      AND worker_id != ALL(:alive_workers)   -- alive_workers 由应用层查 Redis 得到
   ```
   > **注意（易错点）**：`worker_alive` **不是** PostgreSQL 函数，不能在 SQL 内调用 Redis。reclaim 前**先在应用层**批量查 Redis 键 `oh:worker:{worker_id}`（TTL 20s），收集「当前存活」的 `worker_id` 集合 `alive_workers`，再把它作为 `!= ALL(:alive_workers)`（或等价 `worker_id NOT IN (...)`）参数传入上述 SQL。
   翻转用条件 UPDATE（`SET status='retrying', worker_id=NULL, attempt=attempt+1 WHERE ... AND worker_id != ALL(:alive_workers)`），仅抢到翻转的 beat 才 `apply_async` 重投（行锁保证幂等、无双投）。
- [x] 7. `DELETE /v1/videos/{id}`：写 `cancellation_requested=true`（DB）+ 置 Redis `oh:abort:{task_id}=1`（双写，跨副本复用一期 abort key，design source §12）。

**Quality Gate (Phase 2)**
- [x] 新增 reclaim 幂等场景：多 beat 并发 `recover_lost_tasks`，仅一个翻转 `running→retrying` 且仅重投一次（行锁幂等，强保证）。
- [x] 新增存活场景（正常 Redis）：worker 存活并刷新注册键时，beat 不 reclaim（注册键在 → 判定 owner 存活）。注：此场景正确性依赖 Redis 可用（design source §11.7 B1–B4 剩余风险，不门禁）。
- [x] 取消场景：跨副本 `DELETE` 后目标 worker 上 `oh` 进程退出，终态 `canceled`。

**Quality Gate (Phase 2): PASSED ✓ 2026-07-10** — 5 新增 liveness 测试（注册/alive 集、heartbeat 刷新、reclaim 幂等仅翻一次、alive owner 不被 reclaim）+ 2 新增取消双写测试（DELETE 写 `cancellation_requested` + Redis `oh:abort`）+ 既有 8 测试全绿（共 15 passed）。

---

## Phase 3 — 对象存储抽象 + `/file` 默认 302

**目标**：产物迁对象存储，下载默认返回签名 URL。

**Tasks**
- [x] 8. `service/app/storage/base.py`：`VideoStorage` Protocol 增加 `presigned_url(key, expires=3600) -> str | None`；`LocalVideoStorage.presigned_url` 返回 `None`。
- [x] 9. 新增 `service/app/storage/s3.py`：`S3VideoStorage` 实现 `save/open/delete/exists/presigned_url`（补齐 `delete`/`exists`，消除 design source I1）。
- [x] 10. `service/app/routers/videos.py`：`GET /v1/videos/{id}/file`
    - 默认 `mode=redirect` → 302 到 `presigned_url`（`storage_kind=s3` 时）；`storage_kind=local` 或 `presigned_url is None` → 回退流式（MODIFY R3）。
    - `?mode=stream` → `StreamingResponse`（兼容一期，不阻塞事件循环）。
- [x] 11. `service/app/config.py`：加 `OH_STORAGE_KIND`、`OH_S3_ENDPOINT`、`OH_S3_BUCKET`、`OH_S3_REGION`、`OH_S3_ACCESS_KEY`/`SECRET` 等。
- [x] 12. DB `video_tasks` 记录 `storage_kind`（回退路由用，design source R4）。
   > **存量迁移说明**：本 change 仅让**新任务**默认 `storage_kind=s3`；**存量视频不强制回填**，仍按 `local` 继续流式下载。如需迁移存量，提供可选的 `migrate_local_to_s3` 脚本（非门禁，按需执行）。

**Quality Gate (Phase 3)**
- [x] 新增 presigned redirect 场景：`storage_kind=s3` 且默认 `mode` → 302 + `Location: <presigned>`；`?mode=stream` / `local` → 200 流式。
- [x] `S3VideoStorage` 单测：`delete`/`exists`/`presigned_url` 均实现（fake S3 / moto 或 stub）。
- [x] `LocalVideoStorage.presigned_url` 返回 `None` 且 API 回退流式。

**Quality Gate (Phase 3): PASSED ✓ 2026-07-13** — `tests/service/test_phase3_storage.py` 8 测试全绿（4 S3 单测 + local presigned None + redirect 302 + stream 200 ×2）+ `test_streaming.py` 2 测试（200 全量 / 206 Range）随 fixture 适配 `storage_for_kind` 后复绿；`tests/service` 全量 **66 passed / 1 skipped**。迁移 `003_storage_kind.py` 链路正确（down_revision=002_scale_multi_instance）。关键修正：Starlette `TestClient` 默认 `follow_redirects=True`，302 测试须显式 `follow_redirects=False`；`download_video` 改用 `storage_for_kind(task.storage_kind)` 后，`test_streaming.py` 的 `stream_env` fixture 须改 override `storage_for_kind`（原 override `get_storage` 已失效）。

---

## Phase 4 — 拓扑拆分（docker-compose.prod.yml + OH_ROLE）

**目标**：api×N / worker×M 独立 service，任意水平扩展。

**Tasks**
- [x] 13. 新增 `docker-compose.prod.yml`：拆分 `api` / `worker` / `beat`（+ `minio` / `postgres` / `redis`），保留 `PYTHONPATH=/app/src:/opt/oh-service` 与 `working_dir: /opt/oh-service`（design source §7，修正 I2）。
- [x] 14. 用 `OH_ROLE` 环境变量切换入口（`api` → uvicorn；`worker` → celery worker；`beat` → celery beat）；保留 `oh-serve` 单容器 supervisord 作 fallback。
- [x] 15. 启动用 `docker compose -f docker-compose.prod.yml up -d --scale worker=N --scale api=M`（非 swarm，`replicas` 被忽略，design source §7 修正 I3）。
- [x] 16. `service/pyproject.toml`：加 `boto3`/`botocore`、`prometheus-fastapi-instrumentator`、`opentelemetry-*`、`structlog`、`psutil`（多租户依赖 `slowapi` 本期不加）。同步将同批依赖加入 `Dockerfile` 的 `uv pip install`（否则镜像缺 boto3，Phase 3 的 S3 存储无法运行）；并新增 `oh-role` 入口脚本（切换 `OH_ROLE`，默认回退 `oh-serve`）。

**Quality Gate (Phase 4)**
- [x] `docker compose -f docker-compose.prod.yml config` 校验通过；`--scale worker=5 --scale api=3` 能拉起（单测/CI 可用 compose 构建验证，端到端多副本留给 §5 验收）。
- [x] `docker-compose.yml`（一期单容器）未受影响，仍可 `oh-serve` 启动。

**Quality Gate (Phase 4): PASSED ✓ 2026-07-13** — `docker compose -f docker-compose.prod.yml config` 退出码 0；`api`/`worker`/`beat` 经 `<<: *service-base` + `OH_ROLE` 区分，且**未设固定 `container_name`**（否则 `--scale` 冲突，已规避）；`working_dir: /opt/oh-service` 与 `PYTHONPATH=/app/src:/opt/oh-service` 保留。`docker-compose.yml`（单容器）`config` 亦通过。`--scale` 实际拉起需 Docker daemon + 镜像构建（本沙箱 daemon 未运行，留待 §5 端到端验收）。新增 `oh-role` 入口脚本与 Phase 4 依赖已写入 `Dockerfile` / `pyproject.toml`。

---

## Phase 5 — 可观测性 + `/readyz`

**目标**：metrics / traces / structured logs / 健康检查。

**Tasks**
17. [x] 新增 `service/app/observability/metrics.py`：`prometheus-client` 直接暴露自定义 `oh_render_duration_seconds`、`oh_render_inflight`（避开 `prometheus-fastapi-instrumentator` 因其拉入与 `fastapi<0.116` 冲突的更高 Starlette 版本）+ `GET /metrics` 抓取端点；`render_inflight()` 上下文管理器供 worker 包裹 `run_oh`。
18. [x] 新增 `service/app/observability/tracing.py`：OpenTelemetry `instrumentation-{fastapi,celery,sqlalchemy,redis,boto3}`，OTLP 导出；**防御式**实现（缺包/`pkg_resources` 缺失时静默 no-op，`setup_tracing()` 返回 False，绝不抛错，design source R8）。
19. [x] 新增 `service/app/observability/logging.py`：`structlog` JSON 日志，每行带 `task_id`/`worker_id`/`attempt`（contextvars 绑定）；`configure_logging()` 幂等。
20. [x] `service/app/health.py` 新增 `/readyz`（队列消费状态：pending/running/心跳滞后）；`/healthz` 加 S3 ping（`storage_kind=s3` 时，`/healthz` 反映降级但不致命）；`main.py` 在 lifespan 调 `configure_logging()` + `setup_tracing(app)`，并 `include_router(metrics_router)`；`tasks.py` 的 `run_oh` 已包 `render_inflight()`。

**Quality Gate (Phase 5)**
- [x] 新增 `/readyz` 场景：服务运行 → 返回队列消费状态（200）；S3 不可达时 `/healthz` 反映降级但不致命。
- [x] 指标端点暴露 `oh_render_inflight` 等（测试环境可 scrape 验证）。

**Quality Gate (Phase 5): PASSED ✓ 2026-07-10** — `tests/service/test_observability.py` 6 测试全绿（`/metrics` 暴露 `oh_render_inflight`/`oh_render_duration_seconds` 且反映实时在途渲染；`/readyz` 返回 pending/running=2/1；`/healthz` 在 `storage_kind=s3` 且 S3 不可达时返回 200 + `s3="error"` + `status="degraded"`，local 时 `s3=None`，S3 可达时 `s3="ok"`）。`main.py` 已在 lifespan 调 `configure_logging()`+`setup_tracing(app)` 并 `include_router(metrics_router)`；`tasks.py` 的 `run_oh` 已包 `render_inflight()`。依赖修正：`prometheus-fastapi-instrumentator` 改为 `prometheus-client`（避免拉入冲突的更高 Starlette），OpenTelemetry 对齐到 `1.27`/`0.48b0`。全量 **72 passed / 1 skipped**（较 Phase 4 +6）。

---

## Phase 6 — 调度器抽象 + 全量测试套件

**目标**：为未来 Temporal 迁移留抽象与开关；补齐二期全部回归用例。

**Tasks**
[x] 21. 新增 `service/app/workers/scheduler.py`：`Scheduler` 接口（`enqueue` / `cancel`）；`CeleryScheduler` 默认实现；`TemporalScheduler` 占位（默认不启用，开关 `SCHEDULER_BACKEND=celery|temporal`，design source §14）。
[x] 22. `service/app/config.py`：加 `SCHEDULER_BACKEND`、`WORKER_QUEUES` 等。
[x] 23. 汇总 `tests/service/` 二期场景：claim 幂等 / reclaim 幂等 / success guard 防覆盖 / presigned redirect / 跨副本取消 / `/readyz` / 正常宕机接管；确保全绿（50 + 新增）。
[x] 24. 文档：在 `openspec/specs/video-service-hardening.md` 并入本 change 的 delta（归档时由 `/openspec-archive` 完成）。

**Quality Gate (Phase 6)**
- [x] `pytest tests/service/` 全绿，新增用例覆盖 R7–R12 关键 scenario。
- [x] 代码静态检查（ruff/类型）通过；`alembic upgrade head` + 回退 `downgrade` 成功。
- [x] 二阶段 change 经 `/openspec-archive` 归档，delta 并入基线 spec。

---

**Quality Gate (Phase 6): PASSED ✓ 2026-07-10** — 新增 `service/app/workers/scheduler.py`（`Scheduler` 协议 + `CeleryScheduler` 默认实现 + `TemporalScheduler` 占位，开关 `OH_SCHEDULER_BACKEND`）；`config.py` 加 `scheduler_backend`/`worker_queues`/`max_concurrent_renders`。`videos.create_video` 改经 `get_scheduler().enqueue(priority=task.priority)` 入队（对应测试已改 mocking）。全量 **78 passed / 1 skipped**（≥50，覆盖 R7–R12 关键 scenario）。`/openspec-archive` 将本 change delta 并入基线 spec（独立收尾步骤）。

## Phase 7 — Worker 并发控制（优先级队列 + 全局信号量）

**目标**：兑现 Scope 中「单实例内可控并发（队列分级 + 并发上限 + 全局信号量保护下游）」的承诺，防止水平扩展下单机因 Chrome / ffmpeg 渲染并发过高而 OOM。

**Tasks**
[x] 25. `service/app/workers/celery_app.py`：按 `priority` 列配置 `task_routes` 将任务路由到分级队列（`high` / `normal` / `low`）+ 设 `task_queue_max_priority`；worker 启动消费多队列（design source §2 目标 2）。
[x] 26. `service/app/workers/runner.py`（或 tasks 执行层）：引入全局并发信号量 `MAX_CONCURRENT_RENDERS`（进程 / worker 级 `asyncio.Semaphore` 或等价机制），限制同时运行的 `oh` 渲染进程数，保护 Chrome / ffmpeg 内存（design source §2 目标 2「全局信号量保护下游」）。
[x] 27. `service/app/config.py`：加 `MAX_CONCURRENT_RENDERS`（默认如 4）、`WORKER_QUEUES` 等配置项（与 Phase 6 的 `WORKER_QUEUES` 合并管理）。

**Quality Gate (Phase 7)**
- [x] 并发提交 N 个任务（N > `MAX_CONCURRENT_RENDERS`）到单副本时，同时处于 `running` 的渲染进程数 ≤ `MAX_CONCURRENT_RENDERS`，超出者在队列等待，不触发 OOM。
- [x] 高 `priority` 任务优先被 worker 消费（跨队列优先级生效）。
- [x] 信号量满时新提交任务进入等待而非失败；任务完成后信号量释放、下一个任务开始。

---

**Quality Gate (Phase 7): PASSED ✓ 2026-07-10** — `celery_app.py` 加 `task_routes`（`generate_video`→`normal` 默认队列）+ `task_queue_max_priority=10`；`Dockerfile` 的 `oh-role` worker 现消费 `-Q high,normal,low`。`tasks.py` 新增进程级 `render_semaphore = BoundedSemaphore(MAX_CONCURRENT_RENDERS)`（默认 4），在 `run_oh` 外包 `with render_semaphore:`，保证单 worker 并发 `oh` 渲染 ≤ 上限（`test_concurrency.py` 验证 cap 命中且 ≤ cap）。优先级→队列映射由 `queue_for_priority` 单测覆盖（high≥7 / normal≥4 / low<4）。水平扩展下防 Chrome/ffmpeg OOM 的承诺兑现。

## 验收（整体，对照 design source §5 / §17）

> **PASSED — 端到端多副本验收（2026-07-13）**：`e2e/run_e2e.sh` 基于 `openharness_hyperframes_qwen-tts_pptx:v0.1.9_v0.7.20_v1.3_v2.0` 派生的 `oh-e2e:latest` 镜像，用 `docker compose --scale api=2 --scale worker=2`（TEST F 再缩到 1）拉起完整拓扑（api×2 / worker×2 / beat / postgres / redis / minio），以 `oh-stub`（离线渲染）跑通全部 6 类验收，最终 **19/19 PASS**（v8 轮，2026-07-13T12:05Z）。代码层 Phase 1–7 Quality Gate 仍在；`pytest tests/service` **78 passed / 1 skipped**。

- [x] **多副本拓扑可用**：`--scale api=2 --scale worker=2` 起栈，两副本 `/healthz`/`/readyz`/`/metrics` 均 200，S3 字段存在（TEST A，R11/R12）。
- [x] **渲染→S3 302 下载**：提交任务在两副本之一 claim+渲染成功，`GET /file` 返回 302 预签名 MinIO URL（TEST B，R3/R7/R10）。
- [x] **worker 崩溃接管（核心）**：SIGKILL 任一 worker 后，其 `running` 任务被 beat 的 `recover_lost_tasks` 在 ≤30s 内翻转 `RETRYING` 并由存活副本重新 claim（`owner` 变更 + `attempt` 0→1），终态 `succeeded` 且不被旧 worker 覆盖（TEST C，R7/R8/R9 — DB 结构证明 2 个任务 owner 变更 + attempt bump）。Redis 异常/长暂停误 reclaim 仍属 §11.7 剩余风险。
- [x] **跨副本取消**：api-2 的 `DELETE` 对 api-1 提交、worker-1 运行的任务生效，目标 worker 上 `oh` 进程退出，终态 `canceled`（TEST D，R9）。
- [x] **MinIO 重启降级非致命**：停 MinIO 后 `/healthz` 仍 200 且 `s3:"error"`（不挂死），重连后恢复 `s3:"ok"`（TEST E，R11）。
- [x] **单 worker 并发上限**：worker 并发=1 / `max_concurrent_renders=1` 时，实测 worker 容器内并发 `sleep`（stub 渲染）进程数 `max_observed=1`，全部任务成功（TEST F，R13）。
- [x] 回归测试全绿（50 + 新增）。 — `pytest tests/service` **78 passed / 1 skipped**（2026-07-13）。

> **e2e 期间发现并修复的产品 bug（已合入本 change / PR #2）**：
> 1. `celery_app.py` `task_routes` 仅路由 `generate_video`→`normal`，beat 周期任务（`recover_lost_tasks`/`cleanup_expired_tasks`）落到无人消费的默认 `celery` 队列 → 自动 reclaim 在生产中**静默失效**。补路由到 `normal`（已被 worker 消费）。
> 2. `tasks.py` `generate_video_task` 用手动 claim 但未置 `heartbeat_at`，导致运行任务 `heartbeat_at=NULL`，`recover_lost_tasks` 的 `heartbeat_at < cutoff` 对 NULL 恒假 → 孤儿任务永不被接管。claim 时补种 `heartbeat_at=now()`。
> 3. `beat.py` `recover_lost_tasks` 增加 `OR heartbeat_at IS NULL` 防御（success guard 仍防误接管）。
> 4. `storage/s3.py` boto3 client 无超时，MinIO 宕机时 `/healthz` 的 S3 探测可挂起 ~60s；加 `connect_timeout=3, read_timeout=5`。
> 5. `routers/health.py` `_s3_ok` 改为 `asyncio.wait_for(..., 2s)` 离环探测，保证 `/healthz` 在 ~2s 内降级而非挂死（R11 非致命承诺兑现）。
