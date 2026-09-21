# C15 design — durable work queue 消费层

> 引用已冻结决策（§5.9.5 / §5.9.6 / §5.9.9 / §5.9.11 / §7.1 / §10 DECIDED Queue 容量初始值、Persistence ownership），不重复论证。
> 本文档冻结的每一项都是**评审输入**；实现不得偏离，偏离需先改 design。

---

## ADR-1 复用 lease 模式与既有状态词汇，不新造状态机

**结论**：work item 状态沿用数据库已有的 `queued / in_progress / succeeded / failed / cancelled`；认领把 `queued`（或到期的 `failed`、或租约过期的 `in_progress`）推进为 `in_progress` 并写入租约。**不引入** `pending / processing / done` 这一套新词汇。

**理由**

- 该 CHECK 约束已在生产 schema 里（`ck_background_work_items_status`），改名需要迁移 + 改 C2 的 spec 断言，收益为零。
- C2 已在 `outbound_delivery_intents` 上用 `pending/attempting/sent/failed/dead_letter` + lease 证明了模式可用；differences 只在词面。两套词并存已足够表达语义，再加第三套只会加剧混乱。
- 「processing / done」与 `in_progress / succeeded` 完全同义，属纯改名。

**术语映射（供实现与文档统一口径）**

| 通常说法 | 本项目实际词汇 |
| --- | --- |
| pending | `queued` |
| processing | `in_progress` |
| done | `succeeded` |
| failed | `failed` |
| 超时重置为 pending（崩溃恢复） | 清扫把过期 `in_progress` **复位为 `queued`** |

**备选**：改用 `pending/processing/done/failed`——不选：迁移成本 + 与 C2 已有 spec 冲突 + 无信息增益。

---

## ADR-2 表结构扩展用 expand-only 迁移，5 列 + 1 索引

**结论**：新 alembic revision（`down_revision = f3c8a9d2e7b4`，当前 head）对 `background_work_items` 执行纯 additive DDL：

| 列 | 类型 | 约束 | 用途 |
| --- | --- | --- | --- |
| `attempt_count` | `INTEGER` | `NOT NULL DEFAULT 0` | 本周期尝试计数（`claim` 时 +1；供 `max_attempts` 判据） |
| `lease_owner` | `VARCHAR(128)` | `NULL` | 认领者标识 |
| `lease_expires_at` | `TIMESTAMPTZ` | `NULL` | 租约到期（`claim` 时 `now() + lease_ttl`） |
| `next_attempt_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT now()` | 到期后可再认领的时间（退避写入） |
| `last_error` | `TEXT` | `NULL` | 最近失败原因（入库前过 redaction） |

外加 `CREATE INDEX ix_background_work_items_claim ON background_work_items (status, next_attempt_at)`（claim 扫描路径；既有 `(tenant_id, status)` 索引保留给租户过滤查询）。

**理由**

- §5.9.9 要求 schema 演进按 expand → backfill → cutover；本变更**只做 expand**：新列全部有 DEFAULT 或可 NULL，既有行无需回填，读路径不改签名的兼容性风险为零。
- 与 `outbound_delivery_intents` 的 lease 列命名保持一致（`lease_owner`/`lease_expires_at`/`attempt_count`/`next_attempt_at`/`last_error`），使两表的认领语义可被同一套心智模型与同一套测试模式覆盖。
- 不新增 `work_attempts` 审计表：delivery 侧有 `delivery_attempts` 是因为要向管理员呈现 provider receipt；work item 无外部 provider，`attempt_count + last_error` 已足够，避免为对称而建空表（**若后续需要逐次审计再单列扩展**）。

**回滚**：`DROP INDEX` + 5 个 `DROP COLUMN`（§5.9.9 允许纯 additive 变更直接反向）。无 backfill 数据需要撤销。

---

## ADR-3 崩溃恢复：清扫复位为 `queued` 为主，claim 的 stale 分支为兜底

**结论**：提供 `sweep_stale_leases(grace_seconds)`，把 `status = 'in_progress' AND lease_expires_at < now()` 的行**复位为 `queued` 并清空租约**，记录一条 recovery 观测（`recovery_action = recompute` / `restart_to_recovered_ms`）。claim 语句**同时**保留 stale `in_progress` 接管分支，作为清扫未及时运行时的兜底。

**理由**

- 崩溃恢复的**实质**是「进程消失后其持有的租约不再续期，到期后必须能被重新执行」。清扫与 claim-stale 接管都能达成；两者都保留是因为语义不同：
  - **清扫**表达「这一轮执行从未收束」，产出可观测记录（这正是 §5.9.6 要求「每次恢复记录 recovery_started_at/finished_at/action/result」的落点），也让 `attempt_count` 的语义保持「真实执行次数」。
  - **claim-stale 接管**保证即使在清扫周期间隔内，到期 work 也不会饿死。
- 复位为 `queued` 而非「原地换 owner 继续 `in_progress`」：前者让「未收束」这个事实在状态上可见（下一位认领者从干净前置开始），并与用户诉求「超时重置为 pending」一致。
- 复位**不**递增 `attempt_count` 之外的语义：`attempt_count` 只由 `claim` 递增，因此崩溃次数不虚增尝试次数（崩溃不是业务失败，不该触发退避耗尽 → `failed`）。反之，如果崩溃也计入 attempts，一次反复崩溃的 work 会在没有任何业务失败的情况下被判定 `failed` —— 这是**错误**的终态。

**边界**：work item 的 handler 必须是**可重放或幂等**的（§5.9.6「纯内部、声明幂等的 work 可 recompute」）。带外部副作用的 handler 必须自带幂等键并落在 ADR-6 的同事务协议内；本 change 不为 handler 提供「禁止无确认重放」的自动判定（那是 C2 `tool_calls` 的 `unknown/compensation_required` 职责）。

---

## ADR-4 （关键）claim 的两个 per-tenant 条件，使租约只覆盖执行期且租户内不并发

**结论**：`claim_batch` 的单条 SQL 必须**同时**包含两个 per-tenant 条件，缺一不可：

1. **在途互斥**：该 tenant 若已有 `status='in_progress' AND lease_expires_at >= now()` 的行，则本轮**不认领它的任何工作项**；
2. **轮内去重**：同一 tenant 本轮至多返回 1 条（用 `NOT EXISTS` 取 `(next_attempt_at, created_at, id)` 最小者），而不是一次认领它的多条。

两者叠加后，`lease_ttl` **只需覆盖单次 handler 执行**，不需要覆盖任何排队时间——这是本 ADR 最重要的简化结论。tenant lane 因此是**执行串行化装置，而不是缓冲区**；每租户在途 ≤ 1，跨租户并发。

**为什么必须要两个条件（2026-09-21 在真 PG 上实测发现）**

只写条件 2 是**不够**的，这是本 change 实现阶段修正的一处真实设计缺陷。实测：某 tenant 有 3 条 `queued`，第一次 claim 取走 1 条（转 `in_progress`，租约有效）；**第二次 claim 仍会取走该 tenant 的下一条 `queued` 行**——因为「已持租的那条」不再满足 due 判据，于是退出候选集，该 tenant 的另一条 `queued` 行成了它自己的最小候选。

后果正是本 ADR 要排除的陷阱：**同租户两条同时在途**；若投入 lane 排队，则排队那条持租不心跳 → 超 `lease_ttl` → 清扫复位 → 重复执行。因此条件 1 是「在途 ≤ 1」的**真正实现**，条件 2 只负责防止单轮取多条。

**为什么在途互斥放在 SQL，而不是 worker 记账**

worker 侧维护「本进程该 tenant 是否在途」也能挡住单 worker 场景，但 SQL 条件 1 额外挡住：进程重启后旧租约未过期时的重复认领、未来多 worker、以及 worker 记账 bug。把不变量放在数据层是这里唯一稳妥的位置。

**已实测的 SQL 形状**

```sql
WITH picked AS (
    SELECT w.id FROM background_work_items w
    WHERE (<due(w)>)
      AND NOT EXISTS (SELECT 1 FROM background_work_items a
                      WHERE a.tenant_id = w.tenant_id
                        AND a.status = 'in_progress'
                        AND a.lease_expires_at >= now())              -- 条件 1
      AND NOT EXISTS (SELECT 1 FROM background_work_items p
                      WHERE p.tenant_id = w.tenant_id
                        AND (<due(p)>)
                        AND (p.next_attempt_at, p.created_at, p.id)
                            < (w.next_attempt_at, w.created_at, w.id)) -- 条件 2
    ORDER BY w.next_attempt_at, w.created_at, w.id
    LIMIT :batch_size
    FOR UPDATE OF w SKIP LOCKED
)
UPDATE background_work_items i SET ... FROM picked WHERE i.id = picked.id RETURNING ...
```

**实现约束：条件 2 不能用窗口函数表达。** PostgreSQL 的 `FOR UPDATE` **不允许与窗口函数同层**（`ERROR: FOR UPDATE is not allowed with window functions`），`DISTINCT ON` 同样与 `FOR UPDATE` 冲突。这是选相关子查询 `NOT EXISTS` 形式的原因（该形状已在真 PG 上验证可用）。

**已排除的候选**

| 方案 | 结果 | 取舍 |
| --- | --- | --- |
| (a) 两个 per-tenant 条件（**选中**） | 在途 ≤ 1 由数据层保证；租约只覆盖执行期 | 单轮吞吐受 batch_size 与就绪 tenant 数限制——这是**有界背压**的正面表达，非缺陷 |
| (b) 只做轮内去重（即 `row_number()` 式写法） | **实测无效**（见上）；且窗口函数写法根本不被 PG 接受 | ❌ |
| (c) 允许同租户多条排队、让排队项也心跳 | 租约语义被拉长为「从认领到结束」 | 心跳协程随队列深度增长；失租判据不再对应「执行者是否活着」 |
| (d) 在途互斥只由 worker 记账保证 | 单 worker 可行 | 进程重启遗留的有效租约、未来多 worker、记账 bug 均会漏；不变量应在数据层 |

**推论的实现约束**

- 认领量上限 = `batch_size`，且 ≤ 就绪 tenant 数；**背压第一层**。
- 每租户在途 = 1 由条件 1 保证；**背压第二层**（等价于 §5.9.5 的「tenant 内 active work 仍为 1」）。
- **已知取舍：租户内队头阻塞**。若某 tenant 的在途 handler 一直存活并续租，该 tenant 的后续工作项会等待。这是「同租户串行」的必然结果，比「同租户并发」更符合 §5.9.5；但必须可观测（队列等待耗时），否则表现为无解释的停滞。
- handler 执行期中断（`CancelledError`）必须走 `record_work_failed` 或让其租约自然到期，**不得**留下永不释放的 `in_progress`（见 ADR-7 的停止语义）。

**验证**：`openspec/evidence/c15-work-queue-consumer/claim-sql-smoke.md` 记录了 15 项在真 PG（PG18 + 最小 schema）上的实测，含本条条件 1 的反例与修正后复测全绿。

---

## ADR-5 同租户串行复用 C3 `TenantLaneRouter`，不新造第四套 lane

**结论**：worker 内部按 `tenant_id` 把认领项交给 `agent/admission/lanes.py::TenantLaneRouter` 执行：`work_kind == interactive` → `run_interactive`，其余（maintenance 类）→ `run_maintenance`。**不**在 worker 内自建 per-tenant `asyncio.Queue`。

**理由**

- 仓库现有三套同租户串行实现（`ConversationRuntime._admissions`、`PassiveMessageWorker._lane_queues`、`markdown maintenance` 的单槽），再加一套即第四套——这正是本 change 要收敛的问题，不能反手加深。
- `TenantLaneRouter` 的契约已在 C3 冻结并有测试（同租户串行、interactive 优先、owner 在成功/失败/取消/超时/关闭路径统一释放、`close()` 拒绝新 work），且**至今未被任何生产路径使用**（C3 §6 记录在案）。本 change 给它第一个生产消费者，属净收益。
- `run_maintenance` 在 interactive 忙时抛 `MaintenanceDeferred`，正好对应 §5.9.5「maintenance 可延后且可重算」——被延后的 work 由调用方**释放租约并排程重试**（不视为失败），语义自洽。

**与 C3 §6 边界的关系**：C3 §6 说「router 统一迁入生产留待 C8/C9 接 `TenantRuntimePlan`」——那指的是把**既有三条内存路径**迁进 router。本 change 不迁它们，只是**新增**一个消费者，不与之冲突。

**备选**：worker 内自建 per-tenant queue + lock——不选：第四套实现、重复 C3 已测试的释放语义。

---

## ADR-6 effectively-once：副作用写入与 `succeeded` 同事务 + `idempotency_key`

**结论**：handler 的副作用写入与 work item 终态推进**必须在同一数据库事务内提交**，且该事务以「租约仍属本 owner 且状态为 `in_progress`」为 CAS 前置。work item 的创建侧沿用既有 `idempotency_key` 唯一约束。

**理由**

- 这是 §5.9.11 在 turn/outbox 上已确立的协议（执行完成事务 = final message + turn 终态 + outbox intent 同事务），work item 沿用同一形状：**要么「副作用 + succeeded」都在，要么都不在**。
- 只靠 `idempotency_key` 不足以防重复执行（它防的是**重复入队**，不防**重复执行**）；只靠 lease 也不足以防「执行成功但状态未写」导致的重放。两者叠加才是 effectively-once：
  - 入队侧：`idempotency_key` 唯一约束 ⇒ 同一逻辑工作不会产生第二行；
  - 执行侧：`claim` 的 `attempt_count+1` + 租约 CAS ⇒ 同一行同一时刻只有一个执行者；
  - 提交侧：副作用与 `succeeded` 同事务 ⇒ 不存在「副作用生效但状态仍是 in_progress」的窗口；
  - 崩溃侧：ADR-3 的复位只可能造成**重放**，由 handler 自身幂等（或 ADR-6 的事务协议）吸收。
- 因此本 change 对外的承诺是 **effectively-once**（不是 exactly-once）：在 handler 满足「幂等或与终态同事务」的前提下，可观察副作用恰好一次。

**接口约束**：`record_work_succeeded(tenant_id, work_item_id, owner, *, mutate: Callable[[Session], None])` —— 终态推进与 handler 的持久化写入共用同一 `Session`/事务；不提供「先写副作用、再单独推进终态」的入口，从 API 形状上排除误用。

---

## ADR-7 运行期接线：首次建立 control plane async engine，并纳入优雅停止

**结论**：在 bootstrap 建立 control plane 的 async engine + session factory（`bootstrap/db/engine.py` 已有工厂，但生产从未调用），构造 `WorkItemRepository` + `WorkQueueWorker`：
- 启动：`AppRuntime.start()` 内在 `provisioning_worker.start()` 之后创建并启动 worker task（照 `TenantProvisioningWorker.start()/stop()` 的生命周期模式，不使用裸 `asyncio.create_task` 于 tasks 列表之外）。
- 停止：加入 `AppRuntime.shutdown()` 的 `_run_cleanup_steps`，位置在 `conversation_runtime.shutdown` 之前、`core.stop` 之前；`stop()` 语义 = 置停止标志 → 停止认领新 work → 等待在途 handler 收束（**不**中途取消正在执行的 handler，避免留下「副作用已写但状态未推进」）。
- engine 释放：cleanup 内 `engine.dispose()`，避免连接泄漏。
- 尊重既有的 PG/SQLite 二态：`storage.backend == "sqlite"` 时不建 control plane engine、不启 worker（旧单体路径不受影响）。

**理由**

- 当前生产**没有任何 control plane engine**，这是 durable 边不可达的直接原因；不建 engine，后面的 claim 代码写了也没人跑。
- `AppRuntime.start()` 已有 `provisioning_worker` 这一「durable-ish worker 生命周期」先例与 `_run_cleanup_steps` 有序停机框架，照抄即可，不需要新机制。
- 停机顺序敏感：必须在 `core.stop`（拆 provider/存储）之前停下 worker，否则 worker 会在依赖已销毁后仍尝试写库。

**停止语义与 ADR-3 的呼应**：`stop()` 只保证「不再产生新 work item」（§5.9.5 lane `close()` 的同义要求）。未收束的在途项**不**主动复位，交给下次启动的清扫（ADR-3）——这与 C3 `TenantLaneRouter.close()` 与 §5.9.6 的语义一致，也避免停机时双写。

**不做**：本 change 不接线 `OutboundDeliveryWorker`，但**同一 engine 接线方式**应在其单独 change 中复用，避免两次发明。

---

## ADR-8 §7.1 可观测记录点（并入 C12 §8.1 / E10）

**结论**：本 change 作为 `background_work_items` 的落地方，**同时承担 C12「伴随落地协议」对本 canonical store 的指标与事件记录点**，按 `tests/fixtures/observability_event_schema.json` 落字段：

- 记录点：`claim`（`enqueued_at`→`started_at` 的 `queue_wait_ms`）、`finish`（`execution_ms`、`status`）、`recovery`（清扫时的 `restart_to_recovered_ms`、`recovery_action`）。
- 身份与归属：`work_id`、`tenant_id`、`work_kind`、`flow`、`attempt`、`session_key`（有则记）。
- 禁止内容字段：不记 `payload_json` 原文、不记 handler 输入输出原文（fixture 的 `forbidden_content_fields`）。
- 自由文本（`last_error` 等）入库前过 `core/telemetry/redaction.redact_text`。
- label 白名单：只允许 `core/telemetry/label_policy.py` 已登记的维度（`work_kind`/`flow`/`channel`/`model` 等），**不**以 `work_id`/`tenant_id` 作 metric label（高基数）。

**理由**：C12 契约已 verified 但 §8.1 因 C2/C3 合入时未按其 checklist 落地而成为**无 owner 的滞留项**（见 `c12-observability-backup/tasks.md` §8 与 checklist 清点结论）。把该条目并入本 change 是让它归属明确的最短路径；同时也是本 change 自身「崩溃恢复可观测」的验收手段（恢复演练需要 `restart_to_recovered_ms` 才有据可查）。

---

## Rollout / Rollback

- **Rollout**：§5.9.9 首次 Create → Verify → Enable 已由 C1/C2 完成；本 change 属后续 PostgreSQL schema evolution，走 **expand-only**：迁移与代码可分开上线——先上迁移（新列 + 索引，旧代码不受影响，DEFAULT 保证插入兼容），再上 worker 接线。
- **Enable 前置**：worker 默认由 `config.toml` 开关控制（新节 `[work_queue] enabled`，Pilot 默认值随实现冻结）；关掉即回到「表只写不读」，与当前生产行为等价。
- **Rollback**：关开关 → 停 worker（进程重启即生效）→ `alembic downgrade` 反向迁移（drop 5 列 + 索引）。已 `succeeded`/`failed` 的 work 行**保留**（不删审计），仅失去 lease 列。
- **数据影响**：不触碰 canonical message/turn/outbox 任何表；`background_work_items` 既有行的新列取 DEFAULT/NULL。

## 风险与待冻结决策

| 项 | 处置 |
| --- | --- |
| 排队失租导致重复执行 | ADR-4 从 SQL 形状上排除（每租户每轮 ≤1）；`test_work_queue_tenant_lane.py` 需有专门用例断言「同租户两条 work 不会同时在途」 |
| 崩溃被误判为业务失败 → 退避耗尽 `failed` | ADR-3 明确清扫**不**递增 `attempt_count`；需负向用例 |
| 停机留下「副作用已写、状态未推进」 | ADR-7 规定 stop 不取消在途 handler；需用例断言 stop 后无 `in_progress` 残留（除被强制中断场景，留给清扫） |
| handler 非幂等导致重放副作用 | ADR-6 只提供「同事务」接缝，**不**自动判定；非幂等 handler 的注册属各 feature change 的验收责任，本 change 在 handler 协议文档中显式声明该要求 |
| 首次有 durable worker 常驻，可能掩盖既有内存路径问题 | 本 change 不迁移内存路径（Non-Goals）；两者并存期间以 `[work_queue] enabled` 控制影响面 |
| `TenantLaneRouter` 首次生产使用可能暴露未测试路径 | ADR-5 已列其在 C3 的覆盖范围；需在本 change 补「worker 场景」的用例（延后/取消/关闭） |
