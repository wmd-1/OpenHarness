# Proposal: Phase 3 — Multi-Tenancy, Real Temporal Migration, Strict Lease/Fencing

**Change ID:** `phase3-multitenancy-temporal-lease`
**Created:** 2026-07-14
**Status:** Draft
**Baseline:** `.qoder/plans/FastAPI_Hyperframes_Video_Service_3217f912.md` (一期 plan) + `openspec/specs/video-service-hardening.md` (基线，含 R1–R13)
**Design source:** `.qoder/plans/Phase3_Multi-Tenancy_Temporal_Lease_3217f912.md`

---

## Problem Statement

Phase 2（`scale-multi-instance`，已归档）已让视频服务支持多副本、对象存储、可观测性与心跳 reclaim（**非 lease**）。但下列生产就绪能力仍缺，均为 Phase 2 §14/§15 明确推迟项：

1. **无租户隔离**：所有调用方共享同一命名空间，无法区分客户、无法按客户计费/限速/审计；任何拿到 API 的人可操作全部任务。
2. **调度后端锁死 Celery**：`TemporalScheduler` 仅是占位空实现，`OH_SCHEDULER_BACKEND=temporal` 切换无效；长任务（>30min）的 Activity 心跳、声明式重试无从发挥。
3. **Ownership 非严格 lease**：Phase 2 的 reclaim 在 Redis 抖动 / 进程长暂停 / failover 下仍可能误 reclaim → 短暂双跑（§11.7 剩余风险），对「渲染副作用不可重复 / 算力极贵 / 合规要求绝不重复」不达标。

## Proposed Solution

三大工作流（WS），彼此独立、可独立验收/归档：

1. **WS-A 多租户**：`tenant_id` 全链路隔离 + `X-API-Key` 鉴权中间件 + 配额（并发/日限）+ 审计日志 + 按租户限速（slowapi）。
2. **WS-B Temporal 实际迁移**：实现 `TemporalScheduler`（接入 `temporal-server`），`OH_SCHEDULER_BACKEND=temporal` 端到端可用；Celery 路径保留为默认。
3. **WS-C 严格 Lease + Fencing**：`video_tasks` 加 `lease_token`，claim/reclaim 原子自增；每次写（DB 终态 + S3 产物）携带 token 并被 fence 拒绝旧 token，把 Phase 2「显著降低双跑」升级为「严格绝不双跑（含对象存储写）」。

## Detailed Design

完整代码级设计见 **[`.qoder/plans/Phase3_Multi-Tenancy_Temporal_Lease_3217f912.md`](../../.qoder/plans/Phase3_Multi-Tenancy_Temporal_Lease_3217f912.md)**，章节映射：

| 主题 | Plan 章节 |
|------|-----------|
| 代码核实（VERIFIED） | §0 |
| 问题 / 目标 / 范围 | §1 / §2 |
| 影响分析 | §3 |
| 风险与缓解 | §4 |
| 成功标准 | §5 |
| WS-A 多租户 | §6 |
| WS-B Temporal 迁移 | §7 |
| WS-C 严格 Lease+Fencing | §8 |
| 架构考量 | §9 |
| 仍推迟项 | §10 |
| 交付清单 | §11 |
| 验收标准 | §12 |

实现文件映射（落地时）：

| 文件 | 涉及 WS |
|------|---------|
| `service/alembic/versions/004_tenant.sql` + `models.py` | WS-A（tenants/api_keys/quotas/audit_log/video_tasks.tenant_id） |
| `app/middleware/auth.py` + `main.py` | WS-A（X-API-Key → tenant） |
| `app/quota.py` + `routers/videos.py` | WS-A（配额/限速） |
| `app/audit.py` | WS-A（异步审计） |
| `app/workers/temporal_worker.py` + `scheduler.py` | WS-B（TemporalScheduler 实现） |
| `docker-compose.temporal.yml` | WS-B（temporal-server，仅 temporal 路径） |
| `service/alembic/versions/005_lease_token.sql` + `tasks.py` + `storage/s3.py` | WS-C（lease_token 自增 + fencing 写） |
| `tests/service/ws_a_*.py` `ws_b_*.py` `ws_c_*.py` | 各 WS |
| `service/pyproject.toml` | WS-A（slowapi）/ WS-B（temporalio） |

## Scope

### In Scope
- 多租户：`tenant_id` 隔离、API Key 鉴权、配额（并发+日限）、审计日志、按租户限速。
- Temporal：`TemporalScheduler` 真实实现，`OH_SCHEDULER_BACKEND=temporal` 可用；Celery 默认不变。
- 严格 Lease：`lease_token` 全链路 fencing（DB 终态 + S3 产物），升级 R8 为非 lease → 严格 lease。

### Out of Scope
- 多租户计费/账务、租户级 Grafana 看板、SSO/OAuth 登录（本期仅 API Key）。
- Temporal 复杂工作流编排、Temporal 集群 HA。
- 基于 etcd 等更高可用租约服务（本期仍 Redis）。
- 任务优先级抢占式调度、scheduler 多后端热切换。

## Impact Analysis

| Component | Change Required | Details |
|-----------|-----------------|---------|
| DB/Migration | Yes | WS-A（租户表）+ WS-C（lease_token 列） |
| 鉴权/中间件 | Yes | WS-A API Key 中间件 |
| Worker | Yes | WS-A tenant 透传；WS-B Temporal Activity；WS-C token 携带/校验 |
| API | Yes | WS-A 租户路由/限速/403 |
| 存储 | Yes | WS-C S3 fencing 写 |
| 部署 | Yes（仅 WS-B） | temporal-server 容器 |
| 依赖 | Yes | WS-A slowapi；WS-B temporalio |
| 测试 | Yes | ws_a/b/c 独立用例 |

## Architecture Considerations

- WS-A 的 `tenant_id` 经 `request.state` → 依赖注入到 DB session / worker context；既有 seam 在调用处补 `tenant_id`，不重写核心逻辑。
- WS-B 复用 Phase 2 的 `Scheduler` 抽象，仅补真正实现，符合「不锁死后端」。
- WS-C 复用 Phase 2 的 `worker_id`/`heartbeat_at`/`attempt` 列，新增 `lease_token`，向前兼容（历史任务 `lease_token=0` 视为无 fencing，新 claim 从 1 起）。
- 三者保持 `result_backend=Redis`、复用 Redis abort key（WS-B 内取消信号）。
- **WS-B × WS-C 后端耦合（已拍板）**：WS-C 的 lease fence 依赖 `recover_lost_tasks` 来 bump `lease_token`，而 reclaim 与心跳存活注册耦合在 Celery（`beat.py`）。`OH_SCHEDULER_BACKEND=temporal` 下若不跑 Celery 渲染 worker，R20 的严格保证不成立。**已决策**：将 reclaim/watch-dog 抽象为与调度后端无关的独立组件，初版由 Celery 调用（行为不变），再让 Temporal 复用同一套逻辑，不固化「Strict Lease 仅支持 Celery」（详 Phase 3 计划 §8.2）。

## Success Criteria

- [ ] **WS-A**：跨租户 GET/DELETE → 403/404；无效 API Key → 401；超配额 → 429；每次变更写审计；按租户限速生效。
- [ ] **WS-B**：`OH_SCHEDULER_BACKEND=temporal` 启动后提交/取消/重试经 Temporal worker；Celery 默认路径回归全绿。
- [ ] **WS-C**：旧 token 写终态/写 S3 产物 → 全部被 fence 拒绝；Redis 抖动下不再产生有效双产物。
- [ ] `pytest tests/service` 全绿（含 Phase 1/2 回归）；新增 `ws_a/b/c_*` 用例全绿。

## Risks & Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| 多租户 scope creep（tenant_id 过滤遗漏致越权） | High | High | 统一 DB 访问层/依赖注入 tenant 上下文；RLS 或强制 WHERE；越权用例覆盖 |
| API Key 泄露 | Med | High | 密钥哈希存储、可吊销、短时效；审计记录 |
| Temporal 引入运维负担 | Med | Med | temporal-server 仅 temporal 路径；Celery 默认不变；独立 compose |
| Lease fencing 改 S3 写路径复杂（无原生 CAS） | Med | Med | 中间映射表 + token 比对，旧 owner 产物丢弃（无副作用） |
| lease 续约丢失致任务卡死 | Low | Med | 续约失败计数 + 超时回退 reclaim；监控堆积 |
| 三工作流并发改动互相干扰 | Med | Med | 独立 migration 版本号、独立 Phase、独立测试目录 |

> **Redis 高可用边界**（同 Phase 2）：Redis 哨兵/集群不在本 change 范围；WS-C 租约仍以 Redis 为辅助，主可靠性来自 PG `lease_token` 自增（不依赖 Redis 即可 fence）。
