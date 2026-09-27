# tenant-tool-isolation 增量规格

## Purpose

把工具调用的授权边界从「进程级共享可变上下文 + 参数可覆盖」收敛为「服务端派生的不可变执行上下文 + 按租户解析的工具目录 + effect 等级与 typed outcome」，使 WebChat 面向受邀用户开放时，任一 session 持有者只能在服务端派生的租户边界内调用明确白名单内的工具。

## ADDED Requirements

### Requirement: 工具执行上下文由服务端派生且不可覆盖

系统 SHALL 为每次工具调用构造不可变 `ToolExecutionContext`，其中 `account_id`/`tenant_id`/`session_id`/`turn_id`/`channel`/`chat_id`/`principal_type` SHALL 由服务端从已认证身份、可信 channel binding 与当前 turn 派生；模型、客户端帧与 MCP 参数 SHALL NOT 提供或覆盖这些字段。工具执行 SHALL 接受显式 context 并以其为唯一授权依据；历史共享可变上下文 SHALL NOT 再作为跨租户授权依据，且并发 turn 间 SHALL NOT 发生授权身份串扰。`tenant_id` SHALL NOT 作为普通工具参数暴露给模型；内部 context SHALL NOT 原样转发给 MCP。

#### Scenario: 模型参数覆盖可信字段无效

- **WHEN** 模型在工具参数中携带 `tenant_id`/`account_id`/`session_key` 等归属字段，且与执行上下文不同
- **THEN** 工具以执行上下文解析授权与资源目标，参数中的归属字段被忽略并在审计中体现为不可信输入

#### Scenario: 并发 turn 间共享上下文不串租户

- **WHEN** 两个不同租户的 turn 在同一进程内交错执行工具调用
- **THEN** 每次调用按各自的执行上下文解析资源与授权，任一方的资源访问、审计归属与副作用目标不出现另一方租户

### Requirement: 三层工具目录与租户白名单

系统 SHALL 将工具组织为全局只读定义、租户可见目录与管理员目录三层：全局层只承载所有租户共用的只读定义（名称、描述、输入 schema），用户权限、凭据、目录与 binding SHALL NOT 存于其中。普通 tenant 的可见工具 SHALL 由服务端白名单精确到 tool id 冻结，未列入白名单的工具 SHALL 对该租户不可见且执行时拒绝；模型每轮 SHALL 只收到当前上下文对应目录的 schema，且 schema 可见性 SHALL NOT 被当作唯一防线（执行前 SHALL 重新校验账号状态、binding、启用状态、capability 与确认/配额）。P1 白名单范围 SHALL 覆盖：租户内记忆读写（`recall_memory`/`memorize`/`forget_memory`）、本账号消息查询、租户文件读写与图片读取、服务端绑定目标的 `message_push`、租户自有 schedule/reminder、经限流与 SSRF 防护的 web search/fetch。

#### Scenario: 白名单外工具不可见且不可执行

- **WHEN** 普通 tenant 的会话请求执行未列入租户白名单的工具（如 `shell`、`spawn`、`peer_agent`、plugin 管理工具）
- **THEN** 该工具不出现在模型 schema 中，直接调用时被结构化拒绝，不产生副作用

#### Scenario: schema 可见不等于可执行

- **WHEN** 某工具出现在当前租户目录中，但调用时账号已被封禁或 binding 已被撤销
- **THEN** 执行前校验失败并以结构化错误拒绝，不执行任何副作用

### Requirement: effect 等级与 typed outcome

每个工具 SHALL 声明 `read-only`/`tenant-local-write`/`external-read`/`external-write`/`network`/`process-exec`/`admin` 之一的 effect 等级；每次工具调用 SHALL 分配 `tool_call_id` 并收敛到 typed 终态（成功/失败/取消/超时/未知），SHALL NOT 以字符串错误伪装终态。`external-write`、`process-exec` 与 `admin` 等级的调用在缺少幂等键、outcome 与补偿策略时 SHALL 默认拒绝；普通 tenant 对 `process-exec` 与 `admin` 等级工具一律拒绝。

#### Scenario: 高危 effect 无补偿策略时默认拒绝

- **WHEN** 某工具声明 `external-write` 但未登记幂等与 outcome 策略，普通租户会话发起调用
- **THEN** 调用在执行副作用前被结构化拒绝并记录审计终态

### Requirement: 租户路径解析与文件资源边界

文件类工具 SHALL 通过服务端租户路径解析器定位资源 root（按租户与资源类别，如 attachments/scratch/exports），SHALL NOT 接受任意物理路径或由模型/客户端传入的 root；解析 SHALL 拒绝绝对路径、`..` 逃逸与符号链接逃逸，并限制大小、深度与单次读取量。普通租户 SHALL NOT 访问其他租户、system、logs、config 或未授权资源类别。

#### Scenario: 路径逃逸被拒绝

- **WHEN** 租户会话的文件工具调用携带 `../other_tenant/secret` 或指向 system/logs 目录的路径
- **THEN** 解析失败并以结构化错误拒绝，不发生任何读写

### Requirement: 账号状态联动与后台任务重校验

账号进入 `suspended` 或 `revoked` 时，该租户的新工具调用 SHALL 被拒绝，执行中的调用 SHALL 按既有取消边界收束（复用 RuntimeSnapshot revocation recheck 接缝）；`spawn`/`task_output`/`task_stop`/scheduler 等后台任务 SHALL 保存 owner 租户归属，并在真正执行前重新校验账号状态、租户状态、工具 binding 与额度；普通账号 SHALL 只能查看与取消自己租户的后台任务。

#### Scenario: 封禁后执行中调用被收束

- **WHEN** 账号被封禁时该租户有执行中的工具调用
- **THEN** 新调用立即被拒绝，执行中调用被取消或按工具既有 timeout 收束并落 typed 终态，其他租户不受影响

#### Scenario: 后台任务执行前重校验

- **WHEN** 后台任务在创建后账号被封禁，到达执行时机
- **THEN** 任务在执行副作用前被拒绝并记录原因，不产生任何副作用

### Requirement: 工具调用审计与脱敏

每次工具调用 SHALL 产生含租户、账号、session/turn、工具、effect 等级、状态与耗时的审计记录，作为 C12 §8.2 的 tool_call 记录点（E10 指标承接）；API key、OAuth token、完整文件内容等敏感参数 SHALL NOT 原样写入日志、模型上下文或持久化审计，审计中的参数 SHALL 脱敏或仅存 hash。

#### Scenario: 审计记录完整且不含敏感原文

- **WHEN** 任一工具调用完成（无论成功或失败）
- **THEN** 存在一条含租户/账号/turn/工具/effect/状态/耗时的审计记录，且无法从审计中还原敏感参数原文

### Requirement: 跨租户隔离负向验证

系统 SHALL 以自动化测试证明：共享 registry 状态不会在并发交错下串租户；drift flow 与插件自带的文件、shell、发送类工具 SHALL 复用同一套 scope/effect policy，SHALL NOT 绕过主 agent 的租户边界；用户自添加 MCP SHALL 保持整体关闭（不可读取平台环境变量、其他租户目录、数据库连接或 Docker/socket 资源），直到独立 capability 显式开放。

#### Scenario: 插件与 drift 工具同边界

- **WHEN** 插件提供的工具或 drift flow 内的文件/发送工具被租户会话调用
- **THEN** 其授权与资源解析与主 agent 工具一致，越界访问被拒绝

#### Scenario: 用户 MCP 关闭面保持

- **WHEN** 租户会话尝试添加或调用用户 MCP（含 mcp_add/mcp_remove）
- **THEN** 调用被结构化拒绝，且负向测试覆盖环境变量、跨租户目录、数据库连接与 Docker socket 不可达
