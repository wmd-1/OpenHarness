# 三阶段计划 + OpenSpec 审查报告

**审查对象**
- 计划：`.qoder/plans/Phase3_Multi-Tenancy_Temporal_Lease_3217f912.md`
- OpenSpec 变更：`openspec/changes/phase3-multitenancy-temporal-lease/`（proposal.md / specs/video-service-hardening_delta.md / tasks.md）
- 基线：`openspec/specs/video-service-hardening.md`（R1–R13，Phase 2 已归档）

**方法**：先逐条核证计划标注的「VERIFIED」事实（实读 `feature/scale-multi-instance` 归档源码），再比对 plan ↔ openspec 一致性，最后定位设计缺口。

**总体结论**：计划基础扎实、VERIFIED 事实基本属实、openspec 结构合规、章节/需求编号映射一致。但存在 **1 个必须修复的命名错误（F1）**、**3 个高优先级设计问题（F2/F3/F5）** 与若干需澄清的决策（D1–D5）。建议开工前澄清，尤其是 WS-C 与 R9 的冗余关系、以及 WS-B/WS-C 的架构交叉耦合。

---

## 一、VERIFIED 事实核证（全部成立）

| # | 计划声明 | 核对结果 | 证据 |
|---|----------|----------|------|
| 0.1 | `video_tasks` 已有 worker_id/attempt/heartbeat_at/cancellation_requested/priority；无 tenant_id/lease_token | ✅ | `models.py:57-66`（确无 tenant_id/lease_token） |
| 0.2 | `TemporalScheduler` 为占位空实现（抛 NotImplementedError） | ✅ | `scheduler.py:71-82` |
| 0.3 | claim/reclaim 走 PG 行锁；`_mark_succeeded` guard `WHERE worker_id=:wid`，未带 token | ✅ | `tasks.py:83-108`（claim）、`tasks.py:111-144`（_mark_succeeded，仅 `status=RUNNING AND worker_id=:wid`） |
| 0.4 | `/v1/videos` 当前无鉴权中间件 | ✅ | `routers/videos.py` 全文件无 auth dependency / X-API-Key |
| 0.5 | `VideoStorage` 含 `presigned_url`；`S3VideoStorage` 实现 save/open/delete/exists/presigned_url；写路径无 token/条件写 | ✅ | `base.py:46`、`s3.py:48-52`（`put_object` 无 meta/token） |
| 0.6 | `result_backend` 保持 Redis；beat 路由 normal 队列 | 信任（未逐一读） | — |
| 0.7 | `pytest tests/service` → 78 passed/1 skipped；e2e 19/19 | 信任（未跑测试） | — |

> 结论：计划的「代码核实结论」可信，设计前提成立。

---

## 二、必须修复 / 高优先级问题

### F1【必须修复】环境变量命名错误：`SCHEDULER_BACKEND` → `OH_SCHEDULER_BACKEND`

- **证据**：`config.py:63` 字段为 `scheduler_backend`；`scheduler.py:5,87` 实际读取 `OH_SCHEDULER_BACKEND`；全仓 grep `SCHEDULER_BACKEND`（无 `OH_` 前缀）无任何引用。
- **影响**：proposal.md 在 Problem/Solution/Success/Risks 多处、plan §5/§7/§9 均写为 `SCHEDULER_BACKEND=temporal`。实现者照字面配置会静默回退到 Celery 默认，WS-B 等于没做。
- **动作**：proposal.md 与 plan 全文改为 `OH_SCHEDULER_BACKEND=temporal`（delta R19 场景里写的也是 `SCHEDULER_BACKEND=temporal`，需一并改）。

### F2【高】R20 的 DB 终态 fence 与现有 R9 冗余；delta 高估了「新保证」

- **证据**：`recover_lost_tasks` 在 reclaim 时把 `worker_id=None` + `status=RETRYING`（`beat.py:159-167`），随后新 `claim()` 写回新 `worker_id`。因此旧 owner 的 `_mark_succeeded/_mark_failed/_mark_canceled` 守卫 `WHERE status=RUNNING AND worker_id=:wid`（`tasks.py:125-127` 等）**早已命中 0 行**——DB 终态层在 R9（Phase 2）就已受保护。
- **后果**：
  1. delta R20 场景「stale owner cannot write terminal state」即便 **不** 加 lease_token 也会通过——它测的是 R9 既有行为，没有真正锻炼新 token 逻辑。
  2. plan §2 / proposal 称 R8 升级让「preempted owner 不能产生任何有效副作用——terminal state **AND** artifact 都被 fence」。terminal-state 一侧是 R9 既有强保证，并非本期新增；措辞夸大。
- **建议**：
  - 在 delta R20 / plan §8 明确：DB 终态 token 守卫是**防御纵深（defense-in-depth）**，**本期真正的新增益是 S3 产物写 fence**（R9 未覆盖，才是双跑落盘的唯一泄漏点）。
  - 让 R20 的 DB 场景名副其实地锻炼 token（例如断言「即便 worker_id 仍匹配，token 不符也被拒」），或将其定位为「R9 回归 + token 冗余校验」，并**新增专门场景验证 S3 fence**（这才是新逻辑，见 F3 测试）。
  - 把「严格绝不双跑」精确化为「**绝不产生有效双产物/双终态**」——旧 owner 仍会渲染（浪费算力），只是无法落盘，这与 delta R20 的严谨措辞一致。

### F3【高】WS-C 只升级了 `_mark_succeeded`，遗漏 `_mark_failed` / `_mark_canceled`

- **证据**：`tasks.py:147-193` 中 `_mark_failed`、`_mark_canceled` 同为 `WHERE status=RUNNING AND worker_id=:wid` 守卫；但 plan §8.1/§8.4、tasks 4.3 只提 `_mark_succeeded`。
- **影响**：与 R20「Every effectful write MUST carry the current token」自相矛盾；若未来 reclaim 不再清空 worker_id（F2 讨论的备选方案），failed/canceled 路径无 token 守卫会漏。
- **建议**：WS-C 把三个终态写**统一**加 `lease_token=:token` 守卫，或在 plan 中明确「因 R9 的 worker_id 已覆盖，仅 `_mark_succeeded` 示例化 token，但为一致性推荐三处统一加」。

### F5【高】WS-B（Temporal）与 WS-C（lease fence）架构交叉缺口：reclaim 是 Celery 耦合的

- **证据**：存活注册由 Celery 信号 `worker_process_init` 启动（`beat.py:214-220`），reclaim 由 Celery beat 周期任务 `recover_lost_tasks` 驱动（`celery_app.py:42`、`beat.py:223`）。WS-C 的 lease fence **依赖 reclaim 来 bump `lease_token`**；而 WS-B 切换 `OH_SCHEDULER_BACKEND=temporal` 后，渲染由 Temporal Activity 执行，Celery 渲染 worker 不再承接任务——**没有人 bump lease_token / 跑 reclaim**，R20 的「严格 lease」在 Temporal 路径下不成立。
- **影响**：plan 把 WS-B、WS-C 标为「彼此独立、可独立验收」，但二者共享心跳/reclaim 基础设施。R20 的严格保证隐式假设 Celery 后端。
- **建议（在 plan 中明确其一）**：
  - (a) **范围声明**：WS-C 的严格 lease 保证仅在 Celery 后端有效；Temporal 路径下 lease fencing 由 Temporal 自身的 activity heartbeat + timeout 机制承担（需补充设计，不能沿用 `beat.py` reclaim）。
  - (b) 把 reclaim/watchdog 抽到与后端无关的位置（独立进程 / PeriodicTask），Temporal Activity 死亡时也由它 bump token。
  - 至少要在 WS-B §7 与 WS-C §8 增加「跨后端一致性」说明，避免 R20 在 Temporal 下失守。

---

## 三、重要设计决策缺失（建议澄清，非 blocker）

### D1 WS-A 隔离强制机制未定：DI + WHERE 不可靠，RLS 才是硬保证
- plan §4 把 RLS 列为「可选缓解」，§6.1 只加 `tenant_id` 列 + 索引；交付清单无 RLS。但「tenant_id 过滤遗漏致越权」是表内最高风险（High/High）。
- 建议：要么采用 **PostgreSQL RLS**（每个连接 `SET app.current_tenant=` + 策略 `USING (tenant_id = current_setting(...))`），要么建立**集中查询层/仓库模式**强制所有 `video_tasks` 访问带 tenant 过滤，并配齐越权用例。
- 补充：`R14` 目前只测 GET/DELETE 单条（delta 两个 scenario），**list 端点跨租户也应覆盖**（建议补 scenario）。是否 FK 到 `tenants` 表、`system` 默认租户预置、内部 system 调用（cleanup/reclaim）如何携带 tenant 上下文——任务行自带 `tenant_id`，worker 侧无需请求上下文即可读取，OK，但需在 plan 点明。

### D2 WS-A 限速在多副本下会失准（slowapi 默认内存）
- Phase 2 已支持 `api×N` 副本。`slowapi` 默认**每进程内存计数器** → 每副本独立计数，N 副本实际放行约 `N × rate_per_min`。
- plan §6.3 提到「Redis 计数器（推荐）」但交付用 slowapi；需明确 **slowapi + Redis limiter backend（共享存储）**，否则限速形同虚设。建议写进 WS-A §6.3 与 tasks 2.3。

### D3 `claim()` 签名需返回 token（WS-C 落地点未明）
- plan §8.1「worker 进程内存持有当前 token」，但 `claim()` 当前返回 `bool`（`tasks.py:83-108`）。需要 `RETURNING lease_token` 或由 worker 回读。tasks 4.2 未说明 token 如何到达 worker 内存。
- 建议：明确 `claim()` 改为返回 `(claimed: bool, token: int)`，或调用方 claim 后查询；reclaim 同理 bump 并把新 token 交给新 owner。

### D4 Migration 编号正确（004/005 无冲突）✅
- 现有 head 为 `003_storage_kind.py`；`004_tenant` / `005_lease_token` 顺序合理。仅提醒：004 与 005 必须保持前后依赖顺序，且 WS-A/WS-C 各自独立版本号（已满足）。

### D5 delta MODIFIED R8 文本完整性 ✅（archive 时需注意）
- delta 的 MODIFIED R8 已含原「alive worker not reclaimed」场景 + 新「reclaim bumps lease_token」场景，归档合并基线后 R8 文本完整。
- 注意：基线 R8 原有 NOTE「Phase 2 非 lease，残余风险接受，不作为 gate」被升级 NOTE 替换——这是预期的，但 **archive 时需核对 `video-service-hardening.md` 的 R8 NOTE 同步更新**，否则基线会同时残留旧 NOTE。

---

## 四、一致性小结（plan ↔ openspec）

| 维度 | 结论 |
|------|------|
| 文件映射（004/005 migration、auth.py、quota.py、audit.py、temporal_worker.py、s3.py、ws_a/b/c 测试、pyproject） | plan §11 ↔ proposal 实现文件映射 **完全一致** ✅ |
| 章节映射（§0–§12 ↔ proposal 表格） | 一致 ✅ |
| 需求编号（R14–R20 + MODIFY R8） | plan §8.2 ↔ delta 一致 ✅ |
| 测试文件名（ws_a_tenant_isolation / ws_a_auth / ws_a_quota / ws_a_ratelimit / ws_a_audit / ws_b_temporal / ws_c_fencing） | plan §6.5/§7.3/§8.4 ↔ tasks.md Phase 2/3/4 **一致** ✅ |
| OpenSpec 目录结构（proposal + specs/*_delta + tasks） | 合规 ✅ |
| 成功标准（plan §5 ↔ proposal Success Criteria） | 逐条对应 ✅ |
| **环境变量名** | ❌ proposal 用 `SCHEDULER_BACKEND`，应为 `OH_SCHEDULER_BACKEND`（见 F1） |

---

## 五、优先级排序与建议动作

1. **[Blocker] F1**：proposal.md + plan 全文 `SCHEDULER_BACKEND` → `OH_SCHEDULER_BACKEND`（含 delta R19 场景）。
2. **[High] F2 / F3**：厘清 R20 与 R9 关系，明确「S3 fence 才是新增益、DB 为防御纵深」；统一三处终态写守卫；强化 R20 测试真正覆盖 token 与 S3。
3. **[High] F5**：解决 WS-B/WS-C 交叉耦合，明确 Temporal 路径下 lease 保证来源（范围声明或抽离 reclaim）。
4. **[Med] F4**：去除冗余 Redis `oh:lease:{task_id}` TTL，fence 以 PG token 为准；改用「`save`/重渲染前从 PG 重读 token 比对」避免浪费算力。
5. **[Med] D1 / D2**：隔离强制机制决策（RLS 或集中查询层 + list 越权用例）；跨副本限速用 Redis limiter backend。
6. **[Low] D3 / D5**：`claim()` 返回 token；archive 时核对基线 R8 NOTE 同步。

> 以上除 F1 为硬性命名错误外，其余多为「开工前需拍板」的设计澄清。计划与 openspec 整体质量高、可落地，修完 F1–F5 即可进入实施。

---

## 六、修复记录（2026-07-14，已应用）

以下修改已写入计划文档与 openspec 变更（proposal / delta / tasks），grep 校验通过（无裸 `SCHEDULER_BACKEND` 残留）。

| 编号 | 修复内容 | 落点 |
|------|----------|------|
| **F1** | `SCHEDULER_BACKEND` → `OH_SCHEDULER_BACKEND`（plan / proposal / delta / tasks 共 16 处） | plan §1/§2/§5/§7(×2)/§12；proposal 问题/方案/范围/架构/成功；delta R19(×3)；tasks 3.5 |
| **F2** | R20 明确 DB 终态守卫为 **defense-in-depth**（R9 的 worker_id 已覆盖），**S3 产物写 fence 才是本期核心新增**；措辞改为「no valid duplicate side effect」，并注明旧 owner 仍会浪费算力 | delta R20 全文重写（含新增 scenario 说明三处终态写均带 token） |
| **F3** | WS-C 三处终态写（`_mark_succeeded`/`_mark_failed`/`_mark_canceled`）统一加 `lease_token` 守卫 | plan §8.1/§8.4；tasks 4.3 |
| **F4** | 去除冗余 Redis `oh:lease:{task_id}` TTL；fence 以 PG token 为准；改为「重渲染前 / `save` 前从 PG 重读 token 比对，stale 则提前中止」 | plan §8.1/§9；tasks 4.5 |
| **F5** | 新增「WS-B × WS-C 后端耦合」说明：reclaim 耦合 Celery，Temporal 路径下须明确 lease 保证来源（范围声明 或 抽离 reclaim） | plan §8.2/§9；proposal Architecture Considerations |
| **D1** | WS-A 隔离强制机制决策：优先 **PostgreSQL RLS**（`SET LOCAL app.current_tenant` + 策略），退化方案为集中查询层；`tenant_id` 加 FK → `tenants(id)` 且预置 `system`；补 list 跨租户场景 | plan §6.1；delta R14 新增 `cross-tenant list is scoped` |
| **D2** | 跨副本限速须用 **slowapi + Redis limiter backend**，否则 `api×N` 下实际放行 N×rate | plan §6.3；tasks 2.3 |
| **D3** | `claim()` 改为返回 `(claimed, token)`，由调用方持有当前 token | plan §8.1；tasks 4.2 |
| **D5** | archive 前须核对基线 R8 NOTE 同步为「strict lease」版本（删 Phase 2 旧 NOTE 残留） | tasks Completion Checklist |

**未改动（保持原样）**：VERIFIED 事实核证结论、OpenSpec 目录结构、R14–R20 / MODIFY R8 编号体系、测试文件名映射、migration 编号 004/005（已确认无冲突）。

**修复后状态**：F1–F5、D1–D5 全部落实；计划与 openspec 现已自洽，可进入实施。仍建议在 Phase 4（WS-C）实现时按 §8.2 二选一明确 Temporal 路径下的 lease 保证落地方式。
