# durable-work-queue 增量规格

## Purpose

定义 `background_work_items` 的 durable 消费契约：数据库租约认领、崩溃恢复清扫、同租户串行与跨租户并发、有界背压与延后语义、副作用与终态同事务的 effectively-once、租约丢失的写入禁止、运行期接线与优雅停止、租户隔离，以及 §7.1 生命周期事件的记录点与内容边界。时限与容量数值为可配置的 Pilot 初始值，不是业务成功保证。

## ADDED Requirements

### Requirement: work item 数据库租约认领

系统 SHALL 以数据库租约认领 `background_work_items`：认领 SHALL 在单条语句内把到期项（`queued` 且 `next_attempt_at` 已到期，或租约已过期的 `in_progress`）推进为 `in_progress`，记录租约属主与到期时间，并把 `attempt_count` 加一；认领 SHALL 使用 `FOR UPDATE SKIP LOCKED` 使并发扫描器互不阻塞；认领 SHALL 单轮内对同一 `tenant_id` 至多返回一条；认领 SHALL NOT 返回任何其 `tenant_id` 当前已有有效租约在途（`in_progress` 且租约未过期）的工作项；终态（`succeeded`/`failed`/`cancelled`）SHALL NOT 被认领——即 `failed`（死信）不再自动重试是**结构性**成立，而非依赖计数门槛。

#### Scenario: 到期 queued 项被认领为 in_progress

- **WHEN** 存在 `status='queued'` 且 `next_attempt_at <= now()` 的工作项，且调用方请求认领
- **THEN** 该行变为 `in_progress`，`lease_owner` 为调用方标识、`lease_expires_at` 为当前时间加租约时长，`attempt_count` 增加 1

#### Scenario: 未到期的 failed 项不被认领

- **WHEN** 存在 `status='failed'` 但 `next_attempt_at > now()` 的工作项
- **THEN** 该行不被本次认领，状态与租约不变

#### Scenario: 达到尝试上限的业务失败进入死信且不再被认领

- **WHEN** 某工作项因业务失败达到最大尝试次数并进入 `failed` 终态
- **THEN** 该行不再被认领，且其 `attempt_count` 与 `last_error` 保留可查

#### Scenario: 同一租户单轮至多认领一条

- **WHEN** 同一 `tenant_id` 存在多条同时就绪的工作项，且调用方请求认领一批
- **THEN** 本次该租户至多返回一条，其余保持 `queued` 并在后续轮次可被认领

#### Scenario: 已有有效租约在途的租户本轮不被认领

- **WHEN** 某租户已有一条 `in_progress` 且租约未过期的工作项，同时该租户还有其它就绪的 `queued` 工作项
- **THEN** 本次认领不返回该租户的任何工作项，其就绪项保持 `queued`，直到该租户的在途项收束或租约过期

#### Scenario: 在途项租约过期后该租户重新可被认领

- **WHEN** 某租户在途工作项的租约已过期（持它者已崩溃）
- **THEN** 该租户重新可被认领，且其就绪工作项也不再被互斥条件阻塞

#### Scenario: 并发认领同一行只成功一次

- **WHEN** 两个认领者并发对同一条就绪工作项发起认领
- **THEN** 恰好一个认领成功，另一个不获得该行且不阻塞

### Requirement: 崩溃恢复由租约过期清扫实现

系统 SHALL 提供清扫：把 `status='in_progress'` 且租约已过期的工作项复位为 `queued` 并清空租约属主与到期时间，使进程崩溃后遗留的工作项可被重新执行；清扫 SHALL 为每条复位产生可观测的恢复记录；清扫 SHALL NOT 视为业务失败，SHALL NOT 触发退避耗尽。

#### Scenario: 崩溃遗留项被复位为 queued

- **WHEN** 某工作项处于 `in_progress` 且 `lease_expires_at < now()`（持有它的进程已消失）
- **THEN** 清扫把它复位为 `queued` 并清空租约，后续轮次可被重新认领

#### Scenario: 复位不推高尝试计数

- **WHEN** 一条工作项因崩溃被清扫复位
- **THEN** 其 `attempt_count` 不因该次复位而增加，也不会仅因反复崩溃而进入 `failed` 终态

#### Scenario: 租约未过期不被复位

- **WHEN** 某工作项处于 `in_progress` 且租约尚未过期
- **THEN** 清扫不改动该行

#### Scenario: 清扫产出恢复记录

- **WHEN** 清扫复位了一条工作项
- **THEN** 产生一条含工作项标识、租户、恢复动作与恢复耗时的恢复记录，且其中不含工作项负载原文

### Requirement: 同租户串行与跨租户并发

系统 SHALL 保证同一 `tenant_id` 的工作项不并发执行（同一时刻至多一条在途），不同 `tenant_id` 的工作项 SHALL 可并行推进、互不阻塞；该串行语义 SHALL 在成功、失败、取消、超时与关闭路径上统一释放占用。

#### Scenario: 同租户两条不并发

- **WHEN** 同一租户的两条工作项都被执行
- **THEN** 第二条在第一条收束之后才开始，二者执行区间不重叠

#### Scenario: 跨租户并行

- **WHEN** 两个不同租户各有一条就绪工作项，且其中一个的执行被阻塞
- **THEN** 另一个租户的工作项仍可完成，不被前者的阻塞影响

#### Scenario: 异常路径释放占用

- **WHEN** 某租户的工作项在执行中失败、被取消或超时
- **THEN** 该租户的串行占用被释放，后续该租户的工作项仍可执行

### Requirement: 有界背压与延后语义

系统 SHALL 对认领与执行施加上界：单轮认领数量 SHALL 有界；每租户在途数量 SHALL 有界（至多一）；维护类工作项在交互类工作占优时 SHALL 被延后，延后 SHALL NOT 消耗尝试次数、SHALL NOT 计为失败，且被延后的工作项 SHALL 保持可被重新认领。

#### Scenario: 单轮认领数量受限

- **WHEN** 就绪工作项数量超过单轮认领上限
- **THEN** 本次只认领不超过上限的数量，其余保持可认领状态，不产生堆积

#### Scenario: 维护类被延后不消耗尝试

- **WHEN** 某租户的维护类工作项在其交互类工作项忙碌期间被延后
- **THEN** 该工作项不计为失败、尝试计数不因该次延后而增加，且释放租约后可再次被认领

#### Scenario: 延后不阻塞其他租户

- **WHEN** 某租户的维护类工作项被延后
- **THEN** 其他租户的工作项认领与执行不受影响

### Requirement: 副作用与终态同事务的 effectively-once

系统 SHALL 提供「业务副作用写入与工作项终态推进在同一数据库事务内提交」的能力，且该事务 SHALL 以「租约仍归属本次认领者且状态为 in_progress」为前置条件；系统 SHALL NOT 提供绕过该前置条件单独推进终态的入口；同一工作项 SHALL 至多产生一条终态。

#### Scenario: 成功副作用与 succeeded 同提交

- **WHEN** 执行者成功处理一条工作项并请求提交
- **THEN** 业务写入与 `succeeded` 终态在同一事务内生效，任一失败则整体回滚且工作项状态不变

#### Scenario: 前置不成立时拒绝提交

- **WHEN** 执行者尝试提交终态但其已不再持有该租约
- **THEN** 提交被拒绝，工作项状态与业务写入均不改变

#### Scenario: 重复入队不产生第二行

- **WHEN** 以相同幂等键两次创建同一逻辑工作项
- **THEN** 只存在一行，第二次请求返回既有行

### Requirement: 租约丢失不得推进终态

系统 SHALL 在租约丢失（续租失败或终态提交前置不成立）时放弃本次尝试的任何状态推进，并 SHALL 记录该放弃；被放弃的工作项 SHALL 由后续认领或清扫收束，SHALL NOT 出现执行结果已产生而状态被本执行者改写的情形。

#### Scenario: 失租后不推进成功

- **WHEN** 执行者的续租返回失败（他人已接管）且其执行随后成功返回
- **THEN** 该执行者不推进 `succeeded`，工作项状态由接管者收束

#### Scenario: 失租后不推进失败

- **WHEN** 执行者的续租返回失败且其执行抛错
- **THEN** 该执行者不写入失败状态，工作项由接管者收束

#### Scenario: 失租被记录

- **WHEN** 发生上述任一放弃
- **THEN** 产出可查的放弃记录（含工作项标识与原因），且不含工作项负载原文

### Requirement: 运行期接线与优雅停止

系统 SHALL 在应用启动时启动工作项消费者、在关闭时停止它，并使消费者的数据库连接在关闭时释放；停止 SHALL 保证不再认领新工作项、SHALL 等待在途执行收束而不中途取消正在执行的业务处理；关闭后仍未收束的工作项 SHALL 由下次启动的清扫收束；在单机 SQLite 模式下 SHALL NOT 启动该消费者。

#### Scenario: 启动后开始消费

- **WHEN** 应用以 PostgreSQL 后端启动且消费者处于启用状态
- **THEN** 消费者开始认领并执行就绪工作项

#### Scenario: 停止后不再认领

- **WHEN** 应用进入关闭流程
- **THEN** 消费者不再认领新工作项，且在途执行被等待收束而非被取消

#### Scenario: 关闭不产生新工作

- **WHEN** 消费者已停止
- **THEN** 不因本次关闭产生新的工作项或状态推进

#### Scenario: SQLite 模式不启动消费者

- **WHEN** 存储后端为 SQLite
- **THEN** 不建立消费者所需的数据库连接，也不启动消费者

#### Scenario: 关闭后遗留项由下次清扫收束

- **WHEN** 关闭时存在被中断而未收束的工作项
- **THEN** 下次启动的清扫可将其复位为可重新执行状态

### Requirement: work item 消费的租户隔离

工作项的认领与查询 SHALL 以可信租户上下文过滤；跨租户读取或推进其它租户的工作项 SHALL 返回空或失败，SHALL NOT 泄露存在性或内容；终态推进 SHALL 校验 `tenant_id`。

#### Scenario: 跨租户推进被拒绝

- **WHEN** 以租户 X 的上下文推进租户 Y 的工作项
- **THEN** 操作失败且不改变任何行

#### Scenario: 跨租户查询不可见

- **WHEN** 以租户 X 的上下文查询租户 Y 的工作项
- **THEN** 返回为空，不泄露租户 Y 的行

### Requirement: 生命周期事件的记录点与内容边界

系统 SHALL 按已冻结的事件 schema 记录工作项生命周期事件，至少覆盖认领、开始、收束与恢复四类记录点及派生耗时（排队等待、执行耗时、崩溃恢复到恢复完成的耗时）；SHALL NOT 记录工作项负载原文、业务输入输出原文或任何内容型字段；自由文本字段 SHALL 入库前经脱敏；指标标签 SHALL NOT 使用工作项标识或租户标识等高基数字段。

#### Scenario: 收束记录含派生耗时

- **WHEN** 一条工作项执行收束
- **THEN** 产生含工作项标识、租户、工作类型、起止时间与执行耗时的记录

#### Scenario: 不记录负载原文

- **WHEN** 工作项携带负载或业务输入输出
- **THEN** 生命周期记录中不出现这些原文与内容型字段

#### Scenario: 恢复记录含恢复耗时

- **WHEN** 清扫复位了一条崩溃遗留的工作项
- **THEN** 记录中含恢复动作与「从启动到恢复完成」的耗时

#### Scenario: 自由文本字段脱敏

- **WHEN** 将失败原因一类的自由文本随生命周期记录入库
- **THEN** 入库前的文本已过脱敏处理

#### Scenario: 指标标签不含高基数字段

- **WHEN** 注册或记录工作项相关指标
- **THEN** 使用的标签属于已登记白名单，不含工作项标识或租户标识

### Requirement: 死信终态与人工重投

系统 SHALL 把 `failed` 作为**死信终态**：它 SHALL 唯一来自「业务失败达到最大尝试次数」，SHALL NOT 由崩溃或清扫产生；系统 SHALL 提供人工重投（redrive），把死信工作项复位为可重新执行并保留原历史；对非死信状态的 redrive 请求 SHALL 被拒绝。

#### Scenario: 崩溃不产生死信

- **WHEN** 某工作项因进程崩溃被清扫复位，且此后反复经历崩溃
- **THEN** 它始终不被判定为 `failed`，且始终可被重新认领

#### Scenario: redrive 保留历史并复位

- **WHEN** 管理员对一个 `failed` 工作项请求 redrive 并附原因
- **THEN** 追加一条带原因的处置记录，原有生命周期记录不变，该工作项回到可被认领状态且尝试计数复位

#### Scenario: 非死信状态拒绝 redrive

- **WHEN** 对一个 `succeeded`、`queued` 或 `in_progress` 的工作项请求 redrive
- **THEN** 请求被拒绝且不产生任何处置记录或状态变化

### Requirement: 生命周期审计流

系统 SHALL 以只追加的方式记录每个工作项的逐次生命周期事件，至少区分：成功、失败、延后释放、崩溃恢复复位与人工重投五类结果；每条记录 SHALL 含工作项标识、结果、错误（如有）与起止时间；该审计流 SHALL NOT 被 redrive 或任何操作删除或改写。

#### Scenario: 每次失败各留一行

- **WHEN** 同一工作项先后失败三次（每次错误不同）
- **THEN** 审计流中存在三条独立记录，各自的错误与时间可分别读出

#### Scenario: 崩溃恢复与延后同样留痕

- **WHEN** 某工作项被清扫复位，或被判定维护类延后
- **THEN** 审计流中分别追加对应结果的记录

#### Scenario: redrive 不抹除历史

- **WHEN** 对一个已有多次失败记录的死信工作项执行 redrive
- **THEN** 先前的失败记录全部保留，并新增一条带原因的重投记录

### Requirement: 工作类型词汇与分派

工作项 SHALL 同时携带任务类型（`interactive` 或 `maintenance`）与业务链路（`passive`/`proactive`/`drift`/`consolidation`/`optimizer`）两个字段；任务类型 SHALL 决定调度优先级（同租户串行的 lane），业务链路 SHALL 决定执行处理器；两者 SHALL NOT 互相代替（业务链路的值不得用作任务类型）。

#### Scenario: 任务类型决定 lane

- **WHEN** 一个工作项的任务类型为交互类
- **THEN** 它按交互类优先级执行；任务类型为维护类时按维护类语义执行（可被延后）

#### Scenario: 业务链路决定处理器

- **WHEN** 两个工作项任务类型相同但业务链路不同
- **THEN** 它们被分派给各自链路的处理器

#### Scenario: 业务链路值不得冒充任务类型

- **WHEN** 校验一条工作项
- **THEN** 其任务类型取值属于任务类型词表，`consolidation` 一类的链路值出现在任务类型字段中被视为非法
