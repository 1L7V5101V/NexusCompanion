# C11 Explicit Schedules — Design

## 目标与边界

将显式用户 schedule（reminder/cron）从全局 `schedules.json` + 内存 task 迁移为
tenant/account/conversation owned durable business work（§5.9.14），恢复语义与
proactive/optimizer tick 严格分离。本 change 不触碰 proactive/optimizer tick 实现本体
（`proactive_v2/modules_schedule.py`、work queue `proactive/optimizer` flow 的 handler），
只新增独立的 schedule 执行面。

冻结决策引用：

- §5.9.14 全节（ownership 三元组、服务端 binding、`(job_id, scheduled_for)` 幂等、
  execution attempt/terminal outcome/delivery intent/时区/计划版本持久化、
  suspend/revoke 联动、IANA 时区 + DST contract test）。
- §10 DECIDED「Explicit schedule 语义」：tenant-owned durable business work；幂等键；
  suspend 暂停、revoke 禁用；recurring 不回放全部 missed occurrence。
- §10 PROPOSED DEFAULT「Schedule misfire」：one-shot grace 5 分钟——**本 change 保留
  5 分钟默认值**并做成可配置（`[scheduler].misfire_grace_seconds`）；无论数值如何，
  miss/attempt/outcome 一律持久化。
- task-11 独立性边界：本任务拥有 `scheduled_jobs`/`schedule_executions` 表、scheduler
  rework、execution recovery、admin misfire 查看；不触碰 C2 outbox 表 schema（只按
  intent 模式消费）；`schedules.json` 迁移后旧文件只作可重建派生物。

## ADR-1 — 双表 `scheduled_jobs` / `schedule_executions`（expand-only）

迁移 `e8b4c2a6d9f1_c11_scheduled_jobs.py`（`down_revision='c7e9a3f1b5d4'`，raw SQL
风格与 C2 一致）：

**`scheduled_jobs`**（一行 = 一个用户显式任务）：

- 身份/归属：`id UUID PK`、`tenant_id VARCHAR(64) NOT NULL`、
  `account_id UUID NOT NULL FK test_accounts(RESTRICT)`、
  `conversation_id UUID NOT NULL FK canonical_conversations(RESTRICT)`。
- 规格：`name VARCHAR(255) NULL`、`trigger_kind VARCHAR(16)`（`at|after|every`）、
  `tier VARCHAR(16)`（`instant|soft`）、`schedule_spec_json TEXT NOT NULL`
  （`when` 原串 + 解析后的 `cron_expr`/`interval_seconds`/`advance_minutes`）、
  `message TEXT NULL`、`prompt TEXT NULL`、`timezone VARCHAR(64) NOT NULL`（IANA）、
  `revision INTEGER NOT NULL DEFAULT 1`（计划版本，语义变更 +1）。
- 服务端冻结 binding：`delivery_channel VARCHAR(64) NOT NULL`、
  `delivery_target VARCHAR(255) NOT NULL`（创建时解析；后续不可变）。
- 调度态：`status VARCHAR(16)`（`active|suspended|revoked`，CHECK）、
  `next_scheduled_for TIMESTAMPTZ NULL`（下一未来 occurrence / one-shot 唯一一次；
  NULL = 无待执行）、`last_outcome VARCHAR(16) NULL`（冗余最近 execution 终态，查询友好）。
- 审计：`created_at/updated_at`。
- 索引：`ix_scheduled_jobs_due (status, next_scheduled_at)` 即
  `(status, next_scheduled_for)`（tick 认领）、`ix_scheduled_jobs_tenant_status`、
  `ix_scheduled_jobs_account_status`（revoke 批量联动）。

**`schedule_executions`**（一行 = 一次名义 occurrence 的执行记录，只追加不删）：

- `id UUID PK`、`job_id UUID NOT NULL FK scheduled_jobs(RESTRICT)`、
  `tenant_id VARCHAR(64) NOT NULL`、
  `scheduled_for TIMESTAMPTZ NOT NULL`（名义触发时刻，UTC）。
- 幂等：`UNIQUE (job_id, scheduled_for)`（`uq_schedule_executions_job_scheduled`）。
- 状态机：`status VARCHAR(16)`：`running → succeeded|failed`；`missed`/`skipped` 为
  终态直落（不经历 running）。CHECK 约束固化枚举。
- 留痕：`attempt_count INTEGER NOT NULL DEFAULT 0`、`skip_reason VARCHAR(64) NULL`
  （`misfire_grace_exceeded`/`recurring_advance`/`binding_inactive`/`revocation_gate` 等）、
  `error TEXT NULL`、`schedule_timezone VARCHAR(64) NOT NULL`、
  `schedule_revision INTEGER NOT NULL`（触发时快照，§5.9.14「时区/计划版本」）、
  `delivery_intent_id UUID NULL`（软引用 + FK `outbound_delivery_intents(RESTRICT)`）、
  `delivery_message_id UUID NULL`（同）、`started_at/finished_at/created_at`。
- 索引：`ix_schedule_executions_status_time (status, scheduled_for)`（misfire/恢复扫描、
  admin 过滤）、`ix_schedule_executions_tenant (tenant_id, created_at)`。

要点：

- delivery binding 冻结在 job 行、execution 行记录**触发时**的时区/revision 快照——
  即使 job 之后被改名/换时区，历史执行仍可解释（§5.9.14）。
- `delivery_intent_id`/`delivery_message_id` 用 FK RESTRICT：execution 留痕不随
  delivery 清理丢失（retention 批处理未来只清 message/intent 时需按 C12 策略另行处理，
  本 change 不做 retention）。
- 不加 `job_id + status` 部分唯一「只允许一个 running」约束：`(job_id, scheduled_for)`
  已保证单 occurrence 唯一，recurring 相邻 occurrence 天然不同键。

## ADR-2 — delivery binding 服务端解析（创建时冻结 + 触发时重验）

- **创建时**：durable 路径的创建参数由服务端组装——`tenant_id`/`account_id` 取
  `ToolExecutionContext`（服务端注入，模型不可覆盖，C7 既有机制）；
  `conversation_id` 经 `CanonicalIdentityRepository.get_conversation_by_tenant(tenant_id)`
  解析（Pilot 每 tenant 恰一个 canonical conversation，与 C10 telegram binding 同源）；
  `delivery_channel`/`delivery_target` 取 `ToolExecutionContext.channel/chat_id`
  ——对 user principal 该值即服务端注入的当前会话目标（WebChat native：
  `channel="chat"`、`target=<tenant_id>`，与 `WebchatDurableTurnFinisher` 约定一致；
  telegram 会话：`channel="telegram"`、`target=<telegram_chat_id>`）。模型提交的
  `channel/chat_id` 对 user principal 已被 registry 剥离（`ROUTING_ARGUMENT_FIELDS`），
  本 change 以负向测试固化「提交值 ≠ 冻结值」；dev/owner 的跨目标路由豁免沿用 C7
  语义不变（spec 范围 = user principal）。
- **触发时重验（fail-closed）**：执行前按顺序检查——① 账号状态（join
  `test_accounts.status`：`suspended` → 挂起不执行；`revoked` → job 置 `revoked`）；
  ② C7 `RevocationGate.check`（进程内封禁重校验，原样保留）；③ telegram 渠道的
  binding 仍 active（`TelegramBindingRepository.get_active_binding_by_identity`
  快速校验；不再 active → `skipped`，`skip_reason='binding_inactive'`）。任一不过
  即不产生投递副作用。
- 解析失败（无 canonical conversation / 上下文缺 channel）→ 创建拒绝
  （`code=schedule_no_delivery_binding`），不落 job 行。

## ADR-3 — 触发事务与幂等（claim → side effect → 收束）

tick 每秒扫描 `status='active' AND next_scheduled_for <= now` 的 job（
`FOR UPDATE SKIP LOCKED` 风格的单实例扫描即可，Pilot 单进程）：

1. **claim**（事务 1）：对到期 occurrence 插入 `schedule_executions`
   (`status='running'`, `scheduled_for=<到期值>`)——`(job_id, scheduled_for)` 唯一
   约束兜底并发/重放，`IntegrityError → 视为已认领，放弃`。同时：
   - one-shot 且 `now - scheduled_for > grace` → 直接落 `missed`
     （`skip_reason='misfire_grace_exceeded'`）并把 job `next_scheduled_for=NULL`，
     不产生副作用；
   - recurring 且超 grace → 落 `skipped`（`skip_reason='recurring_advance'`）并把
     `next_scheduled_for` 前进到 **now 之后的下一未来 occurrence**（不枚举中间
     次数），本轮不执行；
   - 账号 suspended → **不插 execution 行、不前进**（job 留在到期态，下个 tick
     重查；避免逐 tick skip 刷屏）；
   - 账号 revoked → job 批量置 `revoked`。
2. **side effect**（事务外）：
   - instant：内容 = `message`；
   - soft：`agent_loop.process_direct(...)`（既有参数：`session_key=scheduler:<job_id>`
     等；耗时 AI 调用，绝不持 DB 事务）。
3. **收束**（事务 2，单事务原子）：分配 `conversation.next_sequence` → 插
   canonical message（role=assistant，metadata 标 `scheduler`）→ 插
   `outbound_delivery_intents`（`idempotency_key='sched:<execution_id>'`，channel/
   target 取 job 冻结 binding，status=pending）→ execution 置 `succeeded`
   （AI 返回空内容则 `failed`，error 记录）→ job `last_outcome` 更新；
   recurring 同事务推进 `next_scheduled_for`（以 `max(now, scheduled_for)` 为基准
   算下一 occurrence，沿用 legacy 防重复边界语义）。sequence/message/intent/
   execution/job 五者原子；收束失败 → execution `failed`（error 记录），消息与
   intent 不落。

幂等保证：副作用入口是 execution 行的插入，唯一约束使同一 occurrence 的重复触发
（双 tick、恢复扫描竞态）最多一次进入 side effect；投递层幂等由
`uq_outbound_delivery_intents_idempotency_key`（`sched:<execution_id>` 一对一）兜底。

## ADR-4 — `DurableSchedulerService` 与 legacy 并存（后端门控）

- 新 `bootstrap/schedule_durable.py::DurableSchedulerService`：`run()/stop()` 接口与
  legacy `SchedulerService` 一致（`AppRuntime.start` 无差别加入 tasks），内部 = 启动
  恢复扫描 + 1s tick 循环。自带独立 engine/session_factory（与 work queue runtime
  同模式，`async_pg_url(config.storage.postgres_url)`，池独立）。
- **启动恢复扫描**（ADR-3 语义的崩溃面）：所有 `running` execution → `failed`
  （`error='interrupted_by_restart'`），不重放副作用；其 job 若 recurring →
  `next_scheduled_for` 前进到下一未来 occurrence，one-shot → `next_scheduled_for=NULL`
  （该次已终态，job 无待执行）；随后对 `next_scheduled_for <= now` 的到期态走正常
  tick 规则（grace 内执行 / 超 grace missed/skipped）。`(job_id, scheduled_for)` 去重
  由唯一约束与「running 不重放」共同保证。
- **工具面 Protocol**：`agent/scheduler.py` 新增 `ScheduleManager` Protocol（async
  `create_job`/`list_jobs`/`cancel_jobs`/`suspend_job`/`resume_job`），legacy
  `SchedulerService` 增加对应 async 包装（内部仍 JSON store），durable 服务直接实现。
  工具按 Protocol 面向两个实现。legacy `add_job`/`cancel_job` 等同步 API 原样保留
  （既有测试不回归）。
- **装配门控**：`build_scheduler`（`bootstrap/toolsets/schedule.py`）在
  `config.storage.backend == "postgres"` 时构造 `DurableSchedulerService`
  （engine 独立、`misfire_grace_seconds` 来自配置），否则回退 legacy JSON 路径
  （dev 单用户，零改动）。与 `pg-durable-sot-cutover` 的后端门控同哲学。
- 非 PG 模式下 suspend/resume 工具语义退化：legacy 实现把 suspend 当 cancel
  同义处理（dev 路径无账号状态机），admin 端点仅在 PG 后端注册。

## ADR-5 — 工具面与 admin 面

- `ScheduleTool`/`RemindTool`：参数 schema 保留 `channel/chat_id`（dev/owner 合法
  路由豁免），durable 创建路径**只信服务端组装的 binding**（ADR-2）；返回串附
  execution 语义不变（首次触发时间）。新增工具 `suspend_schedule`/`resume_schedule`
  （按 id/name，租户隔离同 cancel）。
- `ListSchedulesTool`/`CancelScheduleTool`：面向 Protocol，durable 实现按
  `tenant_id` 隔离查询（user principal 只见本租户；dev/owner 全量，沿用 C7 哲学）。
- **admin misfire 查看面**（`bootstrap/auth/api.py`，挂 `/api/admin/*`，仅
  `principal_type == "admin"`，沿用 telegram-bindings 端点门控模式）：
  - `GET /api/admin/schedules?status=`：jobs 列表；
  - `GET /api/admin/schedules/executions?status=missed|skipped|...`：executions 列表
    （默认全量倒序，limit 100）；
  - `POST /api/admin/schedules/{id}/suspend|resume|revoke`。
  端点直接读 `ScheduleRepository`，不经过 agent 进程内状态（PG 即共享态）。

## ADR-6 — 时区与 DST contract

- cron/at：按时区本地时间解析（`next_cron_fire` 走 APScheduler `CronTrigger` +
  ZoneInfo/pytz；`compute_fire_at` 同）；interval（`every '1h'`）：绝对时间推进
  （`timedelta`，不随 DST 漂移）——legacy 行为原样保留并写入 contract test。
- Contract test 固化（`America/New_York` 2026 年边界）：
  - spring-forward（2026-03-08，本地 02:00→03:00）：指定不存在的本地 02:30 →
    下一有效时刻（03:00 本地）单次触发；
  - fall-back（2026-11-01，本地 01:00-01:59 重复）：指定 01:30 → 取首次出现
    （fold=0），不双触发；
  - UTC 无 DST 时区（Asia/Shanghai）作对照。
- 时区名非法（非 IANA）→ 创建拒绝（既有 `ZoneInfo` 校验保留）。

## ADR-7 — `schedules.json` 的派生物地位

PG 后端下 scheduler **不读取** `schedules.json`（不自动迁移存量 JSON 任务——旧
owner 模型只有 channel/chat，无法可信映射到 account/conversation 三元组，猜测归属
违反 §5.9.14 fail-closed 精神）；文件仅非 PG dev 路径继续使用。`check_schedules.py`
改为按存储后端选择查询（PG 查询面 / JSON 兜底），供运维查看。该语义写入
§5.9.12「旧文件只作可重建派生物」的落地注记。

## 门禁与验收映射

| 验收（task-11） | 落点 |
| --- | --- |
| owner 校验负向 | ADR-2 + `tests/schedules/test_binding_freeze.py`（registry 剥离 + 冻结值断言 + 触发时 binding_inactive skip） |
| `(job_id, scheduled_for)` 幂等 | ADR-3 + 唯一约束 + 重复触发测试 |
| one-shot 超 grace → missed 不静默删除 | ADR-3 claim 分支 + admin executions 过滤测试 |
| recurring 不回放 | ADR-3/ADR-4 前进规则 + 跨多 occurrence 恢复测试 |
| IANA + DST contract | ADR-6 contract test |
| suspend/revoke 联动 | ADR-3 claim 挂起 + revoked 批量置态 + admin 处置测试 |
| 重启恢复可解释 | ADR-4 启动扫描（running→failed 不重放 + 去重）测试 |
| 与 tick 恢复语义分离 | ADR-边界（不触碰 `proactive_v2`/work queue flow）+ PR diff 检查 |
| 不触碰 tick 本体 | diff 范围：无 `proactive_v2/`、无 work queue flow handler 改动 |
