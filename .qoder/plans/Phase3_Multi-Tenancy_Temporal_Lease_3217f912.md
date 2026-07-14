# 三期：多租户 + Temporal 实际迁移 + 严格 Lease/Fencing（OpenSpec 提案结构）

> **修订记录（2026-07-14）** —— 本文件是 Phase 2（`scale-multi-instance`，PR #2，已归档）之后、对 §14 / §15 Future Work 的正式立项规划。
> - Phase 2 已把视频服务做成多副本（api×N / worker×M）、对象存储、可观测、心跳 reclaim（非 lease）；本三期承接其明确推迟的三项：**多租户、Temporal 实际迁移、严格 Lease+Fencing**。
> - 全文区分 **已验证事实（VERIFIED）** 与 **设计建议（INFERRED）**；推断性内容标注 `[INFERRED]`。
> - 三大工作流彼此独立、可独立验收与归档；本文作为统一设计源，对应单一 OpenSpec 变更 `phase3-multitenancy-temporal-lease`。

---

## 0. 代码核实结论（VERIFIED）

以下为对 `feature/scale-multi-instance`（已归档）源文件的实读结论，是三期设计的前提。

| # | 事实 | 证据位置 |
|---|------|----------|
| 0.1 | `video_tasks` 已含 `worker_id` / `attempt` / `heartbeat_at` / `cancellation_requested` / `priority`；**尚无 `tenant_id` / `lease_token`** | `service/alembic/versions/002_*`、`app/models.py` |
| 0.2 | 调度器已抽象为 `Scheduler` 接口，`CeleryScheduler` 默认实现，`TemporalScheduler` 为**占位空实现（未接入 temporal-server）** | `app/workers/scheduler.py` |
| 0.3 | 认领/回收走 PG 行锁条件 UPDATE；`_mark_succeeded` 有 success guard（`WHERE worker_id=:wid`，**未带 token**） | `app/workers/tasks.py`、`claim()` |
| 0.4 | 取消复用 Redis `oh:abort:{task_id}`，**无 API Key / tenant 概念**；`/v1/videos` 当前无鉴权中间件 | `app/workers/tasks.py`、`routers/videos.py` |
| 0.5 | 对象存储 `VideoStorage` Protocol 含 `presigned_url`；`S3VideoStorage` 实现 `save/open/delete/exists/presigned_url`，**写路径无 token/条件写** | `app/storage/{base,s3,local}.py` |
| 0.6 | `result_backend` 保持 Redis；beat 周期任务路由到 `normal` 队列 | `app/workers/celery_app.py`、`beat.py` |
| 0.7 | `pytest tests/service` → 78 passed / 1 skipped；e2e 多副本 19/19 PASS | 仓库测试 + `e2e/e2e_report_v8.txt` |

---

## 1. 问题陈述（Problem Statement）

Phase 2 解决了「能不能横向扩、产物放哪、任务会不会丢/被覆盖」。但下列生产就绪能力仍缺：

1. **无租户隔离**：所有调用方共享同一命名空间，无法区分客户、无法按客户计费/限速/审计；任何拿到 API 的人可操作全部任务。
2. **调度后端锁死 Celery**：`TemporalScheduler` 仅是空壳，`OH_SCHEDULER_BACKEND=temporal` 切换无效；长任务（>30min）的 Activity 心跳、声明式重试、可观测性无从发挥。
3. **Ownership 非严格 lease**：Phase 2 的 reclaim 在 Redis 抖动 / 进程长暂停 / failover 下仍可能误 reclaim → 短暂双跑（§11.7 剩余风险）。对「渲染副作用不可重复 / 算力极贵 / 合规要求绝不重复」的场景不达标。

---

## 2. 目标与范围概览

三期 = 三个可独立交付的工作流（WS）：

- **WS-A 多租户**：`tenant_id` 全链路隔离 + API Key 鉴权 + 配额 + 审计 + 按租户限速。
- **WS-B Temporal 实际迁移**：`TemporalScheduler` 真实接入 `temporal-server`，`OH_SCHEDULER_BACKEND=temporal` 端到端可用；Celery 路径保留为默认。
- **WS-C 严格 Lease + Fencing**：`lease_token` 全链路 fencing，把 Phase 2「显著降低双跑」升级为「严格绝不产生有效双产物（含对象存储写）」。

---

## 3. 影响分析（Impact Analysis）

| 组件 | WS-A 多租户 | WS-B Temporal | WS-C Lease |
|------|------------|--------------|-----------|
| DB/Migration | Yes（tenant_id / tenants / api_keys / quotas / audit_log） | No | Yes（lease_token 列 + 自增） |
| 鉴权/中间件 | Yes（API Key 中间件） | No | No |
| Worker | Yes（tenant 上下文透传） | Yes（Temporal Activity） | Yes（token 携带/校验） |
| API | Yes（租户路由/限速/403） | 小（enqueue 后端切换） | No |
| 存储 | No | No | Yes（S3 条件写/元数据 token） |
| 部署 | No | Yes（temporal-server 容器，仅 temporal 路径） | No |
| 依赖 | Yes（slowapi 等） | Yes（temporalio SDK） | No |
| 测试 | Yes（隔离/鉴权/配额/审计） | Yes（temporal worker e2e） | Yes（fencing 拒绝旧 token） |

---

## 4. 风险与缓解（Risks & Mitigations）

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 多租户 scope creep（全表 tenant_id 过滤遗漏导致越权） | High | High | 统一 DB 访问层/依赖注入 tenant 上下文；强制 `tenant_id` 进主键或行级策略（RLS）；越权用例覆盖 |
| API Key 泄露 | Med | High | 密钥哈希存储、可吊销、短时效、限绑定 IP（可选）；审计记录使用 |
| Temporal 引入新运维负担 | Med | Med | temporal-server 仅 temporal 路径需要；Celery 默认不变；提供 docker-compose.temporal.yml 独立栈 |
| Lease fencing 改 S3 写路径复杂（S3 无原生 CAS） | Med | Med | 借对象元数据/`x-amz-meta-lease-token` + 版本 + 中间映射表做条件写；或仅 fence DB 终态 + 产物以 task 维度覆盖（接受旧产物被新 token 覆盖） |
| lease 续约丢失致任务卡死 | Low | Med | 续约失败计数 + 超时回退 reclaim；监控 lease 过期堆积 |
| 三工作流并发改动互相干扰 | Med | Med | 各自独立 migration 版本号、独立 Phase、独立测试目录 `tests/service/ws_a|b|c/` |

---

## 5. 成功标准（Success Criteria）

- [ ] **WS-A**：不同 tenant 的任务彼此不可见/不可操作（跨租户 GET/DELETE → 403/404）；无效 API Key → 401；超配额 → 429；每次变更写审计；按租户限速生效。
- [ ] **WS-B**：`OH_SCHEDULER_BACKEND=temporal` 启动后，提交/取消/重试经 Temporal worker 执行；Activity 心跳替代自实现；Celery 路径回归全绿。
- [ ] **WS-C**：模拟旧 owner 复苏后用旧 token 写终态/写 S3 产物 → 全部被 fence 拒绝；新 owner 正常完成；Redis 抖动下不再出现有效双产物。
- [ ] 全量 `pytest tests/service` 保持绿；新增 ws_a/b/c 用例全绿。

---

## 6. WS-A：多租户（Multi-Tenancy）

### 6.1 数据模型（VERIFIED 现状 + INFERRED 新增）

- 新增表 `tenants(id, name, status, created_at)`、`api_keys(id, tenant_id, key_hash, label, revoked, expires_at)`、`quotas(tenant_id, max_concurrent, daily_submit_limit, rate_per_min)`、`audit_log(id, tenant_id, actor_key_id, action, target_type, target_id, ts, meta_json)`。
- `video_tasks` 加 `tenant_id`（NOT NULL，默认系统租户 `system`；**FK → `tenants(id)`**，迁移须预置 `system` 行）；索引 `(tenant_id, status)`。
- **隔离强制机制（关键决策，对应 §4 最高风险）**：不依赖散落的 `WHERE tenant_id=:tid`。采用 **PostgreSQL 行级安全（RLS）** 作为硬保证——中间件在每个请求连接上 `SET LOCAL app.current_tenant = :tenant_id`，对 `video_tasks` / `audit_log` 等建 `USING (tenant_id = current_setting('app.current_tenant'))` 策略；`system` 内部调用（受信头、仅容器内）设 `app.current_tenant=system` 并可豁免。若 RLS 成本过高，退化为**集中查询层 / Repository**，所有访问强制带 tenant 过滤并由单点越权用例覆盖。`GET /v1/videos`（list）同样受约束。

### 6.2 鉴权中间件

- `app/middleware/auth.py`：`X-API-Key` → 查 `api_keys`（哈希比对，防时序）→ 解析 `tenant_id` → 写入 `request.state.tenant_id`。
- 缺失/无效/已吊销/过期 → `401`。内部服务调用可用 `tenant_id=system` + 受信头（仅容器内）。
- 越权：`tenant_id` 与资源归属不符 → `403`（或 `404` 以不泄露存在性）。

### 6.3 配额与限速

- 提交时查 `quotas`：当前 `running+pending` ≥ `max_concurrent` 或当日提交 ≥ `daily_submit_limit` → `429`。
- `slowapi` 按 `tenant_id` 限流（`rate_per_min`）；超限 → `429`。**须接入 Redis limiter backend**：Phase 2 已支持 `api×N` 副本，slowapi 默认内存后端会按副本独立计数，N 副本实际放行约 `N×rate_per_min`，失去限速意义。
- 计数来源：PG 实时聚合（小表可接受）或 Redis 计数器（推荐，带 TTL 滑动窗口）。

### 6.4 审计

- 所有变更型操作（create / cancel / delete / 状态终态）写 `audit_log`；异步（线程池/队列）避免拖慢主路径。

### 6.5 测试（INFERRED）

- `test_ws_a_tenant_isolation.py`：跨租户不可见/不可操作。
- `test_ws_a_auth.py`：缺失/无效/吊销/过期 key → 401；内部受信头。
- `test_ws_a_quota.py`：超并发/超日限 → 429。
- `test_ws_a_ratelimit.py`：按租户限速。
- `test_ws_a_audit.py`：审计记录存在且字段正确。

---

## 7. WS-B：Temporal 实际迁移（Real Temporal Migration）

### 7.1 现状（VERIFIED）

`scheduler.py` 定义 `Scheduler` 接口（`enqueue` / `cancel`）；`CeleryScheduler` 实现；`TemporalScheduler` 抛 `NotImplementedError`。`OH_SCHEDULER_BACKEND` 开关已存在。

### 7.2 设计（INFERRED）

- 依赖 `temporalio`；新增 `app/workers/temporal_worker.py` 启动 Temporal worker，注册 `VideoGenerationActivity`。
- `TemporalScheduler.enqueue` → `client.start_workflow(VideoGenWorkflow, ..., task_queue="video-gen")`。
- `TemporalScheduler.cancel` → `workflow.handle.cancel()`（替代 Redis abort key；但保留 abort key 作为 Activity 内心跳取消信号，平滑过渡）。
- `VideoGenWorkflow`：单 Activity 封装 `generate_video_task` 逻辑；Activity 心跳上报进度，`heartbeat_timeout` 触发自动取消/重试；`retry_policy` 声明式。
- `docker-compose.temporal.yml`：仅 temporal 路径引入 `temporal-server` + UI；Celery 路径不依赖。

### 7.3 测试（INFERRED）

- `test_ws_b_temporal.py`：起临时 temporal-server（测试容器/内存）→ `enqueue`/`cancel` 经 Temporal 执行；与 Celery 路径行为一致。
- 默认 Celery 回归不退化。

> 注：Temporal 路径为**可选启用**；生产默认仍 Celery。若 temporal-server 不可用，启动期显式报错而非静默回退。

---

## 8. WS-C：严格 Lease + Fencing Token

### 8.1 机理（升级 Phase 2 §11）

- `video_tasks` 加 `lease_token bigint`（或 uuid）；claim/reclaim 时**原子自增**（`UPDATE ... SET lease_token = lease_token + 1 ... RETURNING lease_token`）。`claim()` 改为返回 `(claimed: bool, token: int)`，worker 进程内存持有当前 `token`（见 D3）。
- 每次写携带 token：
  - **DB 终态（防御纵深）**：`_mark_succeeded` / `_mark_failed` / `_mark_canceled` 三个终态写守卫统一升级为 `WHERE worker_id=:wid AND lease_token=:token`。
    > **与 R9 的关系**：Phase 2 的 `recover_lost_tasks` 在 reclaim 时把 `worker_id` 置空并重新派发，新 owner 写回新 `worker_id`，因此旧 owner 的既有 `WHERE status=RUNNING AND worker_id=:wid` 守卫**已经**命中 0 行——DB 终态层在 R9 即受保护。本期 `lease_token` 守卫是**防御纵深**（重复加固）；**本期真正的新增益是下面的 S3 产物写 fence**（R9 未覆盖，是双跑落盘的唯一泄漏点）。
  - **对象存储产物（本期核心新增）**：`S3VideoStorage.save` 写入对象元数据 `x-amz-meta-lease-token=<token>`；reclaim 后新 owner 以更高 token 覆盖；旧 owner 复苏若仍以旧 token 写，S3 侧经中间映射表 / 版本比对拒绝（实现见 §8.3）。
- **避免浪费算力（替代 Redis lease TTL）**：fence 真相源是 PG `lease_token`，**不再引入** Redis `oh:lease:{task_id}` TTL（与 §9 一致，避免新增失效模式）。worker 在「重渲染前」与「`save` 前」**从 PG 重读当前 `lease_token`** 与内存 token 比对，若已 stale 则提前中止渲染 / 丢弃本地产物，而非依赖 Redis 续约。

### 8.2 与 Phase 2 的关系

- 保留 Phase 2 的 heartbeat + Redis TTL 存活检测；在其上叠加 `lease_token` fencing。
- 不推翻现有 claim/reclaim 状态机；仅扩展列与写路径校验。
- 把 R8「non-lease」升级为「strict lease + fencing」——见 delta MODIFIED R8。
- **跨后端一致性（重要，对应 F5）**：本 WS 的 lease fence 依赖 reclaim 来 bump `lease_token`；而 reclaim 与心跳存活注册当前**耦合在 Celery**（`beat.py` 的 `worker_process_init` 信号启动 liveness 线程、Celery beat 周期任务 `recover_lost_tasks`）。若 `OH_SCHEDULER_BACKEND=temporal`（WS-B）下不运行 Celery 渲染 worker，则无人 bump token，本 WS 的「严格 lease」保证在 Temporal 路径下不成立。二选一（须在 WS-B §7 落实）：
  1. **范围声明**：WS-C 严格 lease 仅保证在 **Celery 后端**有效；Temporal 路径下由 Temporal 自身的 Activity heartbeat + timeout 机制承担租约/取消（不沿用 `beat.py` reclaim）。
  2. **抽离 reclaim**：把 `recover_lost_tasks` / watch-dog 改为与调度后端无关的独立进程或 PeriodicTask，使 Temporal Activity 死亡时也能 bump token。

### 8.3 S3 条件写实现（INFERRED）

S3 无原生行级 CAS，采用其一：
1. **版本 + 元数据比对**：产物 key 带版本；写前读当前 `x-amz-meta-lease-token`，仅当新 token > 旧 token 才允许 PUT（用 `If-Match` 绑 ETag 或先 HEAD 比对）。
2. **中间映射表**：`lease_map(task_id → current_token)` 置于 PG/Redis；写 S3 前校验内存 token == 映射表 token，否则丢弃本地产物（不 PUT）。
推荐方案 2（更简单、与 DB guard 同源），代价是旧 owner 可能本地渲染完但产物被丢弃（无副作用，符合「严格绝不双跑」）。

### 8.4 测试（INFERRED）

- `test_ws_c_fencing.py`：
  - **DB 终态（防御纵深回归）**：旧 token 写 `_mark_succeeded` / `_mark_failed` / `_mark_canceled` → 0 行（被 fence）；并断言即便 `worker_id` 仍匹配、token 不符也被拒（直接验证 token 守卫，而非仅靠 R9 的 worker_id）。
  - **S3 产物（本期核心）**：旧 token 写 S3 产物 → 被拒绝/丢弃，最终产物属新 token。
  - 模拟 Redis 抖动 + 旧进程复苏：不产生有效双产物（旧 owner 本地可能渲染完，但 `save` 前 PG 重读 token 不符 → 丢弃）。

---

## 9. 架构考量（Architecture Considerations）

- WS-A 的 `tenant_id` 通过 `request.state` → 依赖注入到 DB session/worker context；所有既有 seam（`tasks.py` / `storage` / `routers`）在调用处补 `tenant_id`，不重写核心逻辑。隔离强制见 §6.1（RLS 或集中查询层）。
- WS-B 复用 Phase 2 的 `Scheduler` 抽象；仅补 `TemporalScheduler` 真正实现，符合「不锁死后端」。
- WS-C 复用 Phase 2 的 `worker_id` / `heartbeat_at` / `attempt` 列，新增 `lease_token`，向前兼容（历史任务 `lease_token=0` 视为无 fencing，新 claim 从 1 起）。
- 三者均保持 `result_backend=Redis`、复用 Redis abort key（WS-B 内取消信号）。
- **WS-C fence 真相源是 PG `lease_token`**，不引入 Redis `oh:lease:{task_id}` TTL；**WS-B × WS-C 后端耦合**见 §8.2（Temporal 路径下须明确 lease fence 来源）。

---

## 10. 仍推迟项（Future Work，非三期必做）

- 多租户计费/账务、租户级 Grafana 看板、SSO/OAuth 登录（本期仅 API Key）。
- Temporal 工作流编排更复杂拓扑（多 Activity 分片渲染）、Temporal 集群 HA。
- Lease fencing 的「分布式锁服务」（如基于 etcd）替代 Redis 做更高可用租约（本期仍 Redis）。
- 任务优先级抢占式调度、scheduler 多后端热切换。

---

## 11. 交付清单（Delivery Checklist）

| 模块 | 文件 | 备注 |
|------|------|------|
| 多租户模型 | `service/alembic/versions/004_tenant.sql` + `models.py` | tenants / api_keys / quotas / audit_log / video_tasks.tenant_id |
| 鉴权中间件 | `app/middleware/auth.py` + `main.py` 装配 | X-API-Key → tenant |
| 配额/限速 | `app/routers/videos.py` + `app/quota.py` | slowapi 按 tenant |
| 审计 | `app/audit.py` | 异步写 audit_log |
| Temporal | `app/workers/temporal_worker.py` + `scheduler.py` 实现 `TemporalScheduler` | temporalio；docker-compose.temporal.yml |
| Lease | `service/alembic/versions/005_lease_token.sql` + `tasks.py` + `storage/s3.py` | lease_token 自增 + fencing 写 |
| 测试 | `tests/service/ws_a_*.py` `ws_b_*.py` `ws_c_*.py` | 独立目录 |
| 依赖 | `service/pyproject.toml` | slowapi / temporalio |

---

## 12. 验收标准（按工作流）

- **WS-A**：`tests/service/ws_a_*` 全绿；手动用两个 tenant 的 key 验证隔离/配额/审计/限速。
- **WS-B**：`tests/service/ws_b_*` 全绿；`OH_SCHEDULER_BACKEND=temporal` 起 temporal-server 后提交/取消走 Temporal。
- **WS-C**：`tests/service/ws_c_*` 全绿；旧 token 写被 fence（DB + S3）；Redis 抖动下无有效双产物。
- 整体：`pytest tests/service` 全绿（含 Phase 1/2 回归）。

> 说明：e2e 多副本验收（类比 Phase 2 的 19/19）在 WS-A/WS-C 完成后补建对应 TEST 用例（跨租户隔离、lease fencing 双跑），需运行 Docker 环境。
