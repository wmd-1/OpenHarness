# 二期：多实例 / 多并发 / 可演进的详细实现

## 1. 目标与范围

二期不重写一期代码，只做**横向加固**：

1. 多副本 FastAPI + 多副本 Celery worker，水平任意扩展。
2. 单实例内可控并发（避免 Chrome/ffmpeg 把内存吃爆）。
3. 视频产物从本地卷迁到对象存储（MinIO/S3），返回签名 URL。
4. 任务可被任意一个副本接管、可恢复、可取消、可重试，状态强一致。
5. 灰度开关：`SCHEDULER_BACKEND=celery|temporal`，未来切 Temporal 不影响 API 层。
6. 完整的可观测性（metrics / traces / logs / 任务事件）。

## 2. 拓扑（二期目标态）

```
                  ┌──────────────┐
        client ─► │  Traefik /   │ ── sticky? no, stateless ──┐
                  │  nginx (LB)  │                             │
                  └──────────────┘                             ▼
                            │                       ┌─────────────────┐
                            ├────────► api×N ◄──────│  PostgreSQL     │
                            │                       │  (HA: Patroni)  │
                            │                       └─────────────────┘
                            │                              ▲
                            │                              │
                            ▼                              │
                   ┌─────────────────┐                     │
                   │  Redis (broker) │◄─── worker×M ──────┘
                   │  + RDB/AOF      │       │
                   └─────────────────┘       │ spawn oh
                                             ▼
                                      /workspaces/<id>
                                             │ upload
                                             ▼
                                  ┌────────────────────┐
                                  │ MinIO / S3 (videos) │
                                  └────────────────────┘
```

要点：
- API 与 worker **拆成两个独立 service**（一期是 supervisord 同容器）。
- 每个 worker 副本仅持有本机临时 `/workspaces`，输出统一推 MinIO，无共享文件系统依赖。
- PostgreSQL/Redis 在生产用托管或 HA 部署，本仓内仅给 single-node 起步。

## 3. 镜像与服务拆分

`docker-compose.prod.yml`（与一期 compose 并存）：

```yaml
services:
  api:
    image: openharness:${OH_VERSION}
    entrypoint: ["/root/.openharness-venv/bin/uvicorn",
                 "app.main:app", "--host", "0.0.0.0", "--port", "8000",
                 "--workers", "${API_WORKERS:-4}"]
    deploy:
      replicas: ${API_REPLICAS:-3}
    environment:
      OH_ROLE: api
      OH_DB_URL: postgresql+asyncpg://oh:oh@postgres:5432/oh
      OH_BROKER_URL: redis://redis:6379/0
      OH_STORAGE_KIND: s3
      OH_S3_ENDPOINT: http://minio:9000
      OH_S3_BUCKET: oh-videos
    depends_on: [postgres, redis, minio]

  worker:
    image: openharness:${OH_VERSION}
    entrypoint: ["/root/.openharness-venv/bin/celery",
                 "-A", "app.workers.celery_app.celery_app",
                 "worker", "-l", "info",
                 "-Q", "${WORKER_QUEUES:-render,default}",
                 "-c", "${WORKER_CONCURRENCY:-2}",
                 "--prefetch-multiplier=1",
                 "--max-tasks-per-child=20"]
    deploy:
      replicas: ${WORKER_REPLICAS:-3}
      resources:
        limits: { cpus: "4", memory: "8g" }
    environment:
      OH_ROLE: worker
      # 同 api
    volumes:
      - workspaces:/workspaces           # 仅本副本临时区，不共享
    shm_size: 2g

  beat:                                  # 单副本，跑定时清理 / metrics 心跳
    image: openharness:${OH_VERSION}
    entrypoint: ["/root/.openharness-venv/bin/celery",
                 "-A", "app.workers.celery_app.celery_app",
                 "beat", "-l", "info"]
    deploy: { replicas: 1 }

  minio:
    image: minio/minio:latest
    command: ["server", "/data", "--console-address", ":9001"]
    environment:
      MINIO_ROOT_USER: oh
      MINIO_ROOT_PASSWORD: ohohohoh
    volumes: [minio:/data]
    ports: ["9000:9000", "9001:9001"]

  postgres: { ... }
  redis:    { command: ["redis-server", "--appendonly", "yes",
                        "--maxmemory-policy", "noeviction"] }
```

启动：

```bash
WORKER_REPLICAS=5 API_REPLICAS=3 docker compose -f docker-compose.prod.yml up -d
```

## 4. 并发与资源隔离

### 4.1 Celery 关键参数

| 参数 | 二期取值 | 原因 |
|---|---|---|
| `-c` (concurrency) | 2~4 | hyperframes + Chrome 单任务峰值 ~1.5GB；按内存/8 计算 |
| `--prefetch-multiplier=1` | 1 | 长任务必须；否则空闲 worker 抢不到任务 |
| `--max-tasks-per-child=20` | 20 | 防 oh / chrome 进程内存泄漏累积 |
| `acks_late=True` | True | 保证 worker 崩溃时任务被重投 |
| `task_reject_on_worker_lost=True` | True | 同上 |
| `broker_transport_options.visibility_timeout` | `7200` | 大于最长任务超时（默认 900s + 余量） |
| `result_backend` | `db+postgresql://...` | 状态持久化，便于多实例查询 |

### 4.2 单实例并发上限

`app/workers/celery_app.py` 增加自适应：

```python
def _detect_concurrency() -> int:
    mem_gb = psutil.virtual_memory().total / 1024**3
    cpu = os.cpu_count() or 2
    return max(1, min(cpu // 2, int(mem_gb // 4)))
```

支持 `WORKER_CONCURRENCY=auto` 触发该函数。

### 4.3 队列分级

```python
celery_app.conf.task_routes = {
    "generate_video": {"queue": "render"},      # 重任务，独立队列
    "cleanup_expired_tasks": {"queue": "default"},
    "probe_metadata": {"queue": "default"},
}
celery_app.conf.task_queue_max_priority = 10
celery_app.conf.task_default_priority = 5
```

部署时 render 队列单独副本（高内存机器），default 队列普通副本。POST 请求体可带 `priority: int`。

### 4.4 全局并发限速（保护下游）

Redis 信号量：

```python
GPU_LOCK = "oh:semaphore:render"
async def acquire(timeout=30):
    # SET key value NX PX 与 TTL；BRPOP from 队列；二选一
```

或使用 `celery.contrib.bottleneck` / `redis-semaphore`，限制全集群同时跑 N 个 oh 进程。

## 5. 状态机强一致

二期把状态变更全部**通过 PostgreSQL 行锁 + 条件更新**，避免多 worker 抢同一任务时双写：

```python
def _claim(task_id, worker_id) -> bool:
    """SELECT ... FOR UPDATE SKIP LOCKED 行级锁；返回是否抢到。"""
    sql = """
    UPDATE video_tasks
       SET status='running', started_at=now(),
           worker_id=:wid, attempt=attempt+1
     WHERE id=:tid AND status IN ('queued','retrying')
    RETURNING id
    """
```

只有 RETURNING 命中，才执行后续 oh 调用。同 task_id 重投也安全。

新增列：

| 列 | 用途 |
|---|---|
| `worker_id` | 哪个副本在跑（hostname + pid） |
| `attempt` | 重试次数 |
| `heartbeat_at` | worker 每 10s 更新；监控发现 stale → 标记 lost |
| `cancellation_requested` | DELETE 时置 true，runner 轮询发现后 SIGTERM |
| `priority` | 排序用 |
| `idempotency_key UNIQUE` | 防重提交 |

## 6. 对象存储抽象

`app/storage/s3.py`：

```python
class S3VideoStorage(VideoStorage):
    def __init__(self, settings):
        self.s3 = boto3.client("s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            config=botocore.config.Config(signature_version="s3v4"))
        self.bucket = settings.s3_bucket

    def save(self, task_id, src):
        key = f"videos/{task_id[:2]}/{task_id}.mp4"
        self.s3.upload_file(str(src), self.bucket, key,
            ExtraArgs={"ContentType": "video/mp4"})
        return key

    def open(self, key):
        obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"], obj["ContentLength"]

    def presigned_url(self, key, expires=3600):
        return self.s3.generate_presigned_url("get_object",
            Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires)
```

`/v1/videos/{id}/file` 行为分两档：
- `?mode=stream` 走 StreamingResponse（兼容一期）。
- 默认 `mode=redirect` → 302 到 presigned URL，**让客户端直接从 MinIO 下载**，API 节点不再传带宽。

## 7. 断点续跑与"任务丢失"恢复

新增定时任务 `recover_lost_tasks`（Celery beat，每 30s）：

```python
WHERE status='running' AND heartbeat_at < now() - interval '60 seconds'
→ status='retrying', re-enqueue
```

worker runner 每 10s 更新 `heartbeat_at`；任意副本崩溃后任务被 picked 起来重跑。

幂等保证：oh 任务在干净的 `/workspaces/<task_id>/<attempt>` 目录跑，输出文件名也带 attempt，最终上传到 S3 的 key 唯一，避免互相覆盖。

## 8. 取消语义（跨副本）

```
client DELETE /v1/videos/{id}
  → API 写 cancellation_requested=true (DB) + PUBLISH oh:cancel <task_id>
  → 所有 worker SUBSCRIBE oh:cancel；命中本机 task → killpg(pgid, SIGTERM)
  → runner finally: status=canceled, finished_at=now()
```

冗余兜底：runner 的日志循环里每 2s 查 `cancellation_requested`，慢但保证最终生效。

## 9. 可观测性

| 维度 | 工具 | 落地 |
|---|---|---|
| Metrics | Prometheus | FastAPI: `prometheus-fastapi-instrumentator`；Celery: `celery-exporter`；自定义 `oh_render_duration_seconds`、`oh_render_inflight` |
| Traces | OpenTelemetry | `opentelemetry-instrumentation-{fastapi,celery,sqlalchemy,redis,boto3}`；OTLP → Jaeger/Tempo |
| Logs | structlog → JSON | 每行带 `task_id`、`worker_id`、`attempt`；用 vector/loki 收集 |
| Dashboards | Grafana | 一份预设面板：QPS、p95、队列堆积、worker 并发、失败率 |
| Celery 可视化 | Flower | `celery -A ... flower --port=5555` |
| 健康检查 | `/healthz`（DB+Redis+S3 ping），`/readyz`（队列消费状态） | k8s readiness 用 |

## 10. 灰度迁移到 Temporal（可选，二期末或三期）

为不锁死 Celery，在 `app/workers/` 下抽出 `Scheduler` 接口：

```python
class Scheduler(Protocol):
    async def enqueue(self, task_id: UUID, **opts) -> str: ...
    async def cancel(self, task_id: UUID) -> None: ...

class CeleryScheduler(Scheduler): ...
class TemporalScheduler(Scheduler):
    """worker 端把 generate_video_task 包成 Temporal Workflow，
       activity = run_oh + upload_video，
       Workflow 提供天然重试 / 长时心跳 / 取消语义。"""
```

API 路由通过 `Depends(get_scheduler)` 注入；切后端只需 `SCHEDULER_BACKEND=temporal` + 起 `temporal-server` 服务，无 API 代码改动。Temporal 收益主要在：

- Activity 心跳替代自实现的 heartbeat_at。
- Workflow 重试策略声明式（`RetryPolicy(maximum_attempts=3, backoff=...)`）。
- 长任务可达数小时无需调 visibility timeout。
- 任务历史完整可重放调试。

如果二期上线后渲染任务超过 30 分钟比例较高，**强烈建议三期切换**。

## 11. 安全 / 多租户

- API 鉴权：`X-API-Key` header，租户表 `api_keys(key_hash, tenant_id, quota_per_day)`。
- 任务行加 `tenant_id`，所有查询带租户过滤；S3 key 前缀 `tenants/<tid>/videos/<id>.mp4`。
- 限流：`slowapi` 按 tenant_id + 路径限速。
- prompt 内容审计：所有 POST 写 `audit_log` 表。
- `extra_oh_args` 严格白名单（如仅允许 `--model`, `--max-turns`），杜绝命令注入。

## 12. 压测与容量基线

`scripts/loadtest_videos.py`（locust）：
- scenario A：100 并发提交、查询、下载，断言 p95 提交 < 200ms。
- scenario B：50 并发渲染（mock runner，sleep 60s + 写假 mp4），断言 worker 队列稳态、无任务丢失。
- 输出报告：`docs/perf/phase2-baseline.md`。

容量公式（参考）：

```
吞吐(任务/min) = WORKER_REPLICAS × WORKER_CONCURRENCY × (60 / 平均渲染时长秒)
内存峰值      = WORKER_REPLICAS × WORKER_CONCURRENCY × 1.5GB
```

## 13. 交付清单

| 模块 | 文件 |
|---|---|
| 拆分镜像入口 | `service/app/workers/celery_app.py` 配置增强、`docker-compose.prod.yml` |
| 行锁状态机 | `service/app/workers/tasks.py` 重写 claim/heartbeat |
| 对象存储 | `service/app/storage/s3.py` + settings |
| 取消通道 | `service/app/workers/cancel_bus.py` |
| 调度器抽象 | `service/app/workers/scheduler.py`（CeleryScheduler 默认实现） |
| 定时回收 | `service/app/workers/beat.py`（recover_lost / cleanup_expired） |
| 可观测性 | `service/app/observability/{metrics,tracing,logging}.py` |
| 多租户 | `service/app/auth/api_key.py` + Alembic migration |
| 压测 | `scripts/loadtest_videos.py`、`docs/perf/phase2-baseline.md` |
| Temporal 占位 | `service/app/workers/temporal_scheduler.py`（默认未启用） |

## 14. 验收标准

1. `docker compose -f docker-compose.prod.yml up --scale worker=5 --scale api=3` 能稳定接收 100 并发提交。
2. 杀掉任意一个 worker 容器，正在跑的任务在 ≤ 90s 内被另一个 worker 接管（status: running → retrying → running）。
3. DELETE 请求在 ≤ 5s 内导致目标 worker 上的 oh 进程退出，任务终态为 canceled。
4. Grafana 面板可看到 `oh_render_inflight`、`oh_render_duration_seconds_bucket`，p95 持续 30 分钟无报警。
5. MinIO 重启后 API 重连成功，已完成任务的下载链接仍可用。
6. 切换 `SCHEDULER_BACKEND=temporal` 启动后所有现有 API e2e 用例（一期 `tests/service/`）全绿。
