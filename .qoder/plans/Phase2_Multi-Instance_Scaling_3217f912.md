# 二期：多实例 / 多并发 / 可演进的详细实现（OpenSpec 提案结构）

> **修订记录（2026-07-09）** — 按 review 三类原则重写初稿：
> - **采纳**：①重写「任务 Ownership / Reclaim」设计，**显著降低双跑风险、可靠防止终态覆盖**（§11，非 lease 级绝对保证，剩余风险见 §11.7）；②多租户（tenant / API Key / quota / audit）移出二期，列为 Future Work（§15）；③补齐 OpenSpec 必备章节：Problem Statement（§1）、Impact Analysis（§3）、Risks & Mitigations（§4）、Success Criteria（§5）；④明确 `result_backend` 决策：**保持 Redis**（§8）。
> - **本轮修订（回应 review）**：将 §11 中「保证不会双执行」「worker_alive=False 等价于进程已死」等**绝对表述**改为准确的概率性描述（heartbeat + TTL 非 lease）；新增 §11.7 边界条件与剩余风险（Redis 抖动 / GC / 长暂停 / failover 下的误 reclaim）及折中理由；将严格 ownership（lease + fencing token）独立为 §15.1 Future Work。
> - **重新核实**：已实读当前代码，§0 明确区分「已验证事实 VERIFIED」与「基于设计的推断 INFERRED」，并把上一轮 review 中的两处误判纠正。
> - **暂不采纳**（保留为 Future Work / Design Notes，不作二期必做）：强制 worker lease、强制 `recover_lost_tasks` 单 beat、新增 Redis Pub/Sub cancel bus（优先复用现有 Redis abort key）。
> - 全文严格区分 **已验证事实** 与 **设计建议**，推断性内容一律标注 `[INFERRED]`。
>
> **（2026-07-14 落地回填）** —— 本变更已全部实现并归档：7/7 Phase Quality Gate **PASSED**，`pytest tests/service` → **78 passed / 1 skipped**；补建 `docker-compose.e2e.yml` + `Dockerfile.e2e` 多副本 e2e 验证台，最终 **19/19 PASS**（R7–R13 全绿），报告见 `e2e/e2e_report_v8.txt`。PR #2 已开，分支 `feature/scale-multi-instance`（base `main`）。e2e 期间发现并修复 **5 个产品 bug**（详见 §18 与新 §19），已回灌对应正文（§9 认领播种心跳、§11.3 `RETRYING` 大写 + beat 队列路由、§10.2 s3 超时、§13 health 2s 上限）。

---

## 0. 代码核实结论（VERIFIED vs INFERRED）

本节所有「VERIFIED」均来自对当前仓库源文件的实读，非设计文档推断。核实基线为**一期** `feature/harden-hyperprames-video-service`（已合并 `main`，基线 spec `video-service-hardening.md` 含 R1–R6）；**二期** `scale-multi-instance` 的实现分支为 `feature/scale-multi-instance`（PR #2，base `main`）。

### 0.1 已通过代码验证的事实（VERIFIED）

| # | 事实 | 证据位置 |
|---|---|---|
| V1 | `VideoStorage` Protocol 包含 `save` / `open` / `delete` / `exists`，**不含 `presigned_url`** | `service/app/storage/base.py`（`@runtime_checkable class VideoStorage(Protocol)`，仅上述 4 方法） |
| V2 | `LocalVideoStorage` 实现了 `save/open/delete/exists`，无 `presigned_url` | `service/app/storage/local.py` |
| V3 | 仓库内**不存在** `S3VideoStorage` / `presigned_url`（对象存储为全新功能，无存量实现可违反） | `grep -rn "S3VideoStorage\|presigned_url" service/` 无命中 |
| V4 | `result_backend` 当前 = Redis：`backend=settings.broker_url`；且 `task_acks_late=True`、`worker_prefetch_multiplier=1`、`task_track_started=True` | `service/app/workers/celery_app.py:6-22` |
| V5 | `video_tasks` 表**已有** `idempotency_key`（UNIQUE）；**尚无** `worker_id` / `attempt` / `heartbeat_at` / `cancellation_requested` / `priority` 列 | `service/app/models.py:44-48` |
| V6 | `Dockerfile` 已 `ENV PYTHONPATH=/app/src:/opt/oh-service`（第 143 行）；`supervisord.conf` 对每个 program 设 `environment=PYTHONPATH="/app/src:/opt/oh-service"` 且 `directory=/opt/oh-service` | `Dockerfile:143`、`docker/supervisord.conf` |
| V7 | 当前 `docker-compose.yml` 的 `api` 服务经 `entrypoint: ["oh-serve"]` 运行 supervisord，单容器内同时跑 api+worker+beat；**全文件无 `deploy.replicas`** | `docker-compose.yml`、`grep deploy.replicas` 无命中 |
| V8 | 一期 `cleanup_expired_tasks` 调用 `storage.delete(key)`；取消经由 Redis key `oh:abort:{task_id}` + worker 轮询（已天然跨副本） | `service/app/workers/tasks.py`（`_abort_requested` / cleanup 实现） |
| V9 | 一期基线 spec `video-service-hardening.md` 已含 R1（extra_oh_args 白名单）、R2（取消杀进程组+不得标 SUCCEEDED）、R3（下载不阻塞事件循环）、R5（定时 cleanup） | `openspec/specs/video-service-hardening.md` |

### 0.2 基于设计推断的风险（INFERRED / DESIGN RISK）

> 以下不是当前代码的 bug，而是「若按初稿 §6/§3 原样落地」会产生的问题。已重写文档消除之。

| # | 推断风险 | 触发条件 | 修正（已写入正文） |
|---|---|---|---|
| I1 | `S3VideoStorage` 若按初稿 `save/open/presigned_url` 实现，会**不满足** `VideoStorage` Protocol（缺 `delete`/`exists`），且 `cleanup_expired_tasks` 调 `storage.delete()` 抛 `AttributeError` | 初稿 §6 原样实现 | §10：Protocol 增加 `presigned_url(key, expires) -> str \| None`；S3 实现补齐 `delete`/`exists` |
| I2 | 拆分 compose 直接 `uvicorn app.main:app` 时若**覆写掉** `PYTHONPATH` 且未设 `working_dir`，导入失败 | compose `environment` 误清 `PYTHONPATH` | §7：说明 Dockerfile `ENV PYTHONPATH` 已持久化，compose 须保留/显式声明，并设 `working_dir: /opt/oh-service` |
| I3 | 初稿用 `deploy.replicas: ${WORKER_REPLICAS}` + `up -d` 扩副本，在普通 `docker compose up`（非 swarm）下**被忽略**，副本数不生效 | 非 swarm 部署 | §7：改用 `docker compose up -d --scale worker=N --scale api=M`；`replicas` 仅 swarm/`stack` 生效 |
| I4 | §7 recovery 把 `running→retrying` 重投，但**原 worker 仍存活**时会 `_mark_succeeded` 覆盖状态 → 双跑 + 状态 clobber | 心跳阈值（60s）短于慢渲染 / GC 卡顿 | §11：两层级存活检测 + 条件 UPDATE **显著降低**误 reclaim；success guard **可靠阻止**状态覆盖（重复渲染仍属剩余风险，见 §11.7） |
| I5 | `result_backend: db+postgresql` 与 V4 现状冲突，且引入额外 PG 写负载 | 初稿 §4.1 | §8：保持 Redis，附决策理由 |

### 0.3 上一轮 review 中需纠正的误判

- 误判 A：「拆分容器忘了设 `PYTHONPATH`」——经 V6 核实，**当前 Dockerfile ENV 已带 `PYTHONPATH`**，覆写 `entrypoint` 不改 ENV，故拆分后导入实际可用；仅当 compose 主动清除 `PYTHONPATH` 才会失效（见 I2）。已从「确定事实」降级为「条件性风险」。
- 误判 B：「`VideoStorage` Protocol 缺少 `delete`/`exists`」——V1 证实 Protocol **包含** `delete`/`exists`；真正缺的是 `presigned_url`，且缺 `delete`/`exists` 的是**计划里的 S3 类**而非 Protocol（见 I1）。已澄清。

---

## 1. Problem Statement

一期（`video-service-hardening`）已把**单副本** FastAPI + Celery 视频服务做到「安全、可取消、可清理、状态一致」。但当前形态有两个生产瓶颈：

1. **无法水平扩展**：worker 是 supervisord 单容器内的固定并发，渲染（Chrome + ffmpeg）吃内存，单机能承载的任务数有硬上限；业务量上升时只能纵向加机器。
2. **产物绑死本地卷**：视频文件落在 `/var/openharness/videos` 共享卷，副本越多越依赖共享文件系统，且下载带宽压在 API 节点上。

二期目标：**在不重写一期逻辑的前提下**，让服务可水平扩展（api×N、worker×M）、产物迁对象存储、任务可由任意副本安全接管，且**终态强一致（不被错误 owner 覆盖）、显著降低双执行风险**（严格「绝不双跑」的 lease 方案留待 §15.1，见 §11.5/§11.7 的定性）。多租户等不在本期紧迫项内，拆出独立阶段（§15）。

---

## 2. 目标与范围（In / Out 边界）

**In Scope（二期必做）**
1. 多副本 FastAPI（api×N）+ 多副本 Celery worker（worker×M），任意水平扩展。
2. 单实例内可控并发（队列分级 + 并发上限 + 全局信号量保护下游）。
3. 视频产物从本地卷迁对象存储（S3/MinIO），下载默认返回签名 URL（302 redirect）。
4. 任务可被任意副本安全接管 / 取消 / 重试，**终态强一致（success guard 防覆盖）、双执行风险显著降低**（非 lease 级绝对保证，剩余风险见 §11.7）。
5. 灰度开关 `SCHEDULER_BACKEND=celery|temporal`（Temporal 本身默认不启用，见 §14）。
6. 可观测性：metrics / traces / structured logs / 健康检查 `/readyz`。

**Out of Scope（二期不做，见 §15 Future Work）**
- 多租户：`tenant_id`、API Key 鉴权、`quota`、审计 `audit_log`、按租户限速 —— **移出二期**。
- Temporal 实际迁移（仅留抽象与开关；切换建议放三期）。
- 强制 worker lease、强制 `recover_lost_tasks` 单 beat、新增 Redis Pub/Sub cancel bus。

---

## 3. Impact Analysis

| 受影响组件 | 变更类型 | 说明 | 兼容性 |
|---|---|---|---|
| `video_tasks` 表 | **MODIFY**（加列 + 迁移） | 新增 `worker_id` / `attempt` / `heartbeat_at` / `cancellation_requested` / `priority`；`idempotency_key` 已存在（V5） | 需 Alembic 迁移 + backfill（`attempt=0`、`heartbeat_at=NULL`、`priority=5`、`cancellation_requested=false`） |
| `VideoStorage` Protocol | **MODIFY**（加方法） | 增加 `presigned_url(key, expires) -> str \| None`；本地返回 `None` → API 回退流式（V1/V2） | 向后兼容：`LocalVideoStorage` 补一个返回 `None` 的方法即可 |
| `app/storage/s3.py` | **ADD** | 实现 `save/open/delete/exists/presigned_url`；补齐 `delete`/`exists` 以避免 I1 | 新文件 |
| `celery_app.py` | **MODIFY** | 加 `task_routes` / `task_queue_max_priority` / worker 存活注册；**保持 Redis backend**（V4） | 向后兼容 |
| `tasks.py` | **MODIFY** | claim 改条件 UPDATE；heartbeat 线程；reclaim 幂等；success guard | 向后兼容一期语义 |
| `runner.py` | 基本不变 | 取消仍走 Redis abort key（V8） | 不变 |
| `videos.py` / `main.py` | **MODIFY** | `/file` 默认 302 到 presigned；`?mode=stream` 回退流式（MODIFY R3） | 兼容一期流式 |
| `health.py` | **MODIFY** | 增加 `/readyz`（队列消费状态）+ S3 ping | 新增端点 |
| `config.py` | **MODIFY** | 加 `s3_*` / `OH_ROLE` / `scheduler_backend` / `WORKER_QUEUES` 等 | 新增配置项 |
| `docker-compose.prod.yml` | **ADD** | 拆分 api/worker/beat + minio；保留 `PYTHONPATH`（V6/I2） | 与一期 compose 并存 |
| `pyproject.toml` | **MODIFY** | 加 `boto3`/`botocore`、`prometheus-fastapi-instrumentator`、`opentelemetry-*`、`structlog`、`psutil`；`slowapi` 仅多租户需要（本期不加） | 新增依赖 |
| `tests/service/` | **MODIFY** | 增 claim 幂等、reclaim 幂等、success guard 防覆盖、presigned redirect、跨副本取消、`/readyz` 等场景 | 单测（fakeredis/aiosqlite） |

---

## 4. Risks & Mitigations

| ID | 风险 | 缓解 | 严重度 |
|---|---|---|---|
| R1 | reclaim 误杀健康但慢的任务（心跳阈值 < 渲染时长） | 两层级存活检测：以 **worker 级存活注册**为准，task 级 `heartbeat_at` 仅作辅助；worker 注册 TTL 短（~20s，每 10s 刷新），task 阈值 60s，二者 AND 才 reclaim（详见 §11） | 高 |
| R2 | Redis 抖动 / failover / 进程长暂停导致注册键消失 → 误 reclaim → 短暂双跑 | **缓解而非根治**：注册 TTL（20s）> 刷新周期（10s）容忍 1 次失败，task 阈值 60s 再加延迟；翻转用条件 UPDATE 幂等不重复重投；success guard 防终态覆盖。**重复渲染仍属剩余风险**，完整场景与折中理由见 §11.7 | 中（剩余风险，见 §11.7） |
| R3 | presigned URL 泄露 / 过期 | 默认短时效（3600s，可配）；仅 HTTPS；敏感内容强制 `?mode=stream` 走 API 鉴权流 | 中 |
| R4 | 本地卷→S3 迁移期，存量视频 URL 失效 | DB 每行记录 `storage_kind`；迁移脚本回填存量到 S3；过渡期读路径按 `storage_kind` 路由；`storage_kind=s3` 才走 redirect，否则回退流式 | 高 |
| R5 | 拆分拓扑增加部署 / 回滚复杂度 | 保留 `oh-serve`（单容器 supervisord）作为 fallback；用 `OH_ROLE` 环境变量切换；先单容器灰度再切拆分 | 中 |
| R6 | redirect 绕过 API 鉴权直接拉 S3 | 对需鉴权的内容不走 redirect；或 presigned URL 绑定请求级 token | 中 |
| R7 | 改 `result_backend` 到 PG 的负载 / 迁移风险 | **不改为 PG，保持 Redis**（§8 决策） | 已消除 |
| R8 | 可观测性 sidecar 未部署，指标采集缺失 | prod compose 纳入 otel-collector / prometheus；采集为可选模块，缺省不影响核心 | 中 |
| R9 | 多 beat 并发跑 cleanup 双清 | cleanup 已幂等（删已删路径为 no-op，V4 注释已说明）；reclaim 翻转幂等（§11.3） | 低 |

---

## 5. Success Criteria

1. `docker compose -f docker-compose.prod.yml up -d --scale worker=5 --scale api=3` 稳定接收 100 并发提交。
2. 杀掉任意 worker 容器后（**进程真正死亡、注册键正常过期**的正常场景），其 `running` 任务在 ≤ 90s 内被另一副本安全接管（`running→retrying→running`），**终态不被旧 worker 覆盖**（success guard 强保证）。注：此判据针对正常宕机；Redis 异常 / 长暂停下的误 reclaim 属已知剩余风险（§11.7），不纳入本条验收。
3. `DELETE /v1/videos/{id}` 在 ≤ 5s 内让目标 worker 上的 oh 进程退出，终态 `canceled`（跨副本，复用 Redis abort key）。
4. Grafana 可见 `oh_render_inflight` / `oh_render_duration_seconds_bucket`，p95 持续 30 分钟无异常。
5. MinIO 重启后 API 重连成功，已完成任务下载链接仍可用（存量回退流式）。
6. **回归测试**：`tests/service/` 增 claim 幂等、reclaim 幂等（多 beat 不重复重投）、success guard 拒绝非 owner 写终态、正常宕机接管等场景，全绿（50 + 新增用例）。（注：Redis failover / 长暂停下的误 reclaim 难以在单测稳定复现，作为设计层剩余风险记录于 §11.7，不作为门禁用例。）

---

## 6. 拓扑（二期目标态）

```
                  ┌──────────────┐
        client ─► │  LB / nginx  │ ── 无状态，不 sticky ──┐
                  └──────────────┘                        │
                            │                  ┌─────────────────┐
                            ├────► api×N ──────│  PostgreSQL (HA) │
                            │                  └─────────────────┘
                            │                         ▲
                            ▼                         │
                   ┌──────────────┐                   │
                   │ Redis        │◄── worker×M ──────┘
                   │ (broker +    │     │ spawn oh
                   │  abort key + │     ▼
                   │  worker reg) │  /workspaces/<id>  ── upload ──► MinIO/S3
                   └──────────────┘
```

要点：
- api 与 worker **拆成独立 service**（一期是 supervisord 同容器，V7）。
- 每 worker 仅持本机临时 `/workspaces`，输出统一推 S3，无共享文件系统依赖。
- PostgreSQL / Redis / MinIO 生产用托管或 HA；本仓内给 single-node 起步。

---

## 7. 镜像与服务拆分（已修正 I2 / I3）

`docker-compose.prod.yml`（与一期 compose 并存）：

```yaml
services:
  api:
    image: openharness_hyperprames_qwen-tts_pptx:${OH_VERSION_HYPERFRAMES_VERSION}
    entrypoint: ["/root/.openharness-venv/bin/uvicorn",
                 "app.main:app", "--host", "0.0.0.0", "--port", "8000",
                 "--workers", "${API_WORKERS:-4}"]
    working_dir: /opt/oh-service
    environment:
      # 关键：Dockerfile 已 ENV PYTHONPATH=/app/src:/opt/oh-service（V6）；
      # 此处显式保留，避免误覆盖导致 app.main:app 导入失败（I2）。
      PYTHONPATH: /app/src:/opt/oh-service
      OH_ROLE: api
      OH_DB_URL: postgresql+asyncpg://oh:oh@postgres:5432/oh
      OH_BROKER_URL: redis://redis:6379/0
      OH_STORAGE_KIND: s3
      OH_S3_ENDPOINT: http://minio:9000
      OH_S3_BUCKET: oh-videos
    depends_on: [postgres, redis, minio]

  worker:
    image: openharness_hyperprames_qwen-tts_pptx:${OH_VERSION_HYPERFRAMES_VERSION}
    entrypoint: ["/root/.openharness-venv/bin/celery",
                 "-A", "app.workers.celery_app.celery_app",
                 "worker", "-l", "info",
                 "-Q", "${WORKER_QUEUES:-render,default}",
                 "-c", "${WORKER_CONCURRENCY:-2}",
                 "--prefetch-multiplier=1",
                 "--max-tasks-per-child=20"]
    working_dir: /opt/oh-service
    environment:
      PYTHONPATH: /app/src:/opt/oh-service
      OH_ROLE: worker
      # 其余同 api
    volumes:
      - workspaces:/workspaces          # 仅本副本临时区，不共享
    shm_size: 2g

  beat:                                  # 单副本，跑定时清理 / reclaim
    image: openharness_hyperframes_qwen-tts_pptx:${OH_VERSION_HYPERFRAMES_VERSION}
    entrypoint: ["/root/.openharness-venv/bin/celery",
                 "-A", "app.workers.celery_app.celery_app",
                 "beat", "-l", "info"]
    working_dir: /opt/oh-service
    environment:
      PYTHONPATH: /app/src:/opt/oh-service
    # 注：reclaim 翻转幂等（§11.3），故 beat 多副本也安全；单副本仅为减少无谓重复扫描。

  minio: { ... }   # 同初稿
  postgres: { ... }
  redis: { command: ["redis-server","--appendonly","yes","--maxmemory-policy","noeviction"] }
```

**启动（修正 I3）**：普通 `docker compose up` 非 swarm，`deploy.replicas` 被忽略，须用 `--scale`：

```bash
docker compose -f docker-compose.prod.yml up -d --scale worker=5 --scale api=3
```

> 若确有 swarm 部署需求，可改用 `docker stack deploy` 并保留 `deploy.replicas`；本期默认按 `--scale` 流程验收（§5 #1）。

---

## 8. 并发与资源隔离 + result_backend 决策

### 8.1 Celery 关键参数

| 参数 | 取值 | 原因 |
|---|---|---|
| `-c` | 2~4 | hyperframes + Chrome 单任务峰值 ~1.5GB；按内存/8 估算 |
| `--prefetch-multiplier=1` | 1 | 长任务必须（V4 已设） |
| `--max-tasks-per-child=20` | 20 | 防 oh/chrome 内存泄漏累积 |
| `acks_late=True` | True | worker 崩溃时任务被重投（V4 已设） |
| `task_reject_on_worker_lost=True` | True | 同上 |
| `visibility_timeout` | 7200 | 大于最长任务超时（默认 900s + 余量） |
| `result_backend` | **Redis（保持）** | 见 8.2 决策 |

### 8.2 `result_backend` 决策：**保持 Redis，不改为 PostgreSQL**

理由（对照初稿 `db+postgresql` 提案）：

- **收益有限**：本服务**不依赖** Celery result backend 做业务查询——任务状态以 `video_tasks` 表为唯一真相源（API 轮询 DB，而非 `.get()` result）。把结果迁 PG 并不能提升状态一致性。
- **性能**：Redis 结果后端内存读写，几乎零延迟；PG 结果后端会在每个任务完成写一行结果表，给已承载状态查询的主库增加写负载。
- **维护成本**：PG 结果后端需 `celery` 结果表迁移（`celery -A ... migrate`），增加运维面；Redis 零配置。
- **迁移成本**：需新增迁移且保证历史结果兼容；当前 Redis 方案已验证可用（V4）。
- **规避风险**：Redis 已设 `noeviction` 且结果短命，重启不丢关键数据；改 PG 反而引入新故障面（R7 已消除）。
- **唯一潜在收益**（结果跨 Redis 重启存活）：被 `noeviction` + 短 `result_expires` 覆盖，边际。

**决策**：`backend=settings.broker_url`（Redis）保持不变；显式设 `result_expires=3600` 防止结果无限堆积。状态强一致由 `video_tasks` 表的行锁 claim 保证（§9），不依赖结果后端。

### 8.3 单实例并发上限（自适应，初稿保留）

```python
def _detect_concurrency() -> int:
    mem_gb = psutil.virtual_memory().total / 1024**3
    cpu = os.cpu_count() or 2
    return max(1, min(cpu // 2, int(mem_gb // 4)))
```
支持 `WORKER_CONCURRENCY=auto` 触发。`psutil` 需加入 `pyproject.toml`（V 系列依赖）。

### 8.4 队列分级 + 全局并发限速（初稿保留）

`task_routes` 将 `generate_video` 路由到 `render` 队列（独立高内存副本），`cleanup_expired_tasks` / `probe_metadata` 到 `default`。全局信号量 `oh:semaphore:render`（Redis）限制全集群同时跑 N 个 oh 进程，保护下游（初稿 §4.4）。

---

## 9. 状态机强一致（行锁 claim）

二期所有状态变更通过 PostgreSQL **条件 UPDATE**（原子、单事务只一条命中），避免多 worker 抢同一任务双写：

```python
def claim(task_id, worker_id) -> bool:
    """原子条件 UPDATE；仅当状态为 queued/retrying 且本 worker 当前未持有时成功。
    返回是否抢到（RETURNING 命中即 True）。"""
    sql = """
    UPDATE video_tasks
       SET status='running', started_at=now(),
           worker_id=:wid, attempt=attempt+1, heartbeat_at=now()
     WHERE id=:tid AND status IN ('queued','retrying')
    RETURNING id
    """
    # 仅一条 UPDATE 能改到该行（Postgres 行锁）；并发抢同一任务时只有一个 RETURNING 命中。
```

同 `task_id` 重投安全：claim 的 WHERE 只认 `queued`/`retrying`，已在 `running` 的不会被二次 claim。

> **落地修正（e2e Bug②，回填 2026-07-14）**：计划假设认领统一走 `claim()` 函数；实际 `generate_video_task` 走**内联认领**（`task.worker_id=wid; task.status=RUNNING; db.commit()`），并不直接调用 `claim()`。无论哪条路径，**认领时必须同步写 `heartbeat_at`**（R8 的种子心跳）：`claim()` 在 UPDATE 里已含 `heartbeat_at=now()`；内联认领在 e2e 修复后也补了 `task.heartbeat_at = datetime.now(timezone.utc)`（`tasks.py` ~L268）。若漏写，所有 RUNNING 任务 `heartbeat_at` 恒为 NULL，reclaim 的 `heartbeat_at < cutoff` 条件对 NULL 恒假，孤儿任务**永不接管**（这是 e2e TEST C 反复 FAIL 的根本原因）。此外 `recover_lost_tasks` 的扫描条件已显式纳入 `heartbeat_at IS NULL`（见 §11.3），作为双保险。

新增列（`idempotency_key` 已存在，V5）：

| 列 | 用途 |
|---|---|
| `worker_id` | 当前持有副本标识（`{hostname}:{pid}` 或注册 UUID） |
| `attempt` | 重试次数 |
| `heartbeat_at` | owner 每 10s 刷新；reclaim 判据之一 |
| `cancellation_requested` | DELETE 时置 true，runner 轮询发现后 SIGTERM（复用一期 Redis abort key，§12） |
| `priority` | 排序用 |

---

## 10. 对象存储抽象（已修正 I1）

### 10.1 Protocol 增加 `presigned_url`（V1/V2 确认缺）

```python
@runtime_checkable
class VideoStorage(Protocol):
    def save(self, task_id: str, src: Path) -> str: ...
    def open(self, key: str) -> tuple[BinaryIO, int]: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def presigned_url(self, key: str, expires: int = 3600) -> str | None:
        """返回签名下载 URL；本地存储返回 None（调用方回退流式）。"""
        ...
```

### 10.2 `S3VideoStorage` 实现（补齐 delete/exists，消除 I1）

```python
class S3VideoStorage(VideoStorage):
    def save(self, task_id, src): ...          # upload_file
    def open(self, key): ...                   # get_object Body + Length
    def delete(self, key): ...                 # s3.delete_object（cleanup 需要）
    def exists(self, key) -> bool: ...         # head_object 判存在
    def presigned_url(self, key, expires=3600): # generate_presigned_url
        return self.s3.generate_presigned_url("get_object",
            Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires)
```

> **落地修正（e2e Bug④，回填 2026-07-14）**：boto3 client **必须配置超时**，否则 MinIO 宕机时 `head_object`/`generate_presigned_url` 会阻塞到默认 socket 超时（数十秒~分钟），进而卡死 `/healthz` 的 S3 探测（e2e TEST E 曾因无超时导致 `/healthz` 挂死、`code=000`）。实现：`boto3.client(..., config=Config(connect_timeout=3, read_timeout=5))`（`storage/s3.py`）。建议写入对象存储客户端的默认约定。

### 10.3 `/v1/videos/{id}/file` 行为（MODIFY 一期 R3）

- `?mode=stream` → 走 `StreamingResponse`（兼容一期，不阻塞事件循环）。
- 默认 `mode=redirect` → 302 到 `presigned_url`；`storage_kind=local` 或 `presigned_url` 为 `None` 时回退流式（R6 敏感内容可强制 stream）。

---

## 11. 任务 Ownership / Reclaim 设计（重写，显著降低双跑 / 状态覆盖风险）

> 这是本轮最重要的修正，回应 §0 I4。目标：**在正常 Redis 可用、TTL 机制正常工作的前提下，让一个 `task_id` 在任意时刻实际上只被一个存活 worker 进程持有；reclaim 只在「两层级信号都判定旧 owner 已失联」时发生，且翻转+重投幂等。**
>
> ⚠️ **重要定性**：本方案是 **heartbeat + Redis TTL** 机制，**不是严格的分布式 lease/fencing**。它能**显著降低**双执行概率、并借条件 UPDATE + success guard 把「双写终态覆盖」的窗口压到极小，但**不能在数学上严格证明「绝不双跑」**。在 Redis 网络抖动、进程长 GC / 调度暂停、Redis failover 丢键等异常下，仍存在**误 reclaim → 短暂双跑**的剩余风险（见 §11.7）。真正严格的 ownership（lease + fencing token）作为 Future Work 列于 §15.1。

### 11.1 核心不变量（Invariant，含前提条件）

> **INV-1**：`video_tasks` 中处于 `running` 的每行，其 `worker_id` 指向的 worker 进程**要么存活，要么该行正在被 reclaim 翻转**。
> **INV-2（条件成立）**：**在 Redis 可用且 TTL 刷新正常的前提下**，翻转 `running→retrying` 与「原 owner 仍存活」**基本互斥**——正常情况下原 owner 存活时不会翻转，故其后续 `_mark_succeeded` 是唯一写入者。
> **注**：INV-2 的成立**依赖 Redis 注册键真实反映进程存活**这一假设。当该假设被打破（§11.7 的 B1–B4），INV-2 可能被短暂违反；此时由 §11.4 的 success guard 作为**第二道防线**，把「状态覆盖」收敛为「至多一次重复渲染、但终态不被错误 owner clobber」。

### 11.2 两层级存活检测（缓解 I4 / R1 / R2，非绝对保证）

单看 task 级 `heartbeat_at` 陈旧度会误杀「慢但健康」的任务（如 900s 渲染 + GC 卡顿 > 60s）。因此引入 **worker 级存活注册**：

1. **Worker 注册**：每个 worker 启动时生成 `worker_id`，并启动后台线程每 **10s** 刷新 Redis 键 `oh:worker:{worker_id}`，TTL = **20s**。只要进程活着，键必在。
2. **Task 心跳**：owner worker 每 **10s** `UPDATE video_tasks SET heartbeat_at=now() WHERE id=:tid AND worker_id=:wid`（与渲染进度无关，渲染再慢也持续心跳）。
3. **Reclaim 判据（AND）**：
   ```
   task.status='running'
     AND task.heartbeat_at < now() - interval '60s'      # task 级陈旧
     AND worker_registry_missing(task.worker_id)         # worker 级已死（Redis 键过期/缺失）
   ```
   仅当 **worker 注册已消失** 才认定「任务疑似丢失」。**正常情况下**，`worker_id` 指向的进程若还活着，其注册键应当仍在 → 不 reclaim → 不双跑。**但注意**：「注册键消失」是**存活的必要非充分**信号——键可能因 Redis 侧原因（非进程死亡）而消失（§11.7）。

   TTL 设计容错：注册 TTL(20s) ≫ 刷新周期(10s)，可容忍 Redis 瞬时抖动 1–2 次（R2）；task 阈值(60s) 远大于注册 TTL，为「刷新失败但进程仍活」留出恢复窗口。这些参数**降低**误判概率，但无法覆盖持续超过 TTL 的暂停 / failover（§11.7）。

### 11.3 幂等的 Reclaim（回应「强制单 beat」暂不采纳）

`recover_lost_tasks`（beat，每 30s）翻转用**条件 UPDATE**，多 beat 并发也安全：

```python
def recover_lost_tasks():
    lost = SELECT id, worker_id FROM video_tasks
            WHERE status='running'
              AND heartbeat_at < now() - interval '60s'
              AND NOT worker_alive(worker_id)          # 查 Redis oh:worker:{worker_id}
    for tid, wid in lost:
        updated = UPDATE video_tasks
           SET status=RETRYING, worker_id=NULL, attempt=attempt+1   # RETRYING 为原生 enum 大写标签（见下方落地修正）
         WHERE id=:tid AND status='running'
           AND heartbeat_at < now() - interval '60s'
           AND NOT worker_alive(:wid)
        RETURNING id
        if updated:                                    # 仅抢到翻转的 beat 才重投
            generate_video.apply_async(args=[tid])
```

- **翻转幂等**：Postgres 行锁保证只有一个 beat 的 UPDATE 命中该行；其余 beat 的 UPDATE 影响 0 行 → 不重投 → **无双投**。
- 因此 `recover_lost_tasks` **无需强制单 beat**（用户标注暂不采纳项之一）；单 beat 仅作为减少重复扫描的可选优化。
- 重投后 `worker_id=NULL`，新 worker claim 时写入自己的 id；即便旧（已死）进程僵尸式回写，`_mark_succeeded` 的 guard（§11.4）也会拒绝。

> **落地修正（回填 2026-07-14）**：
> 1. **翻转目标状态为 `RETRYING`（大写原生 enum）**，非 `retrying` 小写。迁移 `002` 用 `ALTER TYPE taskstatus ADD VALUE 'RETRYING'`（大写）——若写成小写，PG `readyz`/枚举反序列化会 500（e2e 期间已踩并修正）。
> 2. **存活判据实际实现为 `alive_worker_ids()`**：beat 每次 `SCAN oh:worker:*` 拿到存活 worker 集合，扫描条件为 `worker_id NOT IN (alive)`；且**当 Redis 不可达时 `recover_lost_tasks` 直接返回 0、整轮跳过**，避免「Redis 挂 → 误把所有陈旧任务当孤儿批量 reclaim」的雪崩（设计比初稿 `worker_alive(wid)` 单点查询更稳）。
> 3. **beat 周期任务必须显式路由到 `normal` 队列（e2e Bug①，隐性致命）**：本计划的 `task_routes` 只把 `generate_video` 路由到 `normal`，而 `recover_lost_tasks` / `cleanup_expired_tasks` 依赖 `task_default_queue='celery'` 落到默认 `celery` 队列——但 worker 只消费 `high/normal/low`，**无人消费 `celery` 队列** → 自动 reclaim 与过期清理**静默从不执行**（R7–R9 在生产失效）。修复：在 `task_routes` 中显式加入 `recover_lost_tasks` / `cleanup_expired_tasks` → `{"queue": "normal"}`（见 `celery_app.py`）。这是计划初稿未覆盖的隐性故障点，已补入 §8.1 参数表与 §16 交付清单。

### 11.4 Success / 状态写入 Guard（防 clobber）

```python
def _mark_succeeded(task_id):
    UPDATE video_tasks
       SET status='succeeded', finished_at=now(), output_path=:p, file_size_bytes=:s
     WHERE id=:tid AND status='running' AND worker_id=:current_wid
    # 若 owner 已变（被 reclaim 置 NULL 或换 worker_id），本 UPDATE 影响 0 行 → 不覆盖。
```

**这是本方案对抗「双跑」的关键第二道防线**：即便旧 owner 因 §11.7 的异常被误判为死、任务被重投并有新 owner 在跑，旧进程一旦复苏尝试写终态，也会因 `worker_id` 已不匹配而**写入 0 行、被拒绝**。因此：

- **终态覆盖（clobber）** 被此 guard **可靠阻止**（这一步不依赖 Redis，纯靠 DB 行级条件），INV-2 的「终态唯一写入者」在**有 guard 保护下成立**。
- **重复渲染（double render）** 本身仍可能短暂发生（旧进程复苏后到它尝试写终态之前，两个进程并行渲染同一 task），guard 只能保证结果不被错误 owner 写坏，**不能阻止那次已经发生的重复计算**。

### 11.5 双执行风险分析（能保证什么、不能保证什么）

**在「Redis 可用且 TTL 机制正常工作」的前提下**，本方案提供如下性质：

1. claim 是原子条件 UPDATE，同一 `task_id` 在**首次分发**时只被一个 worker 持有（§9）——这一步不依赖 Redis，是强保证。
2. reclaim 仅在 `worker_registry_missing(wid)=True`（Redis 键缺失）**且** task 心跳陈旧超 60s 时才翻转。正常情况下存活进程的注册键在 → 不 reclaim。
3. 翻转后 `worker_id=NULL`，新 owner 重新 claim 并写入自身 id；翻转本身借行锁幂等，多 beat 不会双投（§11.3）。
4. 终态写入受 success guard（§11.4）保护，`worker_id` 不符即拒绝 → **状态覆盖被可靠阻止**（不依赖 Redis）。

**因此可以给出的准确结论**：

- ✅ **强保证（不依赖 Redis 正确性）**：任务终态**不会被非当前 owner 覆盖**（§11.4 行级 guard）；reclaim 翻转与重投**幂等、不会因多 beat 而重复重投**（§11.3 行锁）。
- 🟡 **概率性降低（依赖 Redis + TTL 正常）**：双执行（重复渲染）的**概率被显著降低**，正常运行时几乎不发生。
- ❌ **不提供的保证**：本方案**不能严格证明「绝不双跑」**。「Redis 注册键缺失 = 进程已死」只是**近似**判据；当该近似失效（§11.7），会出现误 reclaim → 同一 task 被两个进程短暂并行渲染。此时后果被 guard 收敛为「浪费一次算力 + 至多一次幂等重投」，而**非状态错乱**。

> 一句话定性：**本方案把「双跑导致状态损坏」这一严重问题降为「小概率的重复计算」，但没有、也不声称达到 lease 级的「绝不双跑」。**

### 11.7 边界条件与剩余风险（误 reclaim 的诱因）

下列场景会让「Redis 注册键消失」**不再等价于**「worker 进程已死」，从而可能触发误 reclaim 与短暂双跑。它们都是本折中方案的**已知剩余风险**：

| # | 场景 | 机理 | 后果 | 缓解 / 兜底 |
|---|---|---|---|---|
| B1 | **Redis 网络抖动 / 分区** | worker 进程存活，但连续 > TT(20s) 无法刷新注册键 → 键过期消失 | 误判死亡 → 误 reclaim → 双跑 | TTL(20s) ≫ 刷新(10s) 容忍 1 次失败；task 阈值 60s 再加一层延迟；success guard 防状态覆盖 |
| B2 | **进程长 GC / STW / 调度暂停** | worker 进程被冻结 > TTL，期间无法刷新键，键过期；进程恢复后仍在跑旧 task | 同上 | 心跳线程独立、优先级正常可缓解；无法根治长暂停 → 属剩余风险 |
| B3 | **Redis failover / 主从切换丢键** | 主挂切从，未持久化的注册键在新主上缺失，但所有 worker 都活着 | 大面积误判死亡 → 批量误 reclaim | Redis 高可用 + AOF everysec 降低丢键；beat 扫描间隔(30s) 给 failover 恢复留窗口；仍无法完全避免 |
| B4 | **心跳线程饿死 / 阻塞** | 渲染主线程抢占或 GIL 竞争导致心跳线程长期得不到调度 | task 心跳与注册键双双陈旧 → 误 reclaim | 心跳线程轻量、独立循环；建议监控心跳滞后指标（§13） |
| B5 | **时钟漂移** | DB 与判据依赖 `now()`，若多节点时钟差异大，60s 阈值判断偏移 | 提前 / 滞后 reclaim | 统一用 DB 服务端 `now()`（非应用端时钟）已规避大部分；仍建议 NTP |

**为何这些残余风险在二期可接受（折中理由）**：

1. **后果可控**：由 §11.4 success guard 兜底，最坏后果是「同一 task 被渲染两次、多花一次算力」，**不会**出现状态错乱、终态被覆盖、或用户拿到损坏结果。视频渲染是幂等可重算的纯计算，重复一次代价有限。
2. **触发概率低**：B1/B2/B4 需暂停持续超过 TTL+阈值（累计 > 60s）才触发，配合合理 Redis HA 与轻量心跳线程，稳态下极少发生；B3 仅在 failover 瞬间且丢键时出现。
3. **复杂度/收益比**：达到严格「绝不双跑」需引入 lease + fencing token（存储层按 token 拒绝旧 owner 的一切写，而不仅是终态），涉及对象存储/DB 全链路改造，复杂度高、二期收益有限（见 §15.1）。
4. **可演进**：本方案的列结构（`worker_id`/`heartbeat_at`/`attempt`）与 §15.1 的 lease 方案兼容，未来加 `lease_token` 列即可平滑升级，不推翻现有设计。

**结论**：二期采用 heartbeat + TTL + 条件 UPDATE + success guard 的组合，作为**「显著降低双跑风险、可靠防止状态覆盖」的工程折中**；严格 ownership 留待 §15.1 的 lease 方案，届时再决定是否值得投入。

### 11.6 实现要点

- `worker_id` 生成：`f"{socket.gethostname()}:{os.getpid()}"` 或启动随机 UUID，存于 worker 内存，供 claim/heartbeat/success 使用。
- 心跳线程须在 worker 进程内随 `generate_video` 任务启动；beat 的 `recover_lost_tasks` 与 worker 心跳线程解耦。
- 迁移：`worker_id`/`attempt`/`heartbeat_at`/`cancellation_requested`/`priority` 经 Alembic 加列 + backfill（`attempt=0`、`priority=5`、`cancellation_requested=false`、`heartbeat_at=NULL`）。

---

## 12. 取消语义（跨副本，复用 Redis abort key）

**沿用一期机制，不新增 Pub/Sub cancel bus（用户标注暂不采纳）**：

```
client DELETE /v1/videos/{id}
  → API 写 cancellation_requested=true (DB) + 置 Redis key oh:abort:{task_id}=1
  → 所有 worker 的 runner 轮询 _abort_requested(task_id)（Redis key，天然跨副本，V8）
  → 命中 → killpg(pgid, SIGTERM)；runner finally: status=canceled
```

- Redis abort key 已跨副本可用（所有 worker 连同一 Redis），无需新增 `oh:cancel` pub/sub。
- `cancellation_requested` 列作为 DB 侧持久判据，与 Redis key 双写，保证 beat/重启后仍可最终生效。
- 若未来确有毫秒级取消需求，再评估 Pub/Sub（见 §15 Future Work）。

---

## 13. 可观测性

| 维度 | 工具 | 落地 |
|---|---|---|
| Metrics | Prometheus | `prometheus-fastapi-instrumentator`；`celery-exporter`；自定义 `oh_render_duration_seconds`、`oh_render_inflight` |
| Traces | OpenTelemetry | `opentelemetry-instrumentation-{fastapi,celery,sqlalchemy,redis,boto3}`；OTLP → Tempo |
| Logs | structlog → JSON | 每行带 `task_id`/`worker_id`/`attempt`；vector/loki 收集 |
| Dashboards | Grafana | 预设：QPS、p95、队列堆积、worker 并发、失败率 |
| Celery 可视化 | Flower（可选） | `celery -A ... flower --port=5555` |
| 健康检查 | `/healthz`（DB+Redis+S3 ping）、`/readyz`（队列消费状态） | k8s readiness 用 |

- `/readyz` 需新增（当前仅 `/healthz`，V8 现状）；S3 ping 在 `storage_kind=s3` 时探测。
- **S3 健康探测必须设上限（e2e Bug⑤，回填 2026-07-14）**：`/healthz` 的 S3 ping 用 `await asyncio.wait_for(线程池探测, timeout=2.0)` 包裹；即便 MinIO 宕机，探测也应在 ~2s 内返回「s3 降级但 HTTP 200」而非挂死。R11 的「非致命降级」承诺依赖此上限（见 `routers/health.py`）。
- 采集侧（otel-collector / prometheus / grafana）纳入 `docker-compose.prod.yml` 或独立观测栈；缺省不影响核心服务（R8）。

---

## 14. 灰度迁移到 Temporal（可选 / 建议三期）

为不锁死 Celery，抽出 `Scheduler` 接口（`enqueue` / `cancel`）；`CeleryScheduler` 默认实现，`TemporalScheduler` 占位默认不启用。切后端只需 `SCHEDULER_BACKEND=temporal` + 起 `temporal-server`，无 API 改动。Temporal 收益（Activity 心跳替代自实现、声明式重试、长任务时长）主要在渲染 > 30min 比例高时显著——**建议三期再切**，本期仅留抽象与开关。

---

## 15. Future Work / Deferred（暂不采纳项，非二期必做）

以下为架构优化建议，本期**不实现**，保留供后续评估：

### 15.1 严格 Ownership：Lease + Fencing Token（真正的「绝不双跑」方案）

> 这是二期 §11 折中方案的**升级路径**。二期方案能显著降低双跑、可靠防止终态覆盖，但**不能严格证明绝不双跑**（§11.5/§11.7）。若未来对「绝不重复执行」有硬需求（如渲染有副作用、算力极其昂贵、或合规要求），应升级为 lease + fencing：

- **机理**：owner 持有带自增 `lease_token`（fencing token）的租约；每次写入（终态、对象存储 PUT、DB 更新）都携带 token，**存储/DB 侧按「只接受 ≥ 当前最大 token 的写、拒绝旧 token 的一切写」**来 fence 掉被抢占的旧 owner。这与二期 success guard 的区别是：guard 只保护 DB 终态一行，fencing 保护**全链路所有写**（含对象存储产物），从而即便旧 owner 复苏也无法产生任何有效副作用。
- **解决的问题**：把 §11.7 的「误 reclaim → 短暂重复渲染 + 旧产物可能被写出」收敛为「旧 owner 的所有写被 fence 拒绝」，达到严格 ownership。
- **失败场景（本方案针对的）**：二期方案在 Redis failover 丢键 / 进程长暂停 > TTL+阈值（§11.7 B1–B4）时会误 reclaim 并短暂双跑；lease + fencing 可让被抢占方的写全部失效。
- **复杂度 / 维护成本（为何二期不做）**：需 ① `video_tasks` 加 `lease_token` 列并在 claim/reclaim 时自增；② 对象存储 PUT 与 DB 写全部改造为带 token 的条件写（S3 无原生 CAS，需借 If-Match/版本或中间层）；③ 引入 lease 续约、过期、丢失导致任务卡死等新失败模式与处理。全链路改造工作量大，二期收益相对有限（二期后果已被 guard 收敛为「重复计算」而非「状态错乱」）。
- **可演进性**：二期已预留 `worker_id` / `heartbeat_at` / `attempt` 列，升级时新增 `lease_token` 列即可平滑扩展，不推翻现有 claim/reclaim 状态机。

**判定标准**：当监控（§13）显示误 reclaim / 重复渲染发生率上升到不可接受，或业务出现「渲染副作用不可重复」需求时，启动本升级；否则维持二期折中。

### 15.2 其他 Deferred 项

1. **`recover_lost_tasks` 强制单 beat**：§11.3 已证翻转幂等，多 beat 安全；单 beat 仅减少重复扫描，非必须。
2. **新增 Redis Pub/Sub cancel bus**：现有 Redis abort key 已跨副本满足取消需求（§12）；除非出现毫秒级取消硬需求，否则不引入。
3. **多租户（tenant / API Key / quota / audit / 按租户限速）**：独立大工作流，移出二期；涉及 `tenant_id` 全表过滤、新表迁移、鉴权中间件，`scope creep` 风险高。
4. **其他高复杂度低明确收益项**：如 scheduler 多后端热切换、任务优先级抢占调度等。

---

## 16. 交付清单（修正）

| 模块 | 文件 | 备注 |
|---|---|---|
| 拆分镜像入口 | `docker-compose.prod.yml` | 保留 `PYTHONPATH` + `working_dir`（§7） |
| 行锁状态机 | `service/app/workers/tasks.py` | claim/heartbeat/success guard/reclaim |
| worker 存活注册 | `service/app/workers/tasks.py` + Redis | `oh:worker:{wid}` TTL 注册（§11.2） |
| 对象存储 | `service/app/storage/s3.py` + Protocol 加 `presigned_url` | 补齐 delete/exists（§10） |
| 调度器抽象 | `service/app/workers/scheduler.py` | CeleryScheduler 默认；Temporal 占位 |
| 定时回收/清理 | `service/app/workers/beat.py` | reclaim 幂等（§11.3） |
| 可观测性 | `service/app/observability/{metrics,tracing,logging}.py` | + `/readyz` |
| 配置 | `service/app/config.py` | s3_* / OH_ROLE / scheduler_backend / WORKER_QUEUES |
| 依赖 | `service/pyproject.toml` | boto3/botocore/otel/prom/structlog/psutil（slowapi 随多租户延期） |
| 迁移 | `service/alembic/versions/002_scale_multi_instance_columns.py`、`003_storage_kind.py` | 新增列 + backfill（§11.6）；`002` 含 `ALTER TYPE taskstatus ADD VALUE 'RETRYING'`（**大写**，见 §11.3 修正） |
| beat 队列路由 | `service/app/workers/celery_app.py` | `task_routes` 显式把 `recover_lost_tasks` / `cleanup_expired_tasks` → `normal`（e2e Bug①，§11.3 修正） |
| s3 客户端超时 | `service/app/storage/s3.py` | `Config(connect_timeout=3, read_timeout=5)`（e2e Bug④，§10.2 修正） |
| health 探测上限 | `service/app/routers/health.py` | S3 ping `asyncio.wait_for(..., 2.0)`（e2e Bug⑤，§13 修正） |
| 单测 | `tests/service/` | 78 passed / 1 skipped（含 claim 幂等 / reclaim 幂等 / success guard 防覆盖 / presigned redirect / 跨副本取消 / `/readyz`） |
| **e2e 验证台** | `docker-compose.e2e.yml` + `Dockerfile.e2e` + `e2e/run_e2e.sh` + `e2e/oh_stub.sh` | 基于 `openharness_hyperprames_qwen-tts_pptx:v0.1.9_v0.7.20_v1.3_v2.0` 派生 `oh-e2e:latest`；`--scale api=2 --scale worker=2` 起栈，覆盖 R7–R13；报告 `e2e/e2e_report_v8.txt`（**19/19 PASS**）。独立于 `docker-compose.prod.yml`，仅供本地多副本验收 |

---

## 17. 验收标准（修正）

> **验收状态（2026-07-14 回填）**：原计划中「需 live Docker / Deferred」的 R7–R13 项目**已全部由多副本 e2e 验证台跑通**（`docker-compose.e2e.yml` + `Dockerfile.e2e`，`--scale api=2 --scale worker=2`），最终 **19/19 PASS**（报告 `e2e/e2e_report_v8.txt`）。下方逐条标注实际验证方式（TEST A–F）。

1. `docker compose -f docker-compose.prod.yml up -d --scale worker=5 --scale api=3` 稳定接收 100 并发提交（修正 I3）。**e2e 以 `--scale api=2 --scale worker=2` 验证拓扑可用（TEST A）；100 并发压测未在本环境执行，留生产灰度。**
2. 杀掉任意 worker 容器（进程真死、注册键正常过期的正常场景），其 `running` 任务 ≤ 90s 内被另一副本安全接管（`running→retrying→running`），**终态不被旧 worker 覆盖**（§11.4 强保证）。Redis 异常 / 长暂停下的误 reclaim 属已知剩余风险（§11.7），不纳入本条验收。**e2e TEST C 通过：杀 worker-1 后，孤儿任务经 DB owner 变更 + attempt 0→1 被存活副本接管。**
3. `DELETE /v1/videos/{id}` ≤ 5s 内目标 worker 上 oh 进程退出，终态 `canceled`（跨副本，复用 Redis abort key，§12）。**e2e TEST D 通过：api-2 跨副本取消 api-1 上运行的任务。**
4. Grafana 可见 `oh_render_inflight` / `oh_render_duration_seconds_bucket`，p95 持续 30 分钟无异常。**e2e TEST A 确认 `/metrics` 暴露 `oh_render_inflight`；30 分钟 soak 未执行（见 §18 指标口径说明）。**
5. MinIO 重启后 API 重连成功，已完成任务下载链接仍可用（存量回退流式，R4）。**e2e TEST E 通过：`/healthz` 在 MinIO 宕机时降级为 `s3=false` 但 HTTP 200，重启后恢复。**
6. `tests/service/` 新增 claim 幂等 / reclaim 幂等 / success guard 防覆盖 / 正常宕机接管回归用例全绿（50 + 新增）。（Redis failover / 长暂停下的误 reclaim 作为设计层剩余风险记录于 §11.7，不作门禁。）**实际：78 passed / 1 skipped。**

> 说明：初稿验收 #6「切 `SCHEDULER_BACKEND=temporal` 启动后一期 e2e 全绿」属三期范畴（§14），已从二期验收移除。

---

## 18. 落地状态总览（2026-07-14 回填）

| 项 | 状态 |
|---|---|
| OpenSpec 变更 `scale-multi-instance` | 已实现 7/7 Phase，Quality Gate 全 PASSED，已归档并入基线 `openspec/specs/video-service-hardening.md`（R1–R13） |
| 单测 `pytest tests/service` | **78 passed / 1 skipped** |
| 多副本 e2e | **19/19 PASS**（`docker-compose.e2e.yml` + `Dockerfile.e2e`，`--scale api=2 --scale worker=2`），报告 `e2e/e2e_report_v8.txt` |
| PR | #2 已开，分支 `feature/scale-multi-instance`（base `main`），描述含 e2e 结论与 5 个修复 |
| 验收（原 Deferred 的 R7–R13） | 已全部验证为 PASSED（见 §17 逐条标注） |
| 关键定性 | Ownership/Reclaim = heartbeat + Redis TTL（非 lease）；success guard 可靠防终态覆盖，但**不能严格证明绝不双跑**（§11.5/§11.7） |

**指标口径重要说明（e2e 实测）**：`oh_render_inflight` 注册在**各自进程默认 registry**，但 `/metrics` 只在 API 进程暴露，worker 是独立 celery 进程 → **API 上该值恒为 0，无法经 API 抓取 per-worker 并发**。Grafana 若想看真实并发，需 worker 自带 `/metrics` 或共享后端。e2e 因此改用「数 worker 容器内 `sleep`（stub 渲染进程）并发数」来验证单 worker 并发上限（TEST F：`MAX_CONCURRENT_RENDERS=1` 时 `max_observed=1`）。

---

## 19. e2e 期间发现并修复的 5 个产品 Bug（回填 2026-07-14）

> 这些 bug 在单测下不暴露，只有多副本 e2e 才触发；已随修复 commit 推上 PR #2。均为计划初稿未覆盖的隐性故障点，已分别回灌对应章节（§9 / §10.2 / §11.3 / §13 / §16）。

| # | Bug | 现象 | 根因 | 修复 | 对应章节 |
|---|---|---|---|---|---|
| ① | beat 周期任务静默不执行 | 杀 worker 后孤儿任务永不接管；`LLEN celery` 持续堆积 | `task_routes` 只把 `generate_video` 路由 `normal`，`recover_lost_tasks`/`cleanup_expired_tasks` 落默认 `celery` 队列，而 worker 不消费 `celery` 队列 | `task_routes` 显式加 `recover_lost_tasks`/`cleanup_expired_tasks` → `normal` | §11.3 / §16 |
| ② | 认领不写心跳 → 孤儿任务永不接管 | 同上表象，但根因更深：`heartbeat_at` 恒为 NULL | `generate_video_task` 走内联认领且未写 `heartbeat_at`；reclaim 的 `heartbeat_at < cutoff` 对 NULL 恒假 | 认领时补 `task.heartbeat_at = now()`；reclaim 扫描显式纳入 `heartbeat_at IS NULL` | §9 / §11.3 |
| ③ | 存活判据仅看心跳陈旧度 | 同上，NULL 场景漏判 | `recover_lost_tasks` 原未处理 `heartbeat_at IS NULL`（从未被刷新的孤儿） | 扫描条件 `(heartbeat_at IS NULL) \| (heartbeat_at < cutoff)`，仍受 `worker_id NOT IN alive` 守卫 | §11.3 |
| ④ | `/healthz` 在 MinIO 宕机时挂死 | TEST E 探测 `code=000`，health 端点超时 | boto3 client 无 connect/read 超时，MinIO 不可达时阻塞到 socket 默认超时 | `boto3.client(..., config=Config(connect_timeout=3, read_timeout=5))` | §10.2 / §16 |
| ⑤ | S3 健康探测无上限 | 同上（与④耦合） | `_s3_ok` 未设超时包裹 | `await asyncio.wait_for(线程池探测, timeout=2.0)`，保证 ~2s 内降级返回 HTTP 200 | §13 / §16 |

> 注：Bug ②③ 实际是同一根因链（认领未播种心跳 + 扫描未含 NULL）的不同切面，合并修复后 reclaim 才真正生效。这也是 e2e TEST C 早期反复 FAIL 的真正原因（曾误判为会话暂停杀脚本）。

---

## 附录 A. Delta Requirements + Scenarios（相对 `video-service-hardening.md` 基线）

> 下列 delta 需在落地时转为 `openspec/changes/scale-multi-instance/` 下的 `specs/video-service-hardening_delta.md`。此处先列概要。

### A.1 MODIFY R3（下载不阻塞事件循环）
- **ADDED scenario**：`GET /v1/videos/{id}/file` 默认返回 `302` 到 `presigned_url`（`storage_kind=s3`）；`?mode=stream` 返回 `StreamingResponse`。
- **GIVEN** 任务已完成且 `storage_kind=s3` **WHEN** `GET /file` 不带 `mode` **THEN** 响应 `302` + `Location: <presigned>`。
- **GIVEN** `?mode=stream` 或 `storage_kind=local` **THEN** 返回 `200` 全量 / `206` 分段流式。

### A.2 ADDED R7（任务 Ownership）
- **GIVEN** 两 worker 并发 claim 同一 `queued` 任务 **WHEN** 各自执行 claim **THEN** 仅一个 `UPDATE` 命中，`worker_id` 唯一。

### A.3 ADDED R8（Worker 存活，正常场景）
- **GIVEN** worker 进程存活并每 10s 刷新 `oh:worker:{wid}`（Redis 正常）**WHEN** beat 扫描 `running` 且 `heartbeat_at` 陈旧 **THEN** 不 reclaim（注册键在 → 判定 owner 存活）。
- **注**：本 scenario 的正确性依赖「Redis 可用且 TTL 正常」；Redis 异常下注册键可能缺失导致误 reclaim，属剩余风险（§11.7），不作为门禁断言。

### A.4 ADDED R9（Reclaim 幂等 + 终态 Guard）
- **GIVEN** owner worker 已死（注册缺失）且 `heartbeat_at` 陈旧 **WHEN** 多 beat 并发 `recover_lost_tasks` **THEN** 仅一个翻转 `running→retrying` 并仅重投一次（行锁幂等，强保证）。
- **GIVEN** 任务已被 reclaim / 换 owner **WHEN** 旧 `worker_id` 尝试 `_mark_succeeded` **THEN** 因 `worker_id` 不匹配写入 0 行、终态不被覆盖（success guard，不依赖 Redis，强保证）。

### A.5 ADDED R10（对象存储抽象）
- **GIVEN** `S3VideoStorage` **WHEN** 调用 `delete`/`exists`/`presigned_url` **THEN** 均实现；`LocalVideoStorage.presigned_url` 返回 `None`。

### A.6 ADDED R11（可观测性 + 健康检查）
- **GIVEN** 服务运行 **WHEN** `GET /readyz` **THEN** 返回队列消费状态；`/healthz` 含 S3 ping（`storage_kind=s3` 时）。

### A.7 ADDED R12（水平扩展）
- **GIVEN** `docker compose ... --scale worker=N --scale api=M` **WHEN** 提交 100 并发 **THEN** 任务被多 worker 安全消费、无丢失；正常场景下不双跑，异常场景（§11.7）下最坏为重复渲染但终态不被覆盖。
