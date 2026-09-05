# durable-control-plane Specification

## Purpose

定义 Pilot durable control plane 的行为契约：入站接受事务（inbox/去重/规范消息/queued turn 原子提交）、入站幂等双键去重、执行完成事务（final message/turn 终态/outbox intent 原子提交）、outbox delivery 状态机（lease 认领、attempt 记录、`sent` 仅由 ack 推进）、重启重放只补投递不重新生成 final、dead-letter 人工处置，以及 inbox 收束（`complete_inbound` 等价）语义。旧单体进程内队列路径不在本规格范围（独立 legacy 路径）。

## Requirements

### Requirement: 入站接受事务原子性

对一条已通过入口校验的入站消息，系统 SHALL 在单个 PostgreSQL 事务内原子写入去重键记录、规范 user 消息、inbox 记录与 queued turn（及可选的 queued 后台工作项）；事务提交前 SHALL NOT 向入口返回接受成功；事务中任一步失败 SHALL 整体回滚且不留任何半写入。

#### Scenario: 接受成功全部同提交

- **WHEN** 以合法的租户、规范会话与任一类幂等键请求接受一条入站消息
- **THEN** 去重键、规范 user 消息、inbox 记录（accepted 状态）与 queued turn 同事务产生，规范消息序号按会话序号连续分配

#### Scenario: 事务中途失败无半写入

- **WHEN** 接受事务中的某一步（如后台工作项违反约束）失败
- **THEN** 该次调用的去重键、规范消息、inbox 记录与 turn 全部不存在，会话序号计数器保持原值，下一次接受可重用同一序号

#### Scenario: 未知规范会话拒绝且零写入

- **WHEN** 对不存在的规范会话请求接受入站消息
- **THEN** 操作失败且所有相关表零写入，错误结果中不出现默认租户标识

### Requirement: 入站幂等双键去重

系统 SHALL 以两类强制幂等键对入站消息去重：Telegram 侧为 source channel + source identity + source message id，WebChat 侧为账号 + 客户端消息 id；唯一性 SHALL 由数据库部分唯一索引强制，重复注入 SHALL NOT 产生第二条规范消息或第二条 inbox 记录，SHALL 返回既有身份供调用方走幂等成功路径。

#### Scenario: 同客户端消息 id 重复注入不产生第二条

- **WHEN** 同一账号以相同客户端消息 id 两次请求接受（内容可不同）
- **THEN** 第二次调用返回重复标记与第一次的既有身份，规范消息与 inbox 记录各只有一条

#### Scenario: 同 Telegram source identity 加 source message id 重复注入不产生第二条

- **WHEN** 同一 source channel 与 source identity 以相同 source message id 两次请求接受
- **THEN** 第二次调用返回重复标记与第一次的既有身份，规范消息与 inbox 记录各只有一条

#### Scenario: 两类键并存互不干扰

- **WHEN** 一条消息携带 Telegram 键、另一条携带 WebChat 键先后接受
- **THEN** 两条各自成功，互不被对方键约束拒绝

#### Scenario: 并发重复注入只产生一条

- **WHEN** 多个并发调用以相同幂等键同时请求接受
- **THEN** 恰好一次接受成功，其余调用返回重复标记，最终规范消息与 inbox 记录各只有一条

### Requirement: inbox 收束语义

inbox 记录 SHALL 具有 `accepted → processed` 的收束状态：processed 表示该消息的持久化接受与终止处理已收束（对应旧单体 `complete_inbound` 的等价语义），SHALL NOT 表示 channel 已成功展示；收束操作 SHALL 幂等。

#### Scenario: 收束后重复收束成功

- **WHEN** 对已 processed 的 inbox 记录再次请求收束
- **THEN** 操作成功且不产生第二条记录或状态变化

#### Scenario: processed 不代表已送达

- **WHEN** inbox 记录已 processed 而对应投递意图尚未被 channel 确认
- **THEN** 投递状态与其 attempt 记录保持独立可查，不受收束影响

### Requirement: 执行完成事务原子性

对一个 turn 的成功完成，系统 SHALL 在单个 PostgreSQL 事务内原子写入 final assistant 规范消息、turn 终态（含对 final 消息的引用）与 pending 的出站投递意图；事务中任一步失败 SHALL 整体回滚；turn 的失败/取消/中断终态 SHALL NOT 创建出站投递意图。

#### Scenario: 完成成功全部同提交

- **WHEN** 请求完成一个 queued 或进行中的 turn 并附带投递目标
- **THEN** final assistant 消息（序号连续分配）、turn 终态（completed，引用 final 消息）与 pending 投递意图同事务产生

#### Scenario: 完成事务失败无半写入

- **WHEN** 完成事务中的某一步失败（如投递意图违反唯一约束）
- **THEN** final 消息、turn 终态与投递意图全部不存在，turn 保持原状态，会话序号计数器保持原值

#### Scenario: 失败终态不创建投递意图

- **WHEN** 将一个进行中的 turn 推进为 failed 终态
- **THEN** turn 到达 failed 终态且不产生任何出站投递意图

### Requirement: delivery 状态机与 lease 认领

出站投递 SHALL 由独立的 delivery worker 以数据库 lease 认领执行，状态机为 `pending / attempting / sent / failed / dead_letter`：认领把 intent 置为 attempting 并记录租约属主与到期时间；`sent` SHALL 只能由 channel/provider 的成功确认（或明确成功结果）推进；单次投递尝试 SHALL 独立记录 attempt、provider receipt、错误与时间戳；失败按退避排程重试，超过最大尝试次数 SHALL 进入 dead_letter；租约过期的 attempting intent SHALL 可被其他扫描器接管。

#### Scenario: 全路径到 sent

- **WHEN** 对一条 pending intent 执行认领、发送并收到成功确认
- **THEN** intent 到达 sent 终态并记录 sent 时间，attempt 记录含成功结果与 provider receipt

#### Scenario: 无 ack 永不 sent

- **WHEN** 发送回调未返回成功（抛错或报告失败）
- **THEN** intent 不进入 sent 状态；失败后进入 failed（或达到上限时 dead_letter），attempt 记录保留错误详情

#### Scenario: 失败退避重试

- **WHEN** 一次投递尝试失败且未达到最大尝试次数
- **THEN** intent 进入 failed 且下一次尝试时间按退避序列排程，租约被释放，到期后可再次被认领

#### Scenario: 超过最大尝试次数进入 dead_letter

- **WHEN** 连续投递失败达到最大尝试次数（默认 5）
- **THEN** intent 进入 dead_letter 终态，全部 attempt 记录与最后错误可查

#### Scenario: stale lease 被接管

- **WHEN** 一个 attempting intent 的租约到期且未被心跳续期
- **THEN** 另一扫描器可以认领该 intent 并开始新的尝试

#### Scenario: 失去租约不得写 sent

- **WHEN** 一个 worker 的心跳续期返回租约已失（他人已接管）
- **THEN** 该 worker 对本次尝试不得推进 sent 状态

### Requirement: 重启重放只补投递不重新生成 final

进程重启后，系统 SHALL 只对未确认送达的投递意图继续补投；SHALL NOT 重新生成或复制 final assistant 规范消息；已到达 sent 终态的意图 SHALL NOT 被再次投递。

#### Scenario: 重启后补投且 final 不变

- **WHEN** 执行完成事务已提交（final 消息与 pending intent 已存在）后模拟进程重启并重新运行 delivery worker
- **THEN** intent 被认领并完成投递，该 turn 的 final assistant 消息始终只有一条

#### Scenario: 已 sent 的意图重启后不再投递

- **WHEN** 重启后 worker 扫描时某 intent 已是 sent 终态
- **THEN** 该 intent 不被认领，无新的 attempt 记录

### Requirement: dead-letter 人工处置

对 dead_letter 的投递意图，系统 SHALL 提供管理员重新投递（redrive）能力：redrive SHALL 以追加记录的方式保留原因，原有 attempt 记录、provider receipt 与时间戳 SHALL NOT 被删除或改写；redrive SHALL 重置本周期尝试计数并使 intent 重新可被认领；对非 dead_letter 状态的 redrive 请求 SHALL 被拒绝。

#### Scenario: redrive 保留历史并复位重投

- **WHEN** 管理员对一个 dead_letter intent 请求 redrive 并附原因
- **THEN** 追加一条带原因的处置记录，原 attempt 记录不变，intent 回到 pending 并可被认领重新投递

#### Scenario: 非 dead_letter 状态拒绝 redrive

- **WHEN** 管理员对一个 sent 或 pending 状态的 intent 请求 redrive
- **THEN** 请求被拒绝且不产生任何处置记录或状态变化

### Requirement: 模型完成与已送达可观测分离

「模型生成完成」与「channel 已送达」SHALL 是相互独立可查的记录：final 消息与 turn 终态的持久化 SHALL NOT 依赖投递结果；投递失败时 final 消息 SHALL 仍完整可按序补拉（含按序号游标），且 delivery 侧 SHALL 保留可供管理员查询的意图、attempt、receipt、错误与时间戳。

#### Scenario: 投递失败时 final 消息仍可补拉

- **WHEN** 一个已完成的 turn 其投递意图进入 failed 或 dead_letter
- **THEN** 该 turn 的 final assistant 消息可从规范消息流按序读回，内容与完成事务提交时一致

#### Scenario: 投递过程独立可查

- **WHEN** 查询一个已 sent 或 dead_letter 的投递意图
- **THEN** 能分别看到意图状态、每次尝试的结果、provider receipt、错误与起止时间，且这些记录不在规范消息表内

### Requirement: control plane 查询租户隔离

control plane 的查询 SHALL 以可信租户上下文过滤；跨租户查询其它租户的 inbox、turn、工作项、投递意图或 attempt SHALL 返回空或失败，SHALL NOT 泄露存在性或内容；相关模块 SHALL NOT 包含旧单体存储回退或默认租户回退路径。

#### Scenario: 跨租户查询不可见

- **WHEN** 以租户 X 的上下文查询租户 Y 的 turn 或投递意图
- **THEN** 返回为空或失败，不泄露租户 Y 的任何行

#### Scenario: 无单体存储回退

- **WHEN** 静态检查 control plane 模型、仓储与 worker 模块
- **THEN** 不存在旧单体 SQLite 引用与默认租户常量引用
