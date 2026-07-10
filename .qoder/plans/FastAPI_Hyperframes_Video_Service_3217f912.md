# FastAPI Hyperframes 视频生成服务方案

> **文档状态（2026-07-09 更新）**：本文档是视频服务的**初版设计构想**。同日的 `harden-hyperframes-video-service` 变更审查了初版实现，修复 14 处缺陷，并把"本计划隐含但初版违反的行为"固化为 OpenSpec 事实契约 **`openspec/specs/video-service-hardening.md`（R1–R6）**，由 `tests/service/` 测试套件强制保证。
>
> **事实来源优先级**：`openspec/specs/video-service-hardening.md` ＞ 本文档。本文档保留为"设计演进记录"，正文多处已被加固实现超越或修正，以「✅ 已实现修正」标注。完整加固设计见 `openspec/archive/harden-hyperprames-video-service/design.md`。
>
> **范围**：本文档只覆盖**一阶段**（单副本可用 + 安全/正确性加固）。水平扩展、对象存储迁移、崩溃恢复 ownership 属**二阶段**（`.qoder/plans/Phase2_Multi-Instance_Scaling_3217f912.md`）。

## 0. 一阶段已实现总览（相对初版的修正）

| # | 主题 | 计划初版 | 一阶段实际落地 |
|---|------|---------|---------------|
| #1 | `extra_oh_args` 白名单 | §4 仅说"受白名单约束" | 新增 `security.py` 显式 allowlist+blocklist，API 边 422 拒绝（R1） |
| #2 | RUNNING 取消 | §13 `revoke(terminate=True)`（杀不到 `oh` 子进程） | worker 自终止：Redis `oh:abort:<id>` + `setsid` 进程组 SIGTERM/KILL，取消后绝不写 SUCCEEDED（R2） |
| #3 | 下载阻塞事件循环 | §8 同步 `read` | `run_in_threadpool` 离线读（R3） |
| #4 | cleanup 无调度 | §13 仅定义任务 | `beat_schedule` + supervisord `[program:beat]`（R4） |
| #5 | 日志连接/per-line ltrim | — | 模块级连接池复用（R5）；并改用 Redis Stream |
| #6 | Alembic 混用 sync URL | — | 新增 `db_migration_url`（asyncpg） |
| #7 | CORS `*`+credentials | 计划未涉及 | 仅显式 origins 且 credentials 互斥（R6） |
| #8 | `Accept-Ranges` 虚标 | §8 标"可选" | 已实现 Range→206 |
| #9 | 幂等竞态 500 | — | 捕获 `IntegrityError` 回退已有任务 |
| #10 | 确定性失败仍 raise | §5 伪代码 raise | 不再重抛，避免无限重投 |
| #11 | 测试覆盖 | §14 仅 api/parser | 新增 worker/SSE/streaming/security/cleanup/api_edge，全绿 |
| #12 | 死依赖 ffmpeg-python | §10 | 已移除 |
| #13 | SSE 重放竞态 | §9 list+pubsub | 改用 Redis Stream（XADD/XREAD），无重复 |
| #14 | cleanup 留 DB 悬针 | — | 清理后置 `output_path/workspace_path=None` |

## 1. 总体架构

```
        +-----------------+         +------------------+
HTTP -> | FastAPI (8000)  | --(SQL)-> PostgreSQL: tasks |
        | uvicorn workers |         +------------------+
        +-----------------+                ^
                |                          |
          enqueue (Celery + Redis)         | state
                v                          |
        +-----------------+                |
        | Celery Worker   |----------------+
        | concurrency=N   |
        |   |  spawn      |
        |   v             |
        |  oh -p "..."    |  --> /workspaces/<task_id>/...mp4
        +-----------------+
                |
           copy/move
                v
        /var/openharness/videos/<task_id>.mp4   (shared volume)
```

- FastAPI、Celery worker、oh CLI、hyperframes 全部在同一镜像里（沿用 [Dockerfile](file:///root/projects/OpenHarness/Dockerfile)），不需要 docker-in-docker。
- 横向扩展：`docker compose up --scale openharness=N`，多实例共享 postgres/redis/视频目录卷；或后续把视频目录换成 S3/MinIO（接口已抽象）。

## 2. 目录结构

新建 `service/` 包（与 `src/openharness/` 平级），不污染 OpenHarness 主代码：

```
service/
  app/
    __init__.py
    main.py              # FastAPI 入口
    config.py            # pydantic-settings: DB/Redis/视频目录
    db.py                # async SQLAlchemy engine/session
    models.py            # Task ORM 模型
    schemas.py           # Pydantic 请求/响应模型
    deps.py              # 依赖注入：DB session、storage
    routers/
      videos.py          # /v1/videos/*  路由
      health.py
    storage/
      base.py            # VideoStorage 抽象
      local.py           # 本地共享卷实现
    workers/
      celery_app.py      # Celery app + broker 配置
      tasks.py           # generate_video_task：调 oh、解析输出、落盘
      runner.py          # 调用 oh CLI 的 subprocess 包装
      parser.py          # 解析 "**输出文件:** `...`" 等输出
    alembic/             # 迁移脚本
    alembic.ini
  pyproject.toml         # 仅 service 自身依赖
  README.md
```

## 3. 数据库模型 (`service/app/models.py`)

```python
class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"

class VideoTask(Base):
    __tablename__ = "video_tasks"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    prompt: Mapped[str]                              # 用户原始 prompt
    skill: Mapped[str] = mapped_column(default="hyperframes")
    status: Mapped[TaskStatus] = mapped_column(default=TaskStatus.QUEUED, index=True)
    celery_task_id: Mapped[str | None]
    workspace_path: Mapped[str | None]               # /workspaces/<id>
    output_path: Mapped[str | None]                  # 在共享存储中的最终路径/对象 key
    file_size_bytes: Mapped[int | None]
    duration_seconds: Mapped[float | None]
    resolution: Mapped[str | None]                   # "1920x1080"
    fps: Mapped[int | None]
    exit_code: Mapped[int | None]
    error_message: Mapped[str | None]
    log_tail: Mapped[str | None]                     # 最后 16KB stdout
    created_at / started_at / finished_at: datetime
```

幂等：可选 `idempotency_key UNIQUE` 列，命中时直接返回已有任务。

> **✅ 一阶段修正**：实际实现额外持久化了 `timeout_seconds`（int，默认 900）与 `extra_oh_args`（Text，存 JSON 列表）；`skill` 为定长列（默认 `hyperframes`）；`idempotency_key` 带 `unique=True`。注意：尚无 `worker_id / attempt / heartbeat_at / cancellation_requested / priority`——这些是二阶段崩溃恢复所需（见二阶段 §9/§11）。

## 4. API 端点 (`service/app/routers/videos.py`)

| 方法   | 路径                          | 行为                                                                    |
| ------ | ----------------------------- | ----------------------------------------------------------------------- |
| POST   | `/v1/videos`                  | 提交任务：写 DB + Celery enqueue，返回 `{task_id, status, links}`       |
| GET    | `/v1/videos/{id}`             | 返回任务详情（状态、metadata、log_tail）                                |
| GET    | `/v1/videos/{id}/file`        | 200 时 `StreamingResponse(media_type="video/mp4")` 流式下载             |
| GET    | `/v1/videos/{id}/events`      | SSE 增量推送 stdout 行（可选，便于前端进度展示）                        |
| DELETE | `/v1/videos/{id}`             | 取消队列中的任务 / 删除已完成产物                                       |
| GET    | `/healthz`                    | 健康检查（DB + Redis 连通性）                                           |

请求体 (`schemas.py`)：

```python
class VideoCreateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    timeout_seconds: int = Field(default=900, ge=30, le=3600)
    extra_oh_args: list[str] = Field(default_factory=list)  # 受白名单约束
    idempotency_key: str | None = None
```

POST 响应：

```json
{
  "task_id": "8e4...",
  "status": "queued",
  "links": {
    "self": "/v1/videos/8e4...",
    "file": "/v1/videos/8e4.../file",
    "events": "/v1/videos/8e4.../events"
  }
}
```

## 5. 任务执行 (`service/app/workers/tasks.py`)

核心 Celery 任务：

```python
@celery_app.task(bind=True, name="generate_video", acks_late=True,
                 autoretry_for=(TransientError,), retry_backoff=True, max_retries=2)
def generate_video_task(self, task_id: str) -> None:
    with sync_session() as db:
        task = db.get(VideoTask, task_id)
        task.status = TaskStatus.RUNNING
        task.started_at = utcnow()
        task.celery_task_id = self.request.id
        db.commit()

    workspace = Path(settings.workspace_root) / task_id
    workspace.mkdir(parents=True, exist_ok=True)

    try:
        result = runner.run_oh(
            prompt=task.prompt,
            cwd=workspace,
            timeout=task.timeout_seconds,
            on_log_line=lambda line: _append_log(task_id, line),  # 写日志文件 + 限频更新 DB
            extra_args=task.extra_oh_args,
        )
        mp4 = parser.locate_output_file(result.stdout, workspace)  # 见 §6
        meta = parser.probe_mp4(mp4)                               # 用 ffprobe 抓时长/分辨率/fps
        final_key = storage.save(task_id, mp4)                     # 拷贝到共享卷或 S3
        _mark_succeeded(task_id, final_key, meta, result)
    except Exception as exc:
        _mark_failed(task_id, exc)
        raise
```

`runner.run_oh`（`workers/runner.py`）核心：

```python
cmd = [
    "/root/.local/bin/oh",
    "-p", prompt,
    "--output-format", "text",
    "--permission-mode", "full_auto",
    *extra_args,
]
proc = subprocess.Popen(
    cmd, cwd=cwd, stdout=PIPE, stderr=STDOUT,
    text=True, bufsize=1,
    env={**os.environ,
         "PRODUCER_HEADLESS_SHELL_PATH": "/opt/chrome-headless-shell-linux64/chrome-headless-shell",
         "CHROME_HEADLESS_BIN": "/opt/chrome-headless-shell-linux64/chrome-headless-shell"},
)
# 行级读取 -> on_log_line 回调；Watchdog 超时 -> proc.kill()；返回 RunResult(exit_code, stdout)
```

注意 `cwd=workspace`：hyperframes 默认按相对路径 `boc-promo/renders/...` 输出，固定 cwd 后才能稳定回收产物。

## 6. 输出文件解析 (`service/app/workers/parser.py`)

按优先级匹配：

1. 正则抓取 oh 终态消息中的反引号路径：
   `re.compile(r"\*\*\u8f93\u51fa\u6587\u4ef6:\*\*\s*` + r"`([^`]+\.mp4)`")`，同时兼容英文 `**Output:**`。
2. 兜底：`workspace.rglob("*.mp4")` 找最新 mtime 文件。
3. 兜两次都没有 → `OutputNotFoundError` → 任务标记 FAILED。

`probe_mp4` 用 `ffprobe -v quiet -print_format json -show_format -show_streams` 解析 metadata 写回 DB（duration / resolution / fps / size）。

## 7. 存储抽象 (`service/app/storage/`)

```python
class VideoStorage(Protocol):
    def save(self, task_id: str, src: Path) -> str: ...        # 返回 key
    def open(self, key: str) -> tuple[BinaryIO, int]: ...      # 文件流 + 大小
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
```

**✅ 一阶段修正**：实际 `VideoStorage` Protocol 含四个方法 `save / open / delete / exists`（比初版多出 `exists`）；`LocalVideoStorage` 已落地（共享卷）。S3 后端仍是二阶段事项（见二阶段 §10）。

- `LocalVideoStorage`：根目录 `/var/openharness/videos`，key = `<task_id>.mp4`，多实例通过共享 named volume / NFS 挂载。
- 后续替换 `S3VideoStorage`（boto3）零侵入。

## 8. 流式下载实现 (`routers/videos.py`)

```python
@router.get("/{task_id}/file")
async def download(task_id: UUID, db: AsyncSession = Depends(get_db),
                   storage: VideoStorage = Depends(get_storage)):
    task = await db.get(VideoTask, task_id)
    if not task: raise HTTPException(404)
    if task.status != TaskStatus.SUCCEEDED:
        raise HTTPException(409, detail={"status": task.status})
    fileobj, size = storage.open(task.output_path)
    return StreamingResponse(
        _iterfile(fileobj, chunk=1024 * 1024),
        media_type="video/mp4",
        headers={
            "Content-Length": str(size),
            "Content-Disposition": f'attachment; filename="{task_id}.mp4"',
            "Accept-Ranges": "bytes",
        },
    )
```

> **✅ 一阶段修正**：`Range` 已落地——解析 `bytes=start-` 返回 `206` + `Content-Range`，且整段读取经 `run_in_threadpool` 离线执行，大文件不阻塞事件循环（对应 #3/#8）。

## 9. SSE 进度推送 (`/events`)

> **✅ 一阶段修正**：初版设计的 list+pubsub 存在"重放竞态"（同一行既被 pubsub 直播又被 list 重放，导致重复 `log` 事件）。实际实现改用 **Redis Stream**（`XADD` 写、`XREAD` 带游标同时做重放与实时尾随），一行不会被投递两次，天然有序，并以 `__DONE__` 标记结束（对应 #13）。

## 10. Dockerfile 增量

在现有 [Dockerfile](file:///root/projects/OpenHarness/Dockerfile) 末尾追加：

```dockerfile
# ---- FastAPI 服务依赖 ----
RUN uv pip install --python /root/.openharness-venv/bin/python \
        fastapi==0.115.* uvicorn[standard]==0.32.* \
        sqlalchemy[asyncio]==2.0.* asyncpg==0.30.* psycopg[binary]==3.2.* \
        alembic==1.14.* \
        celery[redis]==5.4.* redis==5.2.* \
        pydantic-settings==2.6.* sse-starlette==2.1.* python-multipart

COPY service /opt/oh-service
ENV PYTHONPATH=/app/src:/opt/oh-service

# 用 supervisord 同容器跑 api + worker（多实例时各副本各自起一份）
RUN apt-get update && apt-retry supervisor \
    && rm -rf /var/lib/apt/lists/* && apt-get clean
COPY docker/supervisord.conf /etc/supervisor/conf.d/oh-service.conf

# 默认入口改为可选：保留 oh，新增 serve
RUN printf '#!/bin/bash\nexec /usr/bin/supervisord -c /etc/supervisor/conf.d/oh-service.conf\n' \
    > /usr/local/bin/oh-serve && chmod +x /usr/local/bin/oh-serve
EXPOSE 8000
```

`docker/supervisord.conf` 简要：

```ini
[supervisord]
nodaemon=true
[program:api]
command=/root/.openharness-venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
directory=/opt/oh-service
[program:worker]
command=/root/.openharness-venv/bin/celery -A app.workers.celery_app.celery_app worker -l info -c 4
directory=/opt/oh-service

[program:beat]
command=/root/.openharness-venv/bin/celery -A app.workers.celery_app.celery_app beat -l info
directory=/opt/oh-service
autostart=true
autorestart=true
environment=PYTHONPATH="/app/src:/opt/oh-service"
```

## 11. docker-compose 增量

在 [docker-compose.yml](file:///root/projects/OpenHarness/docker-compose.yml) 中扩展：

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: oh
      POSTGRES_PASSWORD: oh
      POSTGRES_DB: oh
    volumes: [oh-pg:/var/lib/postgresql/data]

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes"]
    volumes: [oh-redis:/data]

  api:
    extends: openharness                     # 复用同一镜像与挂载
    container_name: openharness-api
    entrypoint: ["oh-serve"]                  # 启 FastAPI + Celery
    depends_on: [postgres, redis]
    ports: ["8000:8000"]
    environment:
      - OH_DB_URL=postgresql+asyncpg://oh:oh@postgres:5432/oh
      - OH_DB_SYNC_URL=postgresql+psycopg://oh:oh@postgres:5432/oh
      - OH_BROKER_URL=redis://redis:6379/0
      - OH_VIDEO_DIR=/var/openharness/videos
      - OH_WORKSPACE_ROOT=/workspaces
    volumes:
      - ./src:/app/src
      - ./ohmo:/app/ohmo
      - ./service:/opt/oh-service          # 开发热重载
      - oh-videos:/var/openharness/videos
      - oh-workspaces:/workspaces
      - openharness-config:/root/.openharness

volumes:
  oh-pg:
  oh-redis:
  oh-videos:
  oh-workspaces:
```

横向扩展：`docker compose up -d --scale api=3`，配前置 nginx/traefik 即可。

> **✅ 一阶段修正**：`oh-serve` 的 supervisord 现同时拉起 api / worker / beat 三个程序；`--scale api=N` 可横向扩展，但多副本下的**任务接管与崩溃恢复 ownership** 一阶段未做（见二阶段 §11）。

## 12. 启动 / 迁移流程

1. 构建：`docker compose build api`
2. 初始化 DB：`docker compose run --rm api alembic upgrade head`
3. 启服务：`docker compose up -d postgres redis api`
4. 调用：

```bash
curl -X POST http://localhost:8000/v1/videos \
  -H "Content-Type: application/json" \
  -d '{"prompt":"帮我用hyperframe这个skill，做一个交通银行的宣传视频。无头浏览器的地址在：/opt/chrome-headless-shell-linux64/chrome-headless-shell"}'
# {"task_id":"8e4...","status":"queued",...}

curl http://localhost:8000/v1/videos/8e4.../events     # SSE 看进度
curl -o boc.mp4 http://localhost:8000/v1/videos/8e4.../file  # 下载
```

## 13. 错误与边界处理

- oh 退出码 ≠ 0 → FAILED，`error_message` = stderr 末段 + exit_code。
- 输出 mp4 找不到 → FAILED，区分 `OutputNotFoundError`。
- `timeout_seconds` 超时 → kill 子进程 + 子进程组（`os.setsid`/`killpg`），状态 FAILED("timeout")。
- DELETE 时若 RUNNING → `revoke(celery_task_id, terminate=True, signal="SIGTERM")` + 落盘清理。
- workspace/log 文件 7 天后由 Celery beat 定时清理（`cleanup_expired_tasks`）。
- API 鉴权（可选）：`X-API-Key` header 中间件，密钥来自 env。
- 并发限制：Celery worker `-c` 控制单实例并发；DB 上为同 idempotency_key 加唯一约束防重复入队。

**✅ 一阶段加固补充**：
- **`extra_oh_args` 白名单（R1）**：新增 `security.py` 显式 allowlist + blocklist；`--permission-mode`/`--output` 等安全关键 flag 不可由调用方覆盖，违规请求在 API 边返回 422。
- **RUNNING 取消真正杀进程组（R2）**：`runner.run_oh` 以 `os.setsid` 让 `oh` 自成进程组，`watchdog` 线程轮询 Redis `oh:abort:<id>` 标志，命中即 `killpg(SIGTERM→SIGKILL)`；worker 在 `run_oh` 返回后二次检查 abort，若已取消则 `_mark_canceled` 且**绝不**回写 SUCCEEDED。DELETE 接口仅设标志 + best-effort `revoke`，权威收尾在 worker 侧（跨副本安全）。
- **幂等竞态不再 500（#9）**：并发重复提交命中 `idempotency_key` 唯一约束时，捕获 `IntegrityError` 回滚并返回已有任务。
- **确定性失败不再重抛（#10）**：`OutputNotFoundError`/普通异常只 `_mark_failed` 不 `raise`，避免消息被无限重投；仅 `TransientError` 触发 `autoretry_for` 重试。
- **cleanup 清理 DB 悬针（#14）**：`cleanup_expired_tasks` 删除产物/workspace/Redis 日志流后，将 `output_path`/`workspace_path` 置 `NULL`，使后续下载干净返回 404。
- **CORS（R6）**：`allow_origins` 仅接受显式来源列表；`allow_credentials` 仅在有显式来源时开启，杜绝 `*`+credentials 反射任意 Origin。
- **API Key（可选）**：`main.py` 中间件校验 `X-API-Key`，`/healthz` 豁免。

## 14. 测试要点

- `tests/service/test_videos_api.py`：mock `runner.run_oh` 返回固定 stdout + 预置 mp4，验证状态机、下载流、SSE。
- `tests/service/test_parser.py`：覆盖中英文输出、多 mp4、无 mp4 三种分支。
- `scripts/smoke_video_service.sh`：起 compose、POST、轮询、下载、校验文件大小。

> **✅ 一阶段修正**：实际测试套件远超预期——`tests/service/` 含 `test_videos_api` / `test_parser` / `test_runner` / `test_worker`（驱动真实任务路径，mock `run_oh`）/ `test_sse`（Stream 无重复）/ `test_streaming`（Range/206 且不阻塞）/ `test_security`（allowlist 422）/ `test_cleanup`（过期清理+置空）/ `test_api_edge`（幂等竞态、取消、边界），全部（约 50 例）通过，并强制保证 `openspec/specs/video-service-hardening.md` 的 R1–R6。

## 15. 后续演进 (不在本期)

- 把 `LocalVideoStorage` 替换为 S3/MinIO，对外发签名 URL。
- 在 nginx 前面加 OAuth2 / API Gateway。
- 引入 OpenTelemetry（FastAPI + Celery 都有现成 instrumentation）。
- 把 Celery 换成 Dramatiq 或 RQ 视团队偏好；本方案的 worker 接口已经隔离在 `workers/tasks.py` 单文件内便于替换。

## 16. 一阶段已知限制与多副本备注

- **崩溃恢复 ownership 未做**：一阶段中若 worker 进程异常退出，其 `running` 任务会滞留 `running`，**无自动接管/重投**；Redis `oh:abort:<id>` 标志仅覆盖"用户主动取消"，不覆盖"进程死亡"。这是二阶段 §11 的核心议题。
- **beat 多副本冗余**：`--scale` 后每副本各起一个 beat；`cleanup_expired_tasks` 幂等（删已删路径为 no-op）故无害，如需单一权威调度器可上 redbeat（二阶段 §4/#15）。
- **Redis Stream 未限长**：日志流用 `XADD` 但未设 `MAXLEN`，超长任务可能增长过大；当前依赖 cleanup 周期删除整条流，必要时可加 `MAXLEN ~10000`（对齐初版 #5 的 1 万上限意图）。
- **健康检查豁免**：`/healthz` 在启用 API Key 时被中间件豁免，前端轮询不受影响。
- **范围**：一阶段不含多租户（tenant/quota/audit），已在二阶段移出。
