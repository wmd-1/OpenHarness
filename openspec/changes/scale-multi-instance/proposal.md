# Proposal: Scale HyperFrames Video Service to Multi-Instance

**Change ID:** `scale-multi-instance`
**Created:** 2026-07-10
**Status:** Draft
**Baseline:** `.qoder/plans/FastAPI_Hyperframes_Video_Service_3217f912.md` (plan) + `openspec/specs/video-service-hardening.md` (一期基线)
**Design source:** `.qoder/plans/Phase2_Multi-Instance_Scaling_3217f912.md`

---

## Problem Statement

一期变更 `harden-hyperprames-video-service` 已把**单副本** FastAPI + Celery 视频服务做到「安全、可取消、可清理、状态一致」。但当前形态有两个生产瓶颈：

1. **无法水平扩展**：worker 是 supervisord 单容器内固定并发，渲染（Chrome + ffmpeg）吃内存，单机能承载的任务数有硬上限；业务量上升时只能纵向加机器。
2. **产物绑死本地卷**：视频文件落在 `/var/openharness/videos` 共享卷，副本越多越依赖共享文件系统，且下载带宽压在 API 节点上。

二期目标：在不重写一期逻辑的前提下，让服务可水平扩展（api×N、worker×M）、产物迁对象存储、任务可由任意副本安全接管，且**终态强一致（不被错误 owner 覆盖）、双执行风险显著降低**（heartbeat + TTL 机制，非 lease 级绝对保证，剩余风险见 §11.7 / delta R8 注记）。多租户等不在本期紧迫项内，拆出独立阶段（Future Work）。

## Proposed Solution

按四个支柱落地（详见 design source 与 tasks.md）：

1. **水平扩展拓扑**：拆分 api / worker / beat 为独立 service（`docker-compose.prod.yml`），经 `OH_ROLE` 切换；用 `--scale` 而非 swarm `replicas`（已修正初稿 I3）。保留单容器 `oh-serve` 作为 fallback。
2. **状态机强一致**：所有状态变更走 PostgreSQL 条件 UPDATE（行锁 claim），`recover_lost_tasks` 翻转幂等，success guard 防终态覆盖。
3. **Ownership / Reclaim（heartbeat 机制）**：worker 级 Redis 注册（TTL 20s，每 10s 刷新）+ task 级 `heartbeat_at`（60s 阈值）两层级存活检测；reclaim 仅当两信号都判失联时发生。**重要定性**：这是 heartbeat + TTL，不是严格 lease/fencing——显著降低双跑、可靠防止状态覆盖，但不严格证明绝不双跑（剩余风险 §11.7）。
4. **对象存储 + 可观测性**：`VideoStorage` Protocol 增加 `presigned_url`，新增 `S3VideoStorage`（补齐 delete/exists）；`/file` 默认 302 到 presigned、敏感内容 `?mode=stream` 回退流式。新增 metrics / traces / structured logs 与 `/readyz`。
5. **单实例并发控制**：worker 按 `priority` 路由分级队列（high/normal/low）+ 全局并发信号量 `MAX_CONCURRENT_RENDERS` 限制同时渲染的 `oh` 进程数，保护 Chrome/ffmpeg 内存，兑现 Scope 中「单实例内可控并发（队列分级 + 并发上限 + 全局信号量）」的承诺（落地见 tasks Phase 7）。

## Detailed Design

完整代码级设计见 **[`.qoder/plans/Phase2_Multi-Instance_Scaling_3217f912.md`](../../.qoder/plans/Phase2_Multi-Instance_Scaling_3217f912.md)**，关键章节映射：

| 主题 | Plan 章节 |
|------|-----------|
| 代码核实（VERIFIED vs INFERRED） | §0 |
| 问题 / 目标 / 范围 | §1 / §2 |
| 影响分析 | §3 |
| 风险与缓解 | §4 / §11.7 |
| 成功标准 | §5 |
| 目标拓扑 | §6 |
| 镜像与服务拆分 | §7 |
| 并发 / result_backend 决策 | §8 |
| 行锁 claim 状态机 | §9 |
| 对象存储抽象 | §10 |
| Ownership / Reclaim（含边界与剩余风险） | §11 |
| 取消语义（复用 abort key） | §12 |
| 可观测性 | §13 |
| Temporal 灰度（建议三期） | §14 |
| Future Work（含严格 lease + fencing） | §15 |

实现文件映射（落地时）：

| 文件 | 涉及项 |
|------|--------|
| `service/alembic/versions/*` (new) | 加列 `worker_id` / `attempt` / `heartbeat_at` / `cancellation_requested` / `priority` + backfill |
| `service/app/workers/tasks.py` | claim 条件 UPDATE；heartbeat 线程；reclaim 幂等；success guard；`cancellation_requested` |
| `service/app/workers/celery_app.py` | `task_routes` / `task_queue_max_priority` / worker 存活注册；保持 Redis backend |
| `service/app/workers/beat.py` (new) | `recover_lost_tasks` 幂等翻转 |
| `service/app/storage/base.py` | Protocol 增加 `presigned_url` |
| `service/app/storage/s3.py` (new) | `save/open/delete/exists/presigned_url` |
| `service/app/routers/videos.py` | `/file` 默认 302 redirect、`?mode=stream` 回退；`storage_kind` 路由 |
| `service/app/health.py` | 新增 `/readyz`（队列消费状态）+ S3 ping |
| `service/app/observability/{metrics,tracing,logging}.py` (new) | Prometheus / OTel / structlog |
| `service/app/workers/scheduler.py` (new) | `Scheduler` 接口；`CeleryScheduler` 默认；`TemporalScheduler` 占位 |
| `service/app/config.py` | `s3_*` / `OH_ROLE` / `scheduler_backend` / `WORKER_QUEUES` 等 |
| `docker-compose.prod.yml` (new) | 拆分 api/worker/beat + minio；保留 `PYTHONPATH` |
| `service/pyproject.toml` | `boto3`/`botocore`、`prometheus-fastapi-instrumentator`、`opentelemetry-*`、`structlog`、`psutil` |
| `tests/service/*` | claim 幂等 / reclaim 幂等 / success guard 防覆盖 / presigned redirect / 跨副本取消 / `/readyz` |

## Scope

### In Scope
- 多副本 FastAPI（api×N）+ 多副本 Celery worker（worker×M），任意水平扩展。
- 单实例内可控并发（队列分级 + 并发上限 + 全局信号量保护下游）。
- 视频产物从本地卷迁对象存储（S3/MinIO），下载默认返回签名 URL（302 redirect）。
- 任务可被任意副本安全接管 / 取消 / 重试，终态强一致（success guard 防覆盖）、双执行风险显著降低（非 lease 级绝对保证）。
- 灰度开关 `SCHEDULER_BACKEND=celery|temporal`（Temporal 默认不启用，建议三期）。
- 可观测性：metrics / traces / structured logs / 健康检查 `/readyz`。

### Out of Scope
- 多租户：`tenant_id`、API Key 鉴权、`quota`、审计 `audit_log`、按租户限速 —— 移出二期。
- Temporal 实际迁移（仅留抽象与开关；切换建议放三期）。
- 强制 worker lease、强制 `recover_lost_tasks` 单 beat、新增 Redis Pub/Sub cancel bus（复用现有 Redis abort key）。

## Impact Analysis

| Component | Change Required | Details |
|-----------|-----------------|---------|
| DB/Migration | Yes | 加列 + Alembic 迁移 + backfill |
| Storage | Yes | Protocol 增加 `presigned_url`；新增 `S3VideoStorage` |
| Worker | Yes | claim/heartbeat/reclaim/success guard；存活注册 |
| API | Yes | `/file` 302 redirect + `?mode=stream`；`/readyz` |
| Deploy | Yes | `docker-compose.prod.yml` 拆分；`OH_ROLE` |
| Observability | Yes | metrics/traces/logs；`/readyz` |
| Config | Yes | `s3_*` / `OH_ROLE` / `scheduler_backend` / `WORKER_QUEUES` |
| Dependencies | Yes | boto3/otel/prom/structlog/psutil |
| Tests | Yes | claim/reclaim/guard/presigned/cross-replica cancel/`/readyz` |

## Architecture Considerations

- 一期已把 worker 隔离在 `app/workers/tasks.py`、进程 spawn 在 `runner.run_oh`；二期 claim/reclaim/heartbeat 在这些 seam 内扩展，不重写一期逻辑。
- 状态强一致由 `video_tasks` 表的行锁条件 UPDATE 保证（§9），不依赖 Celery result backend（§8.2 决策：保持 Redis）。
- Ownership / Reclaim 是 **heartbeat + Redis TTL** 机制（§11），**非**严格 lease/fencing：能显著降低双跑、可靠防止终态覆盖（success guard，不依赖 Redis），但 Redis 异常 / 进程长暂停 / failover 下仍可能误 reclaim 致短暂双跑（§11.7，作为已知剩余风险，不纳入正常宕机验收）。
- 取消语义沿用一期 Redis `oh:abort:{task_id}` key（天然跨副本），不新增 Pub/Sub bus。
- `result_backend` 保持 Redis（不改为 PG），显式 `result_expires=3600`。

## Success Criteria

- [ ] `docker compose -f docker-compose.prod.yml up -d --scale worker=5 --scale api=3` 稳定接收 100 并发提交。
- [ ] 杀掉任意 worker 容器（进程真死、注册键正常过期的正常场景），其 `running` 任务 ≤ 90s 内被另一副本安全接管（`running→retrying→running`），终态不被旧 worker 覆盖（success guard 强保证）。注：Redis 异常/长暂停下的误 reclaim 属已知剩余风险（§11.7），不纳入本条验收。
- [ ] `DELETE /v1/videos/{id}` ≤ 5s 内让目标 worker 上 oh 进程退出，终态 `canceled`（跨副本，复用 Redis abort key）。
- [ ] Grafana 可见 `oh_render_inflight` / `oh_render_duration_seconds_bucket`，p95 持续 30 分钟无异常。
- [ ] MinIO 重启后 API 重连成功，已完成任务下载链接仍可用（存量回退流式）。
- [ ] 回归测试：`tests/service/` 增 claim 幂等、reclaim 幂等（多 beat 不重复重投）、success guard 拒绝非 owner 写终态、正常宕机接管等场景全绿（50 + 新增用例）。Redis failover/长暂停下误 reclaim 作为设计层剩余风险记录于 §11.7，不作门禁用例。

## Risks & Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| reclaim 误杀健康但慢的任务 | Med | High | 两层级存活检测：worker 级注册为准 + task 级 heartbeat_at 辅助；TTL 20s/刷新 10s/阈值 60s 且 AND（§11） |
| Redis 抖动 / failover / 长暂停致误 reclaim → 短暂双跑 | Med | Med | 缓解非根治：TTL 容 1 次失败；翻转幂等不重复重投；success guard 防终态覆盖；重复渲染为剩余风险（§11.7） |
| presigned URL 泄露 / 过期 | Med | Med | 默认短时效（3600s）；仅 HTTPS；敏感内容强制 `?mode=stream` |
| 本地卷→S3 迁移期存量 URL 失效 | High | High | DB 记 `storage_kind`；迁移脚本回填；按 kind 路由；`local` 回退流式 |
| 拆分拓扑增加部署 / 回滚复杂度 | Med | Med | 保留 `oh-serve` 单容器 fallback；`OH_ROLE` 切换；先单容器灰度 |
| redirect 绕过 API 鉴权直拉 S3 | Med | Med | 需鉴权内容不走 redirect；或 presigned 绑定请求级 token |
| 改 result_backend 到 PG 的负载/迁移风险 | — | — | 不改为 PG，保持 Redis（已消除） |
| 可观测性 sidecar 未部署致指标缺失 | Med | Med | prod compose 纳入 otel/prom；可选模块，缺省不影响核心 |
| 多 beat 并发 cleanup 双清 | Low | Low | cleanup 幂等；reclaim 翻转幂等（§11.3） |
| Redis 全不可用致存活检测失效 | Med | High | 已知边界：Redis HA（哨兵/集群）不在本 change 范围，依赖既有部署；Redis 全宕时 worker 注册/心跳/取消键均不可用，reclaim 与跨副本取消语义降级（§11.7 剩余风险延伸，不纳入正常验收） |

> **Redis 高可用边界**：Redis 哨兵 / 集群等高可用部署**不在本 change 范围**，依赖既有部署环境。Redis 完全不可用时，worker 存活注册、heartbeat、取消 key 全部失效，reclaim 与跨副本取消语义降级——此为 §11.7 剩余风险的延伸，不作为正常宕机验收门禁。
