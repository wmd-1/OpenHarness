# Implementation Tasks: Phase 3 — Multi-Tenancy, Real Temporal Migration, Strict Lease/Fencing

**Change ID:** `phase3-multitenancy-temporal-lease`

---

## Phase 1: WS-A Foundation (Data + Auth)

- [x] 1.1 Migration `004_tenant.py`（Python Alembic，非 `.sql`；repo 既有的 Alembic 约定）：建 `tenants` / `api_keys` / `quotas` / `audit_log`；`video_tasks` 加 `tenant_id`（默认 `system`）+ 索引；`down_revision=003_storage_kind`
- [x] 1.2 `models.py`：新增 `Tenant` / `ApiKey` / `Quota` / `AuditLog` ORM，并给 `VideoTask` 加 `tenant_id`（NOT NULL, default/server_default=`system`, index）
- [x] 1.3 `app/middleware/auth.py`：`X-API-Key` → SHA-256 比对 → 解析 `tenant_id` → `request.state.tenant_id`；缺失/无效/吊销/过期 → 401；内部受信头放行 `system`；`/healthz` 跳过鉴权
- [x] 1.4 `main.py` 装配 `TenantAuthMiddleware`（`sessionmaker=async_session`，`require_keys`/`trusted_header` 取自 settings）；`require_keys=False`（默认）时未带 key 放行为 `system`，兼容现有无鉴权用例
- [x] 1.5 `test_ws_a_auth.py`：无 key+require=False→system；无 key+True→401；有效/无效/吊销/过期 key；受信头绕过；healthz 跳过（独立 aiosqlite engine，不依赖 Postgres）

**Quality Gate:**
- [x] 模型经 `Base.metadata.create_all` 实跑建表通过；中间件 8 项行为（独立运行时脚本）全绿；`py_compile` 全文件通过。注：完整 pytest 需在项目已 provision 的 env 运行（本沙箱 `service/.venv` 未装依赖，已用临时 venv 做等价独立验证）

---

## Phase 2: WS-A Isolation / Quota / Audit / Rate-Limit

- [x] 2.1 DB 访问层注入 `tenant_id`：`routers` 查询（get/download/events/delete）与写（create）均带 `tenant_id`；PG 经 `SET LOCAL app.current_tenant` 驱动 RLS（见 2.9）
- [x] 2.2 `app/quota.py`：提交前查 `quotas`（并发/日限），超限 → 429
- [x] 2.3 `app/ratelimit.py`：按 `tenant_id` 限速；基于 `limits` 异步存储原语（`MemoryStorage`/`RedisStorage`），**须用 Redis backend** 以保证 `api×N` 副本下为全局共享计数（内存后端按副本独立计数，实际放行 N×rate）；以 FastAPI **依赖**形式注入（`create_video` 签名保持 `create_video(body, db)` 不被破坏）
- [x] 2.4 `app/audit.py`：变更型操作（create/cancel/delete）异步写 `audit_log`，与业务写同一事务原子提交；审计写失败非致命
- [x] 2.5 `test_ws_a_tenant_isolation.py`：跨租户 GET/DELETE → 404（system 可读任意）
- [x] 2.6 `test_ws_a_quota.py`：超并发/超日限 → 429
- [x] 2.7 `test_ws_a_ratelimit.py`：按租户限速 → 429（memory backend）
- [x] 2.8 `test_ws_a_audit.py`：审计记录存在且字段正确（action/tenant/target_type/target_id）
- [x] 2.9 Migration `005_rls.py`：PG RLS 启用 `video_tasks`/`audit_log`，按 `app.current_tenant` 隔离、`system` 豁免（PG-only，sqlite 跳过）

**Quality Gate:**
- [x] 隔离/配额/限速/审计用例全绿；Phase 1/2 回归不退化。`pytest tests/service` 全量 **99 passed**（oh-e2e:latest，sqlite + memory limiter + apply_async stub；RLS 由 PG 在 CI 校验）

---

## Phase 3: WS-B Real Temporal Migration

> 目标：把 `TemporalScheduler` 从占位空实现变为真实接入 `temporal-server` 的后端；Celery 仍为默认，行为不变。Temporal 为**可选启用**，生产默认 Celery。`docker-compose.temporal.yml` 提供独立 temporal 栈。详细迁移设计见 [`design.md`](design.md)（WS-B）。

- [x] 3.1 依赖与配置：`service/pyproject.toml` 增加 `temporalio`（异步 SDK）；`config.py` 增加 `temporal_host`（默认 `localhost:7233`）、`temporal_namespace`（默认 `default`）、`temporal_task_queue`（默认 `video-gen`）、`temporal_client_timeout`（默认 5s）
- [x] 3.2 共享渲染管线重构：把 `tasks.generate_video_task` 的渲染主体（claim → `run_oh` → 持久化终态/产物/日志/abort 检查）抽成 `app/workers/render_pipeline.py: execute_video_render(task_id)`；Celery `generate_video_task` 与 Temporal Activity **复用同一函数**，Celery 路径行为零改动（含 `claim`/`_mark_*`/`_abort_requested`/`_append_log`/`render_semaphore`）
- [x] 3.3 `app/workers/temporal_worker.py`：
  - `VideoGenWorkflow`（`@workflow.defn`，name=`VideoGenWorkflow`）：`async def run(self, task_id)` → `await workflow.execute_activity(VideoGenerationActivity.run, task_id, start_to_close_timeout=…, heartbeat_timeout=…, retry_policy=RetryPolicy(maximum_attempts=3, backoff=…))`
  - `VideoGenerationActivity`（`@activity.defn`，name=`VideoGenerationActivity`）：`async def run(self, task_id)` → 调用 `execute_video_render(task_id)`；在 activity 事件循环内以 `activity.heartbeat(...)` 周期上报（满足 `heartbeat_timeout`）
  - `main()`：建 `temporalio.client.Client.connect(temporal_host, namespace=…)` → 起 `Worker(client, task_queue, workflows=[VideoGenWorkflow], activities=[VideoGenerationActivity])`，前台运行（供 supervisord 托管）
- [x] 3.4 `scheduler.py`：`TemporalScheduler` 真正实现：`enqueue(task_id, priority=)` → 惰性建 `temporalio.client.Client` 并 `await client.start_workflow(VideoGenWorkflow.run, task_id, id=f"video-gen-{task_id}", task_queue=settings.temporal_task_queue)`，返回 workflow id；`cancel(workflow_id)` → `handle = client.get_workflow_handle(workflow_id)` → `await handle.cancel()`；`Scheduler` 协议改为 async（Celery 路径同样 await，行为不变）
- [x] 3.5 启动期 fail-fast：`OH_SCHEDULER_BACKEND=temporal` 时，(a) `temporal_worker.py` 建 client 失败即进程退出（非零）；(b) `app/main.py` 增加 startup 事件，backend=temporal 且 `temporalio.client.Client.connect` 不可达时 `raise` 使 API 容器启动失败（不静默回退 Celery）。Celery 默认路径不触碰 temporal
- [x] 3.6 部署：`docker-compose.temporal.yml` 引入 `temporalio/auto-setup`（server）+ `temporalio/ui`；openharness 服务经 supervisord 覆盖改为只跑 `api` + `temporal-worker`（不跑 celery worker/beat），`OH_SCHEDULER_BACKEND=temporal`；提供 `docker/supervisord.temporal.conf`
- [x] 3.7 测试 `test_ws_b_temporal.py`（沙箱可跑 + docker/CI 分离）：
  - 3.7.1 用 `temporalio.testing.ActivityEnvironment` 直接跑 `VideoGenerationActivity.run`，`run_oh` 以 monkeypatch 桩 + sqlite，断言终态/产物写入正确（**无需 temporal-server**）
  - 3.7.2 `get_scheduler()` 路由：`scheduler_backend=celery` → `CeleryScheduler`；`=temporal` → `TemporalScheduler`；`TemporalScheduler` 在未连 server 时 `enqueue`/`cancel` 给出清晰错误（fail-fast 行为）
  - 3.7.3 完整 e2e（起 temporal-server → enqueue/cancel 走 Temporal）标记为 **docker/CI 校验**（本沙箱无 temporal-server，同 Phase 2 e2e DEFERRED 约定），在 compose 中可跑

**Quality Gate:**
- [x] Activity 经 `ActivityEnvironment` 单测绿（沙箱）；scheduler 路由 + fail-fast 单测绿；Celery 默认路径回归全绿（`pytest tests/service` → **104 passed**）
- [ ] `docker-compose.temporal.yml` 起栈后提交/取消经 Temporal worker 执行（CI/手动 docker 校验）
- [x] 注：真实 temporal-server e2e 不在此沙箱跑（无 server 二进制），与 Phase 2 端到端 e2e 同样 DEFERRED，由 docker compose + CI 校验

---

## Phase 4: WS-C Strict Lease + Fencing

- [ ] 4.1 Migration `005_lease_token.sql`：`video_tasks` 加 `lease_token bigint`（默认 0）
- [ ] 4.2 `tasks.py`：`claim`/`reclaim` 原子自增 `lease_token` 并经 `RETURNING` 把新 token 交回调用方（`claim()` 改为返回 `(claimed, token)`，worker 内存持有当前 token）
- [ ] 4.3 `_mark_succeeded` / `_mark_failed` / `_mark_canceled` 三个终态写守卫统一升级为 `WHERE worker_id=:wid AND lease_token=:token`（旧 token → 0 行；DB 层为防御纵深，真实新增保证在 S3 写 fence，见 R20）
- [ ] 4.4 `storage/s3.py`：`save` 写入 `x-amz-meta-lease-token`；经中间映射表比对拒绝旧 token 产物
- [ ] 4.5 worker 在重渲染前 / `save` 前从 PG 重读当前 `lease_token` 与内存 token 比对，stale 则提前中止渲染 / 丢弃产物（**不引入** Redis `oh:lease:{task_id}` TTL，fence 以 PG token 为准；与 §9 一致）
- [ ] 4.6 `test_ws_c_fencing.py`：旧 token 写终态/S3 被 fence；Redis 抖动下无有效双产物

**Quality Gate:**
- [ ] fencing 用例全绿；Phase 1/2 回归不退化

---

## Phase 5: Integration & Polish

- [ ] 5.1 补建 e2e 跨租户隔离 + lease fencing 双跑用例（需运行 Docker）
- [ ] 5.2 `pytest tests/service` 全量验证（含 Phase 1/2 回归）
- [ ] 5.3 文档同步（README / 运维手册：多租户接入、temporal 切换、lease 语义）

**Quality Gate:**
- [ ] 全部测试绿；文档与实现同步

---

## Completion Checklist

- [ ] 所有 Phase 完成
- [ ] 所有 Quality Gate 通过
- [ ] 文档同步
- [ ] archive 前核对 `openspec/specs/video-service-hardening.md` 的 R8 NOTE 已同步为「strict lease」版本（删除 Phase 2 旧 NOTE 残留）
- [ ] 就绪 `/openspec-archive phase3-multitenancy-temporal-lease`
