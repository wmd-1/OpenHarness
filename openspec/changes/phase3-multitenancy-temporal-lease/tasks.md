# Implementation Tasks: Phase 3 — Multi-Tenancy, Real Temporal Migration, Strict Lease/Fencing

**Change ID:** `phase3-multitenancy-temporal-lease`

---

## Phase 1: WS-A Foundation (Data + Auth)

- [ ] 1.1 Migration `004_tenant.sql`：建 `tenants` / `api_keys` / `quotas` / `audit_log`；`video_tasks` 加 `tenant_id`（默认 `system`）+ 索引
- [ ] 1.2 `models.py`：新增租户相关 ORM 模型与 `VideoTask.tenant_id` 关系
- [ ] 1.3 `app/middleware/auth.py`：`X-API-Key` → 哈希比对 → 解析 `tenant_id` → `request.state.tenant_id`；缺失/无效/吊销/过期 → 401
- [ ] 1.4 `main.py` 装配鉴权中间件（内部受信头放行 `tenant_id=system`）
- [ ] 1.5 `test_ws_a_auth.py`：缺失/无效/吊销/过期 → 401；内部受信头

**Quality Gate:**
- [ ] 迁移可升级；鉴权中间件单测通过

---

## Phase 2: WS-A Isolation / Quota / Audit / Rate-Limit

- [ ] 2.1 DB 访问层注入 `tenant_id`：`tasks.py` / `storage` / `routers` 查询与写均带 `tenant_id`
- [ ] 2.2 `app/quota.py`：提交前查 `quotas`（并发/日限），超限 → 429
- [ ] 2.3 `routers/videos.py`：按 `tenant_id` 限速（slowapi `rate_per_min`）；**须用 Redis limiter backend** 以保证 `api×N` 副本下为全局共享计数（slowapi 默认内存后端会按副本独立计数，实际放行 N×rate）
- [ ] 2.4 `app/audit.py`：变更型操作异步写 `audit_log`
- [ ] 2.5 `test_ws_a_tenant_isolation.py`：跨租户 GET/DELETE → 403/404
- [ ] 2.6 `test_ws_a_quota.py`：超并发/超日限 → 429
- [ ] 2.7 `test_ws_a_ratelimit.py`：按租户限速
- [ ] 2.8 `test_ws_a_audit.py`：审计记录存在且字段正确

**Quality Gate:**
- [ ] 隔离/配额/限速/审计用例全绿；Phase 1/2 回归不退化

---

## Phase 3: WS-B Real Temporal Migration

- [ ] 3.1 依赖 `temporalio`；`app/workers/temporal_worker.py` 启动 Temporal worker
- [ ] 3.2 `scheduler.py`：`TemporalScheduler.enqueue` → `start_workflow`；`cancel` → `workflow handle.cancel`
- [ ] 3.3 `VideoGenWorkflow` + `VideoGenerationActivity`：封装 `generate_video_task`；Activity 心跳 + `retry_policy`
- [ ] 3.4 `docker-compose.temporal.yml`：引入 `temporal-server` + UI（仅 temporal 路径）
- [ ] 3.5 `OH_SCHEDULER_BACKEND=temporal` 启动期校验 temporal 可达，否则显式报错
- [ ] 3.6 `test_ws_b_temporal.py`：起 temporal-server → enqueue/cancel 经 Temporal；Celery 默认路径回归

**Quality Gate:**
- [ ] Temporal 路径端到端可用；Celery 默认回归全绿

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
