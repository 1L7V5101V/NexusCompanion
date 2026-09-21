# webchat-protocol-dev-loop 增量规格

## Purpose

在 C4 已冻结的 dev-only WebChat 闭环之上，把「暴露门禁」与「服务端派生身份」两处从 dev-only 形态推进为可承载 C5 认证的形态：门禁由 `dev_mode` 驱动改为认证驱动，身份由常量 dev 三元组改为按已认证 session 经 C1 身份链解析。协议帧、幂等、重连补拉、慢消费者降级与连接生命周期等 C4 既有契约不在本增量内变更。

## MODIFIED Requirements

### Requirement: 服务端派生身份与客户端归属不可信

系统 SHALL 只在服务端派生 `hello` 的 `account_id` / `tenant_id` / `conversation_id`；客户端入站帧中出现的 `tenant_id` / `account_id` / `session_key` / `channel` 等归属字段 SHALL NOT 参与授权或影响入队消息的归属，SHALL 被忽略。启用认证时，该三元组 SHALL 由完成握手的已认证 session 经服务端身份链解析得到；SHALL NOT 取自客户端字段、SHALL NOT 取自连接顺序、SHALL NOT 回退到默认租户。未启用认证时，SHALL 使用显式 dev 单用户身份作为回退，且该回退 SHALL NOT 在启用认证的实例上生效。

#### Scenario: hello 携带账号与规范会话

- **WHEN** 客户端完成握手
- **THEN** `hello` 同时携带服务端派生的 `account_id`、`tenant_id` 与 `conversation_id`（规范会话），并保留 `session_key` 与 `latest_seq`

#### Scenario: 认证模式下身份来自已认证 session

- **WHEN** 持有的有效 session 属于账号 A，客户端完成握手
- **THEN** `hello` 返回的三元组等于按该 session 解析出的账号 A 的身份链结果，且不因客户端帧内容而改变

#### Scenario: 客户端声明他人 tenant 不改变归属

- **WHEN** 客户端在 `send` 帧中携带与当前身份不同的 `tenant_id`（或 `account_id` / `session_key`）
- **THEN** 服务端入队的 `InboundMessage` 仍使用服务端派生身份，客户端声明的字段被忽略且不产生错误

#### Scenario: 认证模式下不得回退到 dev 身份

- **WHEN** 实例启用认证，但握手未携带有效 session
- **THEN** 握手被拒绝，SHALL NOT 以 dev 单用户身份建立连接

#### Scenario: 非 UUID 的 client_message_id 被拒绝

- **WHEN** `send` 帧缺失 `client_message_id` 或该值不是 UUID
- **THEN** 服务端回 `error{code="bad_client_message_id"}`，不提交任何入站消息

## REMOVED Requirements

### Requirement: dev-only 暴露门禁

**Reason**：该要求的放行前提是「P1 认证与 tenant 隔离落地前不得启用」，而 C5 即为 P1 认证。以 `dev_mode` 作为放行条件会把「启动 WebChat」与「打开 LLM payload 全量落盘」绑在一起，属于必须消除的耦合。

**Migration**：由本增量新增的 `认证驱动的暴露门禁` 取代。无 auth 且非 dev 的实例仍 fail-fast，对外行为不变；唯一变化是启用认证的实例不再需要 `dev_mode`。

## ADDED Requirements

### Requirement: 认证驱动的暴露门禁

系统 SHALL 默认关闭 WebChat 通道，且仅在**已启用认证**或**显式 dev 模式**之一成立时允许启用；两种情形下 SHALL 都拒绝非回环 host 绑定（除非显式允许公网绑定），并在运行期拒绝非回环客户端。启用认证时，凭据门禁 SHALL 强制生效（见 `auth-provisioning` 的 `WebSocket 握手认证`）。`dev_mode` SHALL NOT 被用作启用认证实例的放行条件。

#### Scenario: 启用认证时无需 dev 模式即可启动

- **WHEN** 配置 `auth.enabled = true` 且启用 `channels.chat`，`agent.dev_mode` 为 false
- **THEN** 通道正常创建并只接受携带有效 session 的连接

#### Scenario: 无认证且非 dev 时启动即失败

- **WHEN** `auth.enabled = false`、`agent.dev_mode = false`，但配置启用 `channels.chat`
- **THEN** 启动 fail-fast 且不创建 WebChat 通道（不静默降级为可用通道）

#### Scenario: 非回环绑定与运行期非回环请求被拒

- **WHEN** host 被配置为非回环地址且未显式允许公网绑定，或非回环客户端访问已启动的 WebChat 入口
- **THEN** 前者拒绝启动，后者以 HTTP 403 / WebSocket 拒绝码关闭，均不放行消息

#### Scenario: 启用认证不打开 payload 快照

- **WHEN** 实例以 `auth.enabled = true`、`agent.dev_mode = false` 启动
- **THEN** LLM payload 快照保持关闭（认证 SHALL NOT 以打开 dev 调试面为代价）
