# Tasks: Scale HyperFrames Video Service to Multi-Instance

**Change ID:** `scale-multi-instance`
**Status:** Draft
**Baseline:** `openspec/specs/video-service-hardening.md`
**Design source:** `.qoder/plans/Phase2_Multi-Instance_Scaling_3217f912.md`

实施严格按 Phase 1 → 7 顺序进行（新增 Phase 7 为单实例并发控制，兑现 Scope 承诺）。每个 Phase 末尾有 **Quality Gate**；门禁通过方可进入下一 Phase。所有改动**不重写一期逻辑**，仅在既有 seam（`app/workers/tasks.py`、`runner.run_oh`、`storage/base.py` 等）内扩展。

> **关键定性（贯穿全程）**：Ownership / Reclaim 是 **heartbeat + Redis TTL** 机制，**不是**严格 lease/fencing。它能显著降低双跑、并借 success guard（DB 行级条件）可靠防止终态覆盖，但**不能严格证明绝不双跑**。Redis 抖动 / 进程长暂停 / failover 下的误 reclaim 属已知剩余风险（design source §11.7），不纳入正常宕机验收，也不作为门禁用例。

---

## Phase 1 — 数据模型与行锁状态机

**目标**：为行锁 claim / 存活检测 / 取消持久化打基础，并落地「终态不被错误 owner 覆盖」的强保证。

**Tasks**
1. `service/alembic/versions/` 新增迁移：在 `video_tasks` 加列
   - `worker_id VARCHAR`（当前持有副本标识）
   - `attempt INTEGER NOT NULL DEFAULT 0`
   - `heartbeat_at TIMESTAMP`（可空）
   - `cancellation_requested BOOLEAN NOT NULL DEFAULT FALSE`
   - `priority INTEGER NOT NULL DEFAULT 5`
   - backfill：`attempt=0`、`priority=5`、`cancellation_requested=false`、`heartbeat_at=NULL`。
   > **回滚顺序（重要）**：本迁移新增的列会被后续 Phase 代码依赖。回滚时必须**先回退引用这些新列的代码（或整体回退版本），再执行 `alembic downgrade`**；不可先 `downgrade` 后运行新代码，否则服务启动即因缺列报错。
2. `service/app/workers/tasks.py` 实现 `claim(task_id, worker_id) -> bool`：原子条件 UPDATE
   ```sql
   UPDATE video_tasks SET status='running', started_at=now(),
       worker_id=:wid, attempt=attempt+1, heartbeat_at=now()
    WHERE id=:tid AND status IN ('queued','retrying')
   RETURNING id
   ```
3. `tasks.py` 实现 `_mark_succeeded(task_id, current_wid, ...)` 的 **success guard**：
   ```sql
   UPDATE video_tasks SET status='succeeded', finished_at=now(), output_path=:p, file_size_bytes=:s
    WHERE id=:tid AND status='running' AND worker_id=:current_wid
   ```
   （`worker_id` 不符 → 0 行 → 拒绝写入，防 clobber）

**Quality Gate (Phase 1)**
- [ ] `tests/service/` 新增 `claim` 幂等场景：两 worker 并发 claim 同一 `queued` 任务，仅一个 `UPDATE` 命中，`worker_id` 唯一。
- [ ] 新增 success guard 场景：任务被 reclaim / 换 owner 后，旧 `worker_id` 调 `_mark_succeeded` 写入 0 行、终态不被覆盖。
- [ ] 迁移可 `alembic upgrade head` 成功，旧数据 backfill 正确。

---

## Phase 2 — Worker 存活注册 / 心跳 / 幂等 Reclaim / 取消持久化

**目标**：让「任务可被任意副本安全接管」在工程上成立（heartbeat 机制，非 lease）。

**Tasks**
4. worker 启动时生成 `worker_id`，后台线程每 **10s** 刷新 Redis 键 `oh:worker:{worker_id}`，TTL = **20s**（design source §11.2）。
5. owner worker 每 **10s** `UPDATE video_tasks SET heartbeat_at=now() WHERE id=:tid AND worker_id=:wid`。
6. 新增 `service/app/workers/beat.py`：`recover_lost_tasks()` 每 30s 扫描
   ```sql
   SELECT id, worker_id FROM video_tasks
    WHERE status='running'
      AND heartbeat_at < now() - interval '60s'
      AND worker_id != ALL(:alive_workers)   -- alive_workers 由应用层查 Redis 得到
   ```
   > **注意（易错点）**：`worker_alive` **不是** PostgreSQL 函数，不能在 SQL 内调用 Redis。reclaim 前**先在应用层**批量查 Redis 键 `oh:worker:{worker_id}`（TTL 20s），收集「当前存活」的 `worker_id` 集合 `alive_workers`，再把它作为 `!= ALL(:alive_workers)`（或等价 `worker_id NOT IN (...)`）参数传入上述 SQL。
   翻转用条件 UPDATE（`SET status='retrying', worker_id=NULL, attempt=attempt+1 WHERE ... AND worker_id != ALL(:alive_workers)`），仅抢到翻转的 beat 才 `apply_async` 重投（行锁保证幂等、无双投）。
7. `DELETE /v1/videos/{id}`：写 `cancellation_requested=true`（DB）+ 置 Redis `oh:abort:{task_id}=1`（双写，跨副本复用一期 abort key，design source §12）。

**Quality Gate (Phase 2)**
- [ ] 新增 reclaim 幂等场景：多 beat 并发 `recover_lost_tasks`，仅一个翻转 `running→retrying` 且仅重投一次（行锁幂等，强保证）。
- [ ] 新增存活场景（正常 Redis）：worker 存活并刷新注册键时，beat 不 reclaim（注册键在 → 判定 owner 存活）。注：此场景正确性依赖 Redis 可用（design source §11.7 B1–B4 剩余风险，不门禁）。
- [ ] 取消场景：跨副本 `DELETE` 后目标 worker 上 `oh` 进程退出，终态 `canceled`。

---

## Phase 3 — 对象存储抽象 + `/file` 默认 302

**目标**：产物迁对象存储，下载默认返回签名 URL。

**Tasks**
8. `service/app/storage/base.py`：`VideoStorage` Protocol 增加 `presigned_url(key, expires=3600) -> str | None`；`LocalVideoStorage.presigned_url` 返回 `None`。
9. 新增 `service/app/storage/s3.py`：`S3VideoStorage` 实现 `save/open/delete/exists/presigned_url`（补齐 `delete`/`exists`，消除 design source I1）。
10. `service/app/routers/videos.py`：`GET /v1/videos/{id}/file`
    - 默认 `mode=redirect` → 302 到 `presigned_url`（`storage_kind=s3` 时）；`storage_kind=local` 或 `presigned_url is None` → 回退流式（MODIFY R3）。
    - `?mode=stream` → `StreamingResponse`（兼容一期，不阻塞事件循环）。
11. `service/app/config.py`：加 `OH_STORAGE_KIND`、`OH_S3_ENDPOINT`、`OH_S3_BUCKET`、`OH_S3_REGION`、`OH_S3_ACCESS_KEY`/`SECRET` 等。
12. DB `video_tasks` 记录 `storage_kind`（回退路由用，design source R4）。
   > **存量迁移说明**：本 change 仅让**新任务**默认 `storage_kind=s3`；**存量视频不强制回填**，仍按 `local` 继续流式下载。如需迁移存量，提供可选的 `migrate_local_to_s3` 脚本（非门禁，按需执行）。

**Quality Gate (Phase 3)**
- [ ] 新增 presigned redirect 场景：`storage_kind=s3` 且默认 `mode` → 302 + `Location: <presigned>`；`?mode=stream` / `local` → 200 流式。
- [ ] `S3VideoStorage` 单测：`delete`/`exists`/`presigned_url` 均实现（fake S3 / moto 或 stub）。
- [ ] `LocalVideoStorage.presigned_url` 返回 `None` 且 API 回退流式。

---

## Phase 4 — 拓扑拆分（docker-compose.prod.yml + OH_ROLE）

**目标**：api×N / worker×M 独立 service，任意水平扩展。

**Tasks**
13. 新增 `docker-compose.prod.yml`：拆分 `api` / `worker` / `beat`（+ `minio` / `postgres` / `redis`），保留 `PYTHONPATH=/app/src:/opt/oh-service` 与 `working_dir: /opt/oh-service`（design source §7，修正 I2）。
14. 用 `OH_ROLE` 环境变量切换入口（`api` → uvicorn；`worker` → celery worker；`beat` → celery beat）；保留 `oh-serve` 单容器 supervisord 作 fallback。
15. 启动用 `docker compose -f docker-compose.prod.yml up -d --scale worker=N --scale api=M`（非 swarm，`replicas` 被忽略，design source §7 修正 I3）。
16. `service/pyproject.toml`：加 `boto3`/`botocore`、`prometheus-fastapi-instrumentator`、`opentelemetry-*`、`structlog`、`psutil`（多租户依赖 `slowapi` 本期不加）。

**Quality Gate (Phase 4)**
- [ ] `docker compose -f docker-compose.prod.yml config` 校验通过；`--scale worker=5 --scale api=3` 能拉起（单测/CI 可用 compose 构建验证，端到端多副本留给 §5 验收）。
- [ ] `docker-compose.yml`（一期单容器）未受影响，仍可 `oh-serve` 启动。

---

## Phase 5 — 可观测性 + `/readyz`

**目标**：metrics / traces / structured logs / 健康检查。

**Tasks**
17. 新增 `service/app/observability/metrics.py`：`prometheus-fastapi-instrumentator` + 自定义 `oh_render_duration_seconds`、`oh_render_inflight`；celery-exporter 或自采 worker 指标。
18. 新增 `service/app/observability/tracing.py`：OpenTelemetry `instrumentation-{fastapi,celery,sqlalchemy,redis,boto3}`，OTLP 导出（缺省可关，design source R8）。
19. 新增 `service/app/observability/logging.py`：`structlog` JSON 日志，每行带 `task_id`/`worker_id`/`attempt`。
20. `service/app/health.py` 新增 `/readyz`（队列消费状态：心跳滞后、待处理数）；`/healthz` 加 S3 ping（`storage_kind=s3` 时）。

**Quality Gate (Phase 5)**
- [ ] 新增 `/readyz` 场景：服务运行 → 返回队列消费状态（200）；S3 不可达时 `/healthz` 反映降级但不致命。
- [ ] 指标端点暴露 `oh_render_inflight` 等（测试环境可 scrape 验证）。

---

## Phase 6 — 调度器抽象 + 全量测试套件

**目标**：为未来 Temporal 迁移留抽象与开关；补齐二期全部回归用例。

**Tasks**
21. 新增 `service/app/workers/scheduler.py`：`Scheduler` 接口（`enqueue` / `cancel`）；`CeleryScheduler` 默认实现；`TemporalScheduler` 占位（默认不启用，开关 `SCHEDULER_BACKEND=celery|temporal`，design source §14）。
22. `service/app/config.py`：加 `SCHEDULER_BACKEND`、`WORKER_QUEUES` 等。
23. 汇总 `tests/service/` 二期场景：claim 幂等 / reclaim 幂等 / success guard 防覆盖 / presigned redirect / 跨副本取消 / `/readyz` / 正常宕机接管；确保全绿（50 + 新增）。
24. 文档：在 `openspec/specs/video-service-hardening.md` 并入本 change 的 delta（归档时由 `/openspec-archive` 完成）。

**Quality Gate (Phase 6)**
- [ ] `pytest tests/service/` 全绿，新增用例覆盖 R7–R12 关键 scenario。
- [ ] 代码静态检查（ruff/类型）通过；`alembic upgrade head` + 回退 `downgrade` 成功。
- [ ] 二阶段 change 经 `/openspec-archive` 归档，delta 并入基线 spec。

---

## Phase 7 — Worker 并发控制（优先级队列 + 全局信号量）

**目标**：兑现 Scope 中「单实例内可控并发（队列分级 + 并发上限 + 全局信号量保护下游）」的承诺，防止水平扩展下单机因 Chrome / ffmpeg 渲染并发过高而 OOM。

**Tasks**
25. `service/app/workers/celery_app.py`：按 `priority` 列配置 `task_routes` 将任务路由到分级队列（`high` / `normal` / `low`）+ 设 `task_queue_max_priority`；worker 启动消费多队列（design source §2 目标 2）。
26. `service/app/workers/runner.py`（或 tasks 执行层）：引入全局并发信号量 `MAX_CONCURRENT_RENDERS`（进程 / worker 级 `asyncio.Semaphore` 或等价机制），限制同时运行的 `oh` 渲染进程数，保护 Chrome / ffmpeg 内存（design source §2 目标 2「全局信号量保护下游」）。
27. `service/app/config.py`：加 `MAX_CONCURRENT_RENDERS`（默认如 4）、`WORKER_QUEUES` 等配置项（与 Phase 6 的 `WORKER_QUEUES` 合并管理）。

**Quality Gate (Phase 7)**
- [ ] 并发提交 N 个任务（N > `MAX_CONCURRENT_RENDERS`）到单副本时，同时处于 `running` 的渲染进程数 ≤ `MAX_CONCURRENT_RENDERS`，超出者在队列等待，不触发 OOM。
- [ ] 高 `priority` 任务优先被 worker 消费（跨队列优先级生效）。
- [ ] 信号量满时新提交任务进入等待而非失败；任务完成后信号量释放、下一个任务开始。

---

## 验收（整体，对照 design source §5 / §17）

- [ ] `--scale worker=5 --scale api=3` 稳定接收 100 并发提交。
- [ ] 杀掉任意 worker 容器（进程真死、注册键正常过期的**正常场景**），其 `running` 任务 ≤ 90s 内被另一副本安全接管，终态不被旧 worker 覆盖（success guard 强保证）。Redis 异常/长暂停下误 reclaim 属 §11.7 剩余风险，不纳入本条。
- [ ] `DELETE /v1/videos/{id}` ≤ 5s 内目标 worker 上 `oh` 进程退出，终态 `canceled`（跨副本）。
- [ ] Grafana 可见 `oh_render_inflight` / `oh_render_duration_seconds_bucket`，p95 持续 30 分钟无异常。
- [ ] MinIO 重启后 API 重连成功，已完成任务下载链接仍可用（存量回退流式）。
- [ ] 回归测试全绿（50 + 新增）。
