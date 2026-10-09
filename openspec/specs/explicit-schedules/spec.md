# explicit-schedules Specification

## Purpose

定义显式用户 schedule（reminder/cron）作为 tenant/account/conversation owned durable
business work 的行为契约：PG durable 存储、服务端解析冻结的 delivery binding、
`(job_id, scheduled_for)` 幂等执行、one-shot misfire grace、recurring 不回放恢复语义、
账号 suspended/revoked 状态联动、重启恢复、经 outbox intent 投递、admin misfire 查看、
IANA 时区与 DST contract。依据 PILOT_ROADMAP §5.9.14 全节、§5.9.6（durable control
plane 必收对象）、§10 DECIDED（Explicit schedule 语义）与 §10 PROPOSED DEFAULT
（misfire grace 5 分钟，本 change 保留默认值）冻结。

边界：proactive/drift/optimizer tick 是可重算 runtime tick，其恢复语义（不回放、
只重算下次调度）不在本 capability 内；本 capability 只要求两者恢复语义不共享实现
与存储，且本 capability 的实现不触碰 tick 本体。

## Requirements

### Requirement: schedule 的 ownership 与服务端 delivery binding

显式用户 schedule SHALL 持久化 `tenant_id`、`account_id` 与 canonical
`conversation_id`（三者在创建时由服务端派生，缺一 fail-closed 拒绝创建）；delivery
目标 SHALL 由服务端在创建时解析并冻结到 job 行（`delivery_channel` +
`delivery_target`），模型/客户端提交的任意 `channel`/`chat_id` SHALL NOT 成为授权
目标（user principal 一律取服务端注入的上下文目标；冻结值不可被后续工具调用覆盖）。
解析失败（无 canonical conversation、无可用投递路由）SHALL 拒绝创建。

#### Scenario: 模型/客户端不能指定任意推送目标

- **WHEN** user principal 调用 schedule 创建工具并在参数中携带任意的
  `channel`/`chat_id`（如其他租户的会话 ID）
- **THEN** 这些参数被剥离，job 行冻结的 delivery binding 是服务端从当前可信上下文
  解析出的目标，而非模型提交值（负向测试固化）

#### Scenario: 无可解析会话时 fail-closed

- **WHEN** 创建 schedule 时服务端无法派生 canonical conversation（tenant 不存在会话行）
- **THEN** 创建被拒绝并返回机器可读错误码，不落任何 job 行

### Requirement: durable schedule/execution 存储

schedule 与执行记录 SHALL 持久化到 PostgreSQL（`scheduled_jobs` /
`schedule_executions`，PostgreSQL 后端时为 canonical 存储；旧全局 `schedules.json`
在 PG 后端 SHALL NOT 被读取，仅作为非 PG dev 路径的兼容存储）。job 行 SHALL 保存
trigger/tier 规格与 IANA 时区名称及计划版本（`revision`，每次语义变更递增）；
execution 行 SHALL 持久化 attempt、terminal outcome、skip 原因、触发时使用的时区与
计划版本，以及产出的 delivery intent/message 引用。

#### Scenario: 创建即 durable

- **WHEN** 一次 schedule 创建成功
- **THEN** `scheduled_jobs` 出现对应行（含 owner 三元组、冻结 binding、IANA 时区、
  revision=1），进程立即重启后该 job 仍可被恢复执行

### Requirement: `(job_id, scheduled_for)` 幂等执行

每次计划执行 SHALL 以 `(job_id, scheduled_for)`（名义触发时刻，UTC）为幂等键唯一
约束；同一键的重复触发（并发 tick、重放、恢复扫描）SHALL NOT 产生第二次副作用
（第二次起直接放弃，不重复写 message/intent）。

#### Scenario: 重复执行无第二次副作用

- **WHEN** 同一 `(job_id, scheduled_for)` 的执行被触发两次（如两个 tick 竞争或恢复
  扫描与 tick 同时认领）
- **THEN** 恰好产生一次 execution 行与一次投递副作用，第二次触发不创建任何新记录

### Requirement: one-shot misfire（不静默删除）

one-shot job 的名义触发时刻已过且超过 misfire grace（默认 5 分钟，可配置）SHALL
产生一条 `missed` execution 记录（含原因），job SHALL NOT 被静默删除或无痕丢弃；
missed 执行 SHALL 可由管理员查询。grace 内的过期 one-shot SHALL 正常执行。

#### Scenario: 超过 grace 标记 missed 且 admin 可查

- **WHEN** one-shot job 的 fire_at 早于当前时间超过 grace（如进程停机 10 分钟后恢复）
- **THEN** 产生 `missed` execution 行（原因含超时语义），job 不再调度，管理员经
  admin 查询面能看到该 missed 记录；无任何推送副作用发生

#### Scenario: grace 内仍执行

- **WHEN** one-shot job 的 fire_at 早于当前时间但在 grace 之内
- **THEN** 该次执行正常发生，execution 以实际触发收束（不标 missed）

### Requirement: recurring 不回放全部 missed

recurring job 恢复/追赶时 SHALL NOT 回放全部错过的 occurrence，只计算并调度下一
未来 occurrence；被跳过的边界 SHALL 记录（`skipped` execution 行 + 原因），不留
无声空洞。

#### Scenario: 重启跨多个 occurrence 只前进一次

- **WHEN** 每 10 分钟一次的 recurring job 停机 1 小时后恢复
- **THEN** 不产生 6 次补执行；产生至多一条 `skipped` 记录（原因表明 misfire 前进），
  调度前进到下一个未来 occurrence

### Requirement: suspended/revoked 状态联动

账号 `suspended` 期间 SHALL 暂停新执行（不产生逐 tick 的 skip 记录刷屏）；恢复
active 后从下一未来 occurrence 按正常 misfire 规则继续。账号 `revoked` SHALL 使其
schedule 禁用（job 置 `revoked`，不再产生任何执行）。管理员 SHALL 能对单个 job
执行 suspend/resume/revoke。

#### Scenario: suspended 期间不执行、恢复后前进

- **WHEN** 账号被 suspend 后其 recurring job 的触发时刻过去，随后账号恢复 active
- **THEN** suspended 期间无执行、无 skip 记录刷屏；恢复后该 job 按 misfire 规则
  跳到下一未来 occurrence（跨 grace 时记录 skip）

#### Scenario: revoked 禁用

- **WHEN** 账号被 revoke（或管理员对 job 执行 revoke）
- **THEN** 该账号的 schedule 被置为 `revoked`，不再产生任何执行或投递

### Requirement: 重启恢复可解释

进程重启后 SHALL 从 durable schedule/execution 恢复：以 `(job_id, scheduled_for)`
去重保证不重复执行；中断时处于 `running` 的 execution SHALL 被恢复扫描标记 `failed`
（原因表明进程中断）且 SHALL NOT 重放其副作用（宁可漏一次、不重复推送）。

#### Scenario: running 中断不重放副作用

- **WHEN** soft 执行进行中进程崩溃，重启后恢复扫描运行
- **THEN** 该 execution 被标 `failed`（原因 `interrupted_by_restart` 类语义），不产生
  重复投递；recurring job 的调度前进到下一未来 occurrence

### Requirement: delivery 经 outbox intent

schedule 触发的投递 SHALL 复用 C2 outbox intent 模式（`outbound_delivery_intents`
行，幂等键 `sched:<execution_id>`），投递结果由既有 delivery 状态机独立收束；
schedule execution 的 terminal outcome 表达「触发/产出投递意图」的收束，不等待
provider ack。

#### Scenario: 执行产出投递意图

- **WHEN** 一次 instant 执行成功触发
- **THEN** 同事务产生 canonical assistant message 与 pending delivery intent，
  delivery worker 按既有状态机投递；投递失败不回改 execution 终态

### Requirement: admin misfire 查看

管理员 SHALL 能查询 schedule jobs（按状态过滤）与 executions（按 `missed`/
`skipped`/终态过滤），并能对 job 执行 suspend/resume/revoke；该查询面 SHALL 仅限
admin principal。

#### Scenario: admin 查看 missed

- **WHEN** admin 请求 executions 列表并按 `missed` 过滤
- **THEN** 返回全部 missed 执行（含 job、名义触发时刻、原因）；非 admin principal
  得到拒绝

### Requirement: IANA 时区与 DST contract

schedule 时区 SHALL 使用 IANA 名称；cron/at 语义 SHALL 按时区本地时间解析，interval
语义按绝对时间推进；DST 跳时（本地不存在的时刻）与重复时刻 SHALL 有固化 contract
test（spring-forward 缺失时刻前进到下一有效本地时刻；fall-back 重复时刻取首次
出现，不双触发）。

#### Scenario: DST 跳时 contract

- **WHEN** 时区（如 America/New_York）进入 spring-forward，cron 指定的本地时刻不存在
- **THEN** 计算出的下一触发时刻是该时区语义下的下一个有效时刻，且该行为由 contract
  test 固化（fall-back 重复时刻同理：单次触发）
