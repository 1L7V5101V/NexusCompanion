# webchat-durable-storage Specification

## Purpose

定义 WebChat 通道接入 PostgreSQL durable source of truth 后的行为契约：durable 接受与重启存续幂等、canonical 流为会话内容 source of truth、执行完成事务与终态、final 投递经 delivery 状态机、durable 重放与 REST 重建、启动对账与恢复、E10 生命周期记录点（内容边界与脱敏）。事务/状态机原语由 durable-control-plane 承载，序号原子分配由 canonical-identity 承载；单机 dev（无 PG）回退路径不在本规格范围。

## Requirements

### Requirement: durable 接受与重启存续幂等

对通过入口校验与 overload 门禁的 WebChat 入站消息，系统 SHALL 在入站接受事务提交后才向客户端返回 `message.accepted`；同一账号重复提交相同 `client_message_id` SHALL 重放首次的 accepted 帧（含同一 `seq`）且 SHALL NOT 产生第二条 canonical 消息、inbox 记录或 turn；该幂等 SHALL 在进程重启后存续。接受事务失败时 SHALL 返回结构化错误帧且不缓存幂等结果（客户端可重发）。

#### Scenario: 提交后才返回 accepted

- **WHEN** 客户端发送合法 `send` 帧且接受事务成功提交
- **THEN** 客户端收到的 `message.accepted` 对应一条已提交的 canonical user 消息、inbox 记录与 queued turn，四者同事务产生

#### Scenario: 重启后重复提交重放原 ack

- **WHEN** 服务进程重启后，同一账号以重启前已接受过的 `client_message_id` 再次提交
- **THEN** 服务重放首次 accepted 的同一 `seq`，canonical 消息、inbox 与 turn 计数不变

#### Scenario: 接受事务失败可重发

- **WHEN** 接受事务中途失败（如约束违反）
- **THEN** 客户端收到结构化错误帧，所有相关表零写入，后续以同一 `client_message_id` 重发可正常接受

### Requirement: canonical 流为会话内容 source of truth

WebChat 会话的用户消息与最终助手回复 SHALL 以 canonical message stream 为准（0-based、per-conversation 连续序号）；会话内容的任何重建、补拉或对账 SHALL 以 canonical 流为权威来源，派生视图（session view）SHALL 可从 canonical 流重建，派生视图与 canonical 流的分歧 SHALL 以 canonical 流为准被修复。

#### Scenario: 重建结果与实时会话一致

- **WHEN** 客户端经 REST 从 canonical 流重建某会话的消息
- **THEN** 重建结果包含该会话全部已提交的 user 消息与成功 turn 的 final assistant 消息，序号连续且与实时推送内容一致

#### Scenario: 失败 turn 不产生 final 消息

- **WHEN** 某 turn 以失败/取消终态收束
- **THEN** canonical 流中不存在该 turn 的 final assistant 消息，派生视图同样不出现

### Requirement: 执行完成事务与终态

turn 成功完成时，系统 SHALL 在单个事务内原子写入 final assistant 消息、turn 终态（引用 final 消息）与 pending 的出站投递意图；turn 的失败/取消/中断终态 SHALL 记录终态原因且 SHALL NOT 创建投递意图；事务失败 SHALL 整体回滚且 turn 保持原状态。

#### Scenario: 成功完成原子收束

- **WHEN** 一个 WebChat turn 的模型生成成功完成
- **THEN** final assistant 消息（序号连续分配）、turn completed 终态与 pending 投递意图同事务产生

#### Scenario: 失败终态无投递意图

- **WHEN** turn 执行中出错并以 failed 终态收束
- **THEN** 投递意图不存在，客户端仍可收到 `turn.failed` 终态帧

### Requirement: final 投递经 delivery 状态机

成功 turn 的 final 帧投递 SHALL 由 delivery worker 以数据库 lease 认领投递意图后执行；`sent` SHALL 仅由 WebChat 发送成功确认推进；发送失败 SHALL 按冻结退避参数重试直至 `dead_letter`，且 SHALL NOT 删除或改写 final canonical 消息；服务重启后未确认投递意图 SHALL 继续被认领重试。流式 delta 与 tool 状态帧 SHALL NOT 进入投递状态机（仍为在线连接的即时优化）。

#### Scenario: 在线投递推进 sent

- **WHEN** 会话存在在线连接且 delivery worker 认领到该 turn 的投递意图
- **THEN** final 帧写入在线连接，发送成功后投递意图推进为 `sent` 并记录 attempt

#### Scenario: 无在线连接时重试

- **WHEN** final 帧投递时该会话无任何在线连接
- **THEN** 本次 attempt 记录失败并按退避重试，final 消息不受影响，客户端重连后可经补拉/REST 重建获得内容

#### Scenario: 重启后继续投递

- **WHEN** 服务重启时有处于 pending/attempting 的投递意图
- **THEN** 重启后 delivery worker 继续认领并投递，不重新生成 final 消息

### Requirement: durable 重放与 REST 重建

replayable 帧（`message.accepted`/`turn.completed`/`turn.failed`）的 `seq` 分配与帧记录 SHALL 持久化并随对应事务提交，重连补拉 SHALL 从持久层服务且在进程重启后存续；补拉结果 SHALL NOT 产生重复或乱序帧；请求游标超出持久窗口或数据不可用时 SHALL 回 `replay_required`，由客户端经 REST 从 canonical 流重建。delta/tool 帧不分配 `seq`、不参与补拉。

#### Scenario: 重启后按游标补拉

- **WHEN** 客户端断线期间服务重启，重连后以断线前最后 `seq` 请求补拉
- **THEN** 服务从持久层返回其后全部 replayable 帧，序号连续无重复

#### Scenario: 游标不可用指引 REST 重建

- **WHEN** 请求游标早于持久窗口内最早 `seq`（或对应数据已被 retention 清理）
- **THEN** 服务回 `replay_required`，不静默补发部分帧

### Requirement: 启动对账与恢复

服务启动 SHALL 对账非终态 turn：中断的执行 SHALL 收束为显式失败终态并记录原因，SHALL NOT 重新生成 final 消息、SHALL NOT 创建投递意图；启动对账 SHALL 修复派生视图与 canonical 流的分歧（以 canonical 流为准）；对账结果 SHALL 对管理员可见（日志/指标），SHALL NOT 静默丢弃记录。

#### Scenario: 中断 turn 对账为失败

- **WHEN** 服务在 turn 执行中重启，启动扫描发现该 turn 处于非终态
- **THEN** 该 turn 被收束为 failed 终态（含原因），无投递意图产生，canonical 流无该 turn 的 final 消息

#### Scenario: 派生视图分歧被修复

- **WHEN** 派生视图缺少 canonical 流中已提交的消息（如投影写入失败后重启）
- **THEN** 启动对账以 canonical 流为准补齐派生视图，不产生重复消息

### Requirement: E10 生命周期记录点（C12 §8.1 承接）

`turn`/`tool_call`/`delivery` 三个生命周期 SHALL 按 C12 事件 schema fixture 的字段白名单记录事件与指标；事件 SHALL 以白名单构造（内容字段在结构上不可进入），自由文本 SHALL 经脱敏后入库，metric label SHALL 限于已注册白名单维度；记录点失败 SHALL NOT 阻断对应主流程。

#### Scenario: 事件字段不超过 fixture 白名单

- **WHEN** 任一记录点产出事件
- **THEN** 事件字段集合为 fixture 声明字段的子集，含内容字段的事件被构造逻辑拒绝

#### Scenario: 记录点失败不阻断主流程

- **WHEN** 指标/事件写入异常（如存储不可用）
- **THEN** 对应 turn/tool/delivery 主流程继续执行，异常仅记录日志
