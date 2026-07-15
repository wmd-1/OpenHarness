# OpenHarness Video Service — Operations Runbook

HyperFrames 视频生成服务（`service/`）的部署与运维手册。覆盖三大 Phase 3 能力：
**多租户隔离**、**可插拔调度后端（Celery / Temporal）**、**严格租约 + fencing**。

> 默认调度后端是 **Celery**（`OH_SCHEDULER_BACKEND=celery`）。Temporal 为可选启用，
> 生产默认仍走 Celery。对象存储默认 **本地**（`OH_STORAGE_KIND=local`），可切 S3。

---

## 1. 多租户接入（WS-A）

每个调用方通过 `X-API-Key` 头解析到 `tenant_id`，所有任务操作（list / get / create /
cancel / delete / download）按 `tenant_id` 隔离；跨租户访问返回 `403`（或 `404` 不暴露存在性）。

### 1.1 鉴权中间件

| 项 | 行为 |
|----|------|
| 请求头 | `X-API-Key: <raw-key>` |
| 解析 | 对 raw key 做 SHA-256 → 比对 `api_keys.key_hash`（唯一索引） |
| 失败 | 缺失 / 无效 / 已吊销(`revoked=True`) / 过期(`expires_at`) → `401` |
| 内部调用 | 受信头（配置 `trusted_header`）可直通为 `tenant_id=system` |
| 健康检查 | `GET /healthz` 跳过鉴权 |
| 兼容模式 | `OH_REQUIRE_KEYS=False`（默认）时未带 key 放行并记为 `system`，兼容既有无鉴权用例 |

### 1.2 接入一个新租户

数据模型（`app/models.py`）：

- `tenants`：`id`(PK) / `name` / `status`(默认 `active`)
- `api_keys`：`id` / `tenant_id` / `key_hash`(SHA-256 hex, **唯一**) / `label` / `revoked` / `expires_at`
- `quotas`：`tenant_id`(PK) / `max_concurrent`(默认 2) / `daily_submit_limit`(默认 100) / `rate_per_min`(默认 10)
- `audit_log`：`tenant_id` / `actor_key_id` / `action` / `target_type` / `target_id` / `ts` / `meta_json`

```sql
-- 1) 建租户
INSERT INTO tenants (id, name, status) VALUES (gen_random_uuid(), 'acme', 'active');

-- 2) 生成密钥（明文仅展示一次），入库存 SHA-256 摘要
--    key_hash = sha256(raw_key) 的 hex 串
INSERT INTO api_keys (id, tenant_id, key_hash, label, revoked, expires_at)
VALUES (gen_random_uuid(), '<tenant_id>', '<sha256_hex>', 'default', false, NULL);

-- 3) 配额（不插则用默认值：并发 2 / 日限 100 / 10 req/min）
INSERT INTO quotas (tenant_id, max_concurrent, daily_submit_limit, rate_per_min)
VALUES ('<tenant_id>', 4, 500, 30);
```

客户端随后在请求带 `X-API-Key: <raw_key>` 即可；超并发/超日限 → `429`，超速率 → `429`。

### 1.3 隔离与审计

- 隔离：API 层查询/写入均带 `tenant_id`；PG 经 `SET LOCAL app.current_tenant` 驱动 RLS
  （`video_tasks` / `audit_log` 按 `current_tenant` 隔离，`system` 豁免；RLS 仅 PG 生效，sqlite 跳过）。
- 审计：create / cancel / delete 等变更操作异步写 `audit_log`，与业务写同事务原子提交。

---

## 2. 调度后端切换：Celery ↔ Temporal（WS-B）

调度器经 `Scheduler` 协议抽象，`OH_SCHEDULER_BACKEND` 切换：

| 后端 | 环境变量 | 说明 |
|------|----------|------|
| Celery（默认） | `OH_SCHEDULER_BACKEND=celery` | 沿用 Phase 2 的 Celery worker + beat，行为不变 |
| Temporal（可选） | `OH_SCHEDULER_BACKEND=temporal` | 真实接入 `temporal-server`，任务经 `VideoGenWorkflow` + `VideoGenerationActivity` 执行 |

### 2.1 启用 Temporal

相关配置（`app/config.py`）：

- `OH_TEMPORAL_HOST`（默认 `localhost:7233`）
- `OH_TEMPORAL_NAMESPACE`（默认 `default`）
- `OH_TEMPORAL_TASK_QUEUE`（默认 `video-gen`）
- `OH_TEMPORAL_CLIENT_TIMEOUT`（默认 `5`s）

启动期 **fail-fast**：`OH_SCHEDULER_BACKEND=temporal` 且 `temporal-server` 不可达时，
API 容器启动直接失败（**不会静默回退 Celery**）。

### 2.2 本地 / CI 起 Temporal 栈

`docker-compose.temporal.yml` 提供独立 temporal 栈（server + ui + worker + api 覆盖）：

```bash
# 起 temporal 栈（api + temporal-worker，不跑 celery worker/beat）
OH_SCHEDULER_BACKEND=temporal docker compose -f docker-compose.temporal.yml up
# 访问 Temporal UI
open http://localhost:8088
```

- `VideoGenWorkflow`：声明式重试（`maximum_attempts=3`）+ `heartbeat_timeout=30s`。
- `VideoGenerationActivity.run`：调用共享渲染管线 `execute_video_render(task_id)`，
  在 activity 事件循环内周期 `activity.heartbeat(...)` 以续命心跳。
- **Celery 与 Temporal 复用同一份渲染实现**（`render_pipeline.execute_video_render`），
  渲染行为零差异；共享渲染管线是 Phase 3 抽出的单一事实源。

### 2.3 端到端验收说明

真实 `temporal-server` 全链路 e2e（起 server → enqueue/cancel 走 Temporal worker）依赖
Docker daemon + temporal 二进制，沙箱未跑，按 Phase 2 端到端 e2e 惯例由
**docker compose + CI 校验**（compose 已就绪，可手动跑）。单测用
`temporalio.testing.ActivityEnvironment` 直接驱动 Activity，无需 server。

---

## 3. 严格租约 + Fencing 语义（WS-C / R20）

目标：把 Phase 2「心跳 + TTL（非 lease）」升级为**严格 lease**——被抢占（reclaim）的旧
owner **无法产生任何有效副作用**（既不写终态，也不落对象存储产物）。

### 3.1 机制

- `video_tasks.lease_token BIGINT NOT NULL DEFAULT 0`。
- `claim()` 返回 `(claimed, token)`，**原子自增** `lease_token` 并经 `RETURNING` 交回新 token
  （首次 claim → `1`，避免 `NULL+1` 歧义）。worker 进程用模块级字典持有当前 token。
- `recover_lost_tasks` 重占时同步 `lease_token = lease_token + 1`，旧 owner 立即失效。
- 三个终态写（`_mark_succeeded` / `_mark_failed` / `_mark_canceled`）在传入 token 时追加
  `WHERE worker_id=:wid AND lease_token=:token` 守卫：旧 token → 0 行（DB 层防御纵深）。
- 对象存储写（**主新增保证**）：`storage.save(task_id, src, lease_token=...)` 携带 token；
  `fence_artifact()` 经 `video_lease_fence` 映射表比对，仅接受**严格更高** token，旧 token
  产物被丢弃；S3 路径还会写 `x-amz-meta-lease-token`。
- 渲染管线在 `save` 前从 PG 重读 `lease_token` 与内存 token 比对，stale 则提前丢弃产物并中止。

### 3.2 语义要点（权威）

`lease_token` 表示**任务执行所有权**，仅在所有权转移时变化：首次 `claim`（新 owner）或
`reclaim`（旧 owner 被判死、重派）。同一 owner 的本地重试 / Temporal Activity 重试
（同一 workflow 实例）**不** bump token，沿用同一 token，因此 fence 永不拒绝 owner 自身的写。
Celery 与 Temporal 两条路径遵守同一条 bump 规则。

### 3.3 保证与残留

- 升级后：被抢占 owner 的后续写（终态 + 对象存储产物）**全部被 fence**，无有效重复副作用留存。
- 残留：被抢占 owner 仍可能**浪费算力**在本地渲染（render 本身不可中断）；保证的是
  "无有效重复终态 / 产物存活"，符合 R20。
- Redis 抖动（注册键短暂丢失 → 误 reclaim）：旧 token 的所有写被 fence，不产生有效双产物。

---

## 4. 常用配置速查（`app/config.py`）

| 变量 | 默认 | 说明 |
|------|------|------|
| `OH_SCHEDULER_BACKEND` | `celery` | `celery` / `temporal` |
| `OH_REQUIRE_KEYS` | `false` | 是否强制要求 `X-API-Key` |
| `OH_STORAGE_KIND` | `local` | `local` / `s3` |
| `OH_TEMPORAL_HOST` | `localhost:7233` | Temporal server 地址 |
| `OH_TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `OH_TEMPORAL_TASK_QUEUE` | `video-gen` | Temporal task queue |
| `OH_TEMPORAL_CLIENT_TIMEOUT` | `5` | 客户端连接超时（秒） |
| `OH_BIN` / `OH_HEADLESS_SHELL_PATH` | — | `oh` 可执行与 headless shell 路径 |

---

## 5. 测试

```bash
pytest tests/service          # 全量（含 Phase 1/2 回归 + WS-A/B/C 用例）
```

- WS-C fencing 单测：`tests/service/test_ws_c_fencing.py`（旧 token 终态写被 fence、产物被
  fence、渲染中 reclaim 丢弃、stale 心跳拒绝）。
- WS-B：`tests/service/test_ws_b_temporal.py`（`ActivityEnvironment` 驱动 Activity，无需
  temporal-server）。
- 涉及 PG RLS 的用例在 CI（真实 Postgres）校验；sqlite 下跳过 RLS 部分。
