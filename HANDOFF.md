# OpenHarness HyperFrames 视频服务 — 工作交接简报（2026-07-10）

> 给新工作空间/agent 的上下文。读完即可接手二阶段实施。
> ⚠️ 本简报已于 2026-07-10 由接手 agent 同步至「二阶段 OpenSpec 经 superpowers 完善后」的状态（tasks 7 Phase / delta R7–R13 / 五支柱）；本次修订细节见文末「📝 本会话修订记录」。

## 1. 项目是什么
- 仓库：OpenHarness（fork 自上游；PR 提往 `wmd-1/OpenHarness` 的 `main`）。
- 组件：`service/` = HyperFrames FastAPI + Celery 视频生成服务（渲染用 `oh` CLI / Chrome / ffmpeg）。
- **规范工作目录（本次会话归正后）**：`D:\WorkBuddy-Workspace\Openharness_hyperprames_Development\OpenHarness\`（纯英文，避免中文路径挂载抖动）。旧的中文目录（`Openharness_hyperprames开发` 带/不带 f）均为遗留，勿在此工作。

## 2. 当前进度
- **一阶段 `harden-hyperprames-video-service`**：已实施并合并入 `main`。14 项加固（extra_oh_args allowlist、CORS 不配通配+凭证、supervisord `[program:beat]` 定时清理、取消杀进程组且不得标 SUCCEEDED、Redis 连接池、Stream 替代 list+pubsub 实现 SSE、确定性失败不重抛、cleanup 置空指针、Range/206 流式、idempotency 竞态修复、删除置空、移除死依赖 ffmpeg-python、对象存储 presigned 预留）。
  - 基线契约：`openspec/specs/video-service-hardening.md`（R1–R6），是事实真相源。
- **二阶段 `scale-multi-instance`**：设计已完成并**正式落成 OpenSpec change**，但**尚未实施（零代码改动）**。

## 3. 二阶段 OpenSpec change 位置
`openspec/changes/scale-multi-instance/`
- `proposal.md` — 问题 / 方案 / 范围 / 影响 / 风险 / 成功标准。
- `tasks.md` — **7 个 Phase，每阶段带 Quality Gate**（实施顺序与验收门禁，务必按序）。
- `specs/video-service-hardening_delta.md` — MODIFY R3 + ADD R7–R13（含 Scenario）。
- 设计源文档：`.qoder/plans/Phase2_Multi-Instance_Scaling_3217f912.md`（完整代码级设计，区分 VERIFIED/INFERRED，§11 含边界与剩余风险）。
- **📝 本会话修订补充**：新增 **Phase 7（Worker 并发控制：优先级队列 + 全局信号量 `MAX_CONCURRENT_RENDERS`）** 与对应 **R13**（兑现 proposal Scope 中"单实例内可控并发"承诺）；proposal 五大支柱补第 5 点"单实例并发控制"并新增 Redis 高可用边界声明。详见文末「📝 本会话修订记录」。

## 4. 关键技术定性（务必记住，避免重蹈过度承诺）
- **⚠️ Ownership/Reclaim 是 heartbeat + Redis TTL 机制，不是严格 lease/fencing。** 它能显著降低双跑、并用 success guard（DB 行级条件 UPDATE）可靠防止**终态覆盖**，但**不能证明"绝不双跑"**。Redis 网络抖动 / 进程长 GC·STW / Redis failover 丢键等异常下，注册键可能消失而进程仍活，导致误 reclaim → 短暂双跑（剩余风险，design source §11.7 B1–B5）。**文档/注释里不要写"零双跑/杜绝/worker_alive=False⟺进程已死"这类绝对表述。**
- **`result_backend` 保持 Redis**（不改为 PG）：状态强一致靠 `video_tasks` 行锁 claim，不依赖 result backend。决策理由见 design source §8.2。
- **多租户**（tenant_id / API Key / quota / audit / 按租户限速）已移出二期，列 Future Work。
- 对象存储 S3 是新增功能，无存量实现；`VideoStorage` Protocol 已含 `save/open/delete/exists`，**缺 `presigned_url`**（S3 类还需补齐 `delete/exists`）。
- 拓扑用 `docker compose ... --scale worker=N --scale api=M`（非 swarm，`deploy.replicas` 被忽略）；拆分容器须保留 `PYTHONPATH=/app/src:/opt/oh-service` 与 `working_dir`。

## 5. 已验证事实（VERIFIED，来自实读代码，非推断）
- `video_tasks` 表**已有** `idempotency_key`(UNIQUE)；**尚无** `worker_id` / `attempt` / `heartbeat_at` / `cancellation_requested` / `priority`。
- `celery_app.py`：`backend=settings.broker_url`（Redis）；`task_acks_late=True`、`worker_prefetch_multiplier=1`、`task_track_started=True`。
- `Dockerfile` 已 `ENV PYTHONPATH=/app/src:/opt/oh-service`（约第 143 行）。
- `docker-compose.yml` **无** `deploy.replicas`；`api` 经 `oh-serve` 跑 supervisord（api+worker+beat 同容器）。
- 取消沿用 Redis key `oh:abort:{task_id}`（天然跨副本，无需新增 Pub/Sub）。

## 6. 下一步：实施 Phase 1（详见 `tasks.md`）
Phase 1 = 数据模型与行锁状态机（自包含、低风险，建议从这里起步）：
1. **Alembic 迁移**：`video_tasks` 加列 `worker_id`(VARCHAR) / `attempt`(INT DEFAULT 0) / `heartbeat_at`(TIMESTAMP 可空) / `cancellation_requested`(BOOL DEFAULT false) / `priority`(INT DEFAULT 5)；backfill 旧数据。
2. **`tasks.py` 实现 `claim(task_id, worker_id) -> bool`**：原子条件 `UPDATE ... SET status='running', worker_id=:wid, attempt=attempt+1, heartbeat_at=now() WHERE id=:tid AND status IN ('queued','retrying') RETURNING id`（Postgres 行锁，并发只一个命中）。
3. **`_mark_succeeded` 加 success guard**：`UPDATE ... SET status='succeeded', ... WHERE id=:tid AND status='running' AND worker_id=:current_wid`（worker_id 不符 → 0 行 → 拒写，防 clobber）。
- **Phase 1 门禁**：claim 幂等测试（两 worker 并发只一个命中）+ success guard 防覆盖测试（旧 worker_id 写 0 行）+ 迁移 `alembic upgrade head` 成功。
- 后续 Phase 2–7（存活注册/心跳/reclaim/取消持久化 → 对象存储 → 拓扑拆分 → 可观测性 → 调度器抽象+全量测试 → **Phase 7 单实例并发控制：优先级队列 + 全局信号量**）见 `tasks.md`，每阶段有门禁。

## 7. 测试与运行
- 单测用 `fakeredis` / `aiosqlite`（无需真 Redis/PG 即可跑 `tests/service/`）。
- 全量：`pytest tests/service/`。一阶段已 **50 passed**。
- 代码静态检查：ruff / 类型。

## 8. 文档索引
| 内容 | 路径 |
|------|------|
| 一阶段计划（已补充落地情况） | `.qoder/plans/FastAPI_Hyperprames_Video_Service_3217f912.md` |
| 二阶段设计源（VERIFIED/INFERRED 区分） | `.qoder/plans/Phase2_Multi-Instance_Scaling_3217f912.md` |
| 一期基线 spec（R1–R6） | `openspec/specs/video-service-hardening.md` |
| 二阶段 change（proposal/tasks/delta） | `openspec/changes/scale-multi-instance/` |
| git 远程 / PR | origin = `wmd-1/OpenHarness`；无 gh CLI，PR 用 `GITHUB_TOKEN` + `curl --proxy 127.0.0.1:10808` 调 REST API |

## 9. 给新 agent 的提醒
- 不要重写一期逻辑，只在既有 seam（`app/workers/tasks.py`、`runner.run_oh`、`storage/base.py` 等）内扩展。
- 所有二阶段新增需求以 `openspec/changes/scale-multi-instance/specs/video-service-hardening_delta.md` 的 **R7–R13** 为契约（R13 为本会话新增，对应 Phase 7 并发控制）；实施完成后用 `/openspec-archive` 把 delta 并入基线 spec。
- 涉及"双跑/lease"的措辞严格按 §4 定性，不要过度承诺。

---

## 📝 本会话修订记录（2026-07-10，接手 agent 用 superpowers 完善二阶段 OpenSpec 后同步）

> 以下改动由当前工作空间的接手 agent 在通读并评审二阶段 OpenSpec 后完成。目的：补上 proposal Scope 已承诺但 tasks 未落地的「单实例内可控并发（队列分级 + 并发上限 + 全局信号量）」缺口，并前置标注 4 个易踩坑的实现细节。

### 一、对 OpenSpec 三件套的改动（本会话，superpowers 完善）
- **`proposal.md`**：四大支柱补第 5 点「单实例并发控制」；Risks 表新增 **Redis 高可用边界声明**（Redis HA 不在本 change，全不可用→存活注册/heartbeat/取消 key 全部失效、reclaim 与跨副本取消语义降级，属 §11.7 剩余风险延伸，不作正常宕机门禁）。
- **`tasks.md`**：头部「Phase 1→6」改为「Phase 1→7」；Phase 1 加 alembic **回滚顺序**注记（先回退引用新列的代码，再 `alembic downgrade`，否则启动即崩）；Phase 2 task 6 修正 `worker_alive` 为**应用层查 Redis 得 alive 集合再拼 `!= ALL(:alive_workers)`**（非 PG 函数）；Phase 3 task 12 **存量 S3 迁移澄清**（新任务默认 `storage_kind=s3`，存量视频保持 `local` 继续流式，可选回填脚本非门禁）；**新增 Phase 7 — Worker 并发控制**（task 25–27 + Quality Gate：按 `priority` 路由分级队列 + 全局信号量 `MAX_CONCURRENT_RENDERS` 限制同时运行的 `oh` 渲染进程数，防 Chrome/ffmpeg OOM）。
- **`specs/video-service-hardening_delta.md`**：概述「6 处 ADD（R7–R12）」→「7 处 ADD（R7–R13）」；**新增 R13 — Worker concurrency control**（优先级队列路由 + 全局并发信号量，含 2 个可验证 scenario）。

### 二、对本文档（HANDOFF.md）的同步（本次修订）
- 顶部新增同步提示行，声明已对齐至完善后状态。
- §3：`tasks.md` **6 Phase → 7 Phase**；delta **R7–R12 → R7–R13**；并补「📝 本会话修订补充」一行。
- §6：后续 Phase 范围 **Phase 2–6 → Phase 2–7**，补 Phase 7 并发控制说明。
- §9：契约引用 **R7–R12 → R7–R13**（注明 R13 为本会话新增、对应 Phase 7）。
- 其余（§1 / §2 / §4 / §5 / §7 / §8）保持不变，经核对仍准确。

### 三、状态确认
- 二阶段代码**仍未实施（零代码改动）**，Phase 1 起点步骤（§6）依然有效，可从 `claim()` / success guard 起步。
