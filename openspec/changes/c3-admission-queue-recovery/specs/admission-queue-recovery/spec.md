# admission-queue-recovery 增量规格

## Purpose

定义 Pilot 运行时的 tenant-scoped admission（同 tenant 串行、跨 tenant 异步）、有界队列与 overload 语义、分类资源并发上限、interactive 与 maintenance 的调度优先级，以及启动恢复扫描的状态语义。容量数值为 §10 DECIDED 冻结的 Pilot 初始值，部署可配置但默认不允许无界。

## ADDED Requirements

### Requirement: tenant-scoped 串行 admission

系统 SHALL 以服务端派生的 `tenant_id` 作为 admission key：同一 tenant 的 work（interactive turn、maintenance）任意时刻最多一个在执行，且按提交顺序执行；不同 tenant 的 work SHALL NOT 因共享执行路径互相等待。admission key 解析 SHALL 在服务端边界完成，SHALL NOT 回退到默认租户标识。

#### Scenario: 同 tenant 的两条 turn 串行

- **WHEN** 向同一 tenant 连续提交两条 interactive turn，第一条执行中提交第二条
- **THEN** 第二条在前一条完全结束（含释放）后才开始执行，两条的执行时间窗无重叠

#### Scenario: 不同 tenant 异步执行

- **WHEN** tenant A 持有一条长时间执行的 turn，同时 tenant B 提交一条 turn
- **THEN** B 的 turn 在 A 执行期间即开始执行，B 的等待时间不包含 A 的执行时长

#### Scenario: 空 tenant 标识被拒绝

- **WHEN** 入站 work 携带空 tenant 标识且服务端无法从可信来源派生
- **THEN** 该 work 被拒绝进入 admission，不落入默认租户

### Requirement: interactive 有界队列与 overload

global interactive ingress 队列 SHALL 有界（默认 128）；每个 tenant 的 pending interactive 上限 SHALL 有界（默认 16，tenant 内 active work 仍为 1）。队列满载时系统 SHALL 以携带重试指引的类型化 overload 信号明确拒绝新 work，SHALL NOT 先接受后丢弃，SHALL NOT 无限阻塞入站处理。已接受的 work SHALL NOT 因后续 overload 被丢弃。

#### Scenario: 全局 interactive 队列满返回 overload 信号

- **WHEN** 全局 interactive 队列已有 128 个待处理 work，新 work 到达
- **THEN** 新 work 被拒绝并收到含重试间隔指引的 overload 信号，既有 128 个 work 不受影响

#### Scenario: tenant pending 满明确拒绝

- **WHEN** 某 tenant 已有 1 个 active work 与 16 个 pending work，第 17+ 条消息到达
- **THEN** 该消息被明确拒绝（收到拒绝反馈），该 tenant 的 pending 数量不继续增长，其他 tenant 不受影响

### Requirement: maintenance 有界、可合并、可延后

global maintenance ready 队列 SHALL 有界（默认 64）；每种 maintenance kind 在一个 tenant 内 SHALL 最多保留一个 pending 意图，重复意图 SHALL 被合并。maintenance 队列满时 SHALL 延后而不是拒绝用户消息；maintenance work SHALL 可从持久状态重算。interactive work SHALL 优先于同 tenant 的 maintenance work，新 interactive 到达时 SHALL NOT 被已排队的 maintenance 阻塞在队尾。

#### Scenario: 重复 maintenance 意图被合并

- **WHEN** 同一 tenant 的同一 maintenance kind 已有 pending 意图时再次提交同类意图
- **THEN** 系统不新增队列项，既有意图执行时按最新持久状态计算一次

#### Scenario: maintenance 满不拒绝用户消息

- **WHEN** 全局 maintenance 队列已达 64 个在途 work，新的用户消息到达
- **THEN** 用户消息正常被 interactive 路径接受，maintenance 意图延后保留

#### Scenario: interactive 优先于 maintenance

- **WHEN** 某 tenant 的 maintenance 排队等待，同一 tenant 有新 interactive turn 到达
- **THEN** interactive turn 先于排队中的 maintenance 执行

### Requirement: 分类资源并发上限

系统 SHALL 对 LLM 调用、embedding 调用、MCP 调用、进程执行四类资源分别施加异步许可上限（默认 30/4/8/2），四类计数相互独立：任一类达到上限 SHALL NOT 消耗其他类的许可额度。上限为可配置初始值。

#### Scenario: LLM 达到上限时 embedding 不受影响

- **WHEN** 同时有 30 个 LLM 调用在执行，第 31 个 LLM 调用与一个 embedding 调用同时发起
- **THEN** 第 31 个 LLM 调用等待许可，embedding 调用立即获得许可执行

#### Scenario: 各类许可独立释放

- **WHEN** 一批 MCP 调用全部完成
- **THEN** MCP 类可用许可恢复满额，LLM/embedding/process 类许可计数不受该批调用影响

### Requirement: WebSocket outbound 有界与分级降级

每条 WebSocket 连接的 outbound 队列 SHALL 有界（默认 hard 256）；出队深度达到 soft 阈值（默认 192）时系统 SHALL 丢弃非持久 delta 类帧并要求客户端按序号补拉；达到 hard 阈值或累计载荷上限（默认 1 MiB）时 SHALL 以明确的关闭码断开连接。规范的最终消息与终态帧 SHALL NOT 因连接队列满而被删除。

#### Scenario: soft 阈值丢弃 delta 要求补拉

- **WHEN** 某连接 outbound 积压达到 192 帧，新的流式 delta 帧产生
- **THEN** delta 帧被丢弃，连接收到要求按 `last_sequence` 补拉的信号，最终消息与终态不受影响

#### Scenario: hard 阈值断开但终态不丢

- **WHEN** 某 connection outbound 达到 256 帧或累计载荷超过 1 MiB
- **THEN** 连接以明确关闭码断开，重连后可按序号补拉规范终态帧

### Requirement: lane owner 统一释放与关闭语义

work 的执行所有权 SHALL 在成功、失败、取消、超时与异常路径上统一释放；owner 未释放前同 tenant 后续 work SHALL NOT 开始。系统进入关闭流程后 SHALL 拒绝产生新 work item，未完成项 SHALL 交给启动恢复扫描。

#### Scenario: 异常路径释放 owner

- **WHEN** 某 tenant 的 active work 抛出未捕获异常
- **THEN** 该 tenant 的 owner 被释放，后续 work 可立即开始，其他 tenant 全程不受影响

#### Scenario: 关闭后拒绝新 work

- **WHEN** 系统进入关闭流程后再有入站 work 提交
- **THEN** 提交被拒绝，不产生新的执行任务

### Requirement: 启动恢复扫描与非终态处置

系统启动时 SHALL 扫描持久化的非终态 work 记录，并逐条产出含 `recovery_started_at`、`recovery_finished_at`、`recovery_action`、`recovery_result`、原始 work id 与 attempt 的恢复记录。对可能已产生外部副作用的操作 SHALL 禁止无确认重放：结果不可判定时标记为 `unknown`（先查询结果再决定），确认有副作用且外部状态未恢复时标记为 `compensation_required`（先补偿再结束），SHALL NOT 直接从头重试。

#### Scenario: 重启后非终态 turn 被解释

- **WHEN** 进程重启前存在状态为执行中的 turn 记录，重启后执行启动扫描
- **THEN** 该 turn 获得终态处置，恢复记录中 `recovery_action` 为取消（运行中任务随重启中断）并包含起止时间与 work id

#### Scenario: unknown 与 compensation_required 语义区分

- **WHEN** 恢复框架对一条可能产生外部副作用的记录做处置判定
- **THEN** 结果不可判定时进入 `unknown` 且不重试原调用；确认有副作用时进入 `compensation_required` 且保留原调用记录等待补偿

### Requirement: 已知丢失窗口有明确记录

系统 SHALL 以文档逐项登记进程内队列与任务的已知丢失窗口（重启后哪些内存态数据不恢复、原因与恢复边界），文档 SHALL 随实现变更保持同步并可被测试断言其存在与覆盖项。

#### Scenario: 丢失窗口文档覆盖主要进程内队列

- **WHEN** 检查丢失窗口记录文档
- **THEN** 文档逐项列出入站队列、出站队列、lane 队列、维护意图与运行中任务的丢失窗口及各自的重启行为
