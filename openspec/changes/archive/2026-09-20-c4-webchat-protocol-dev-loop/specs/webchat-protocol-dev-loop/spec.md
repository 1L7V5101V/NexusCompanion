# webchat-protocol-dev-loop 增量规格

## Purpose

定义 P0.5 dev-only WebChat 闭环的行为契约：WebSocket 帧协议与版本、`client_message_id` 幂等、按 `last_sequence` 游标的重连补拉（终态从 canonical message 补拉）、慢消费者分级降级与 overload close code、连接生命周期（空闲回收）、`hello` 的服务端派生身份与「客户端不得声明可信归属」的授权边界，以及 dev-only 暴露门禁（P1 认证与 tenant 隔离前不得公网）。生产公网 WebChat（C5 认证承载）与跨通道同步（C10）不在本规格范围。

## ADDED Requirements

### Requirement: WebSocket 帧协议契约与版本

系统 SHALL 以 JSON 文本帧承载 WebChat 协议，帧信封为 `{"type": str, "seq": int | null, ...}`；服务端 SHALL 在 `hello` 中声明 `protocol_version`；协议帧的精确字段、错误码与 close code SHALL 由一份被前后端共同消费的 contract fixture 冻结，任何一侧偏离 SHALL 使该侧契约测试失败。

#### Scenario: hello 声明协议版本与连接身份

- **WHEN** 客户端完成 WebSocket 握手
- **THEN** 服务端立即发送 `hello`，携带 `connection_id`、`protocol_version`、`latest_seq`，且不等待客户端先发帧

#### Scenario: 前后端消费同一契约向量

- **WHEN** 后端 pytest 与前端 `npm run test:chat-protocol` 分别对 `tests/fixtures/chat_protocol_frames.json` 执行断言
- **THEN** 两侧断言的是同一份文件、同一组帧形状/常量/错误用例，任一侧不匹配即失败

#### Scenario: 未知帧类型与坏帧结构化拒绝

- **WHEN** 客户端发送无法解析的 JSON、非对象帧或未知 `type`
- **THEN** 服务端回 `error` 帧（`bad_frame` / `unknown_type`），连接保持开启

### Requirement: 服务端派生身份与客户端归属不可信

系统 SHALL 只在服务端派生 `hello` 的 `account_id` / `tenant_id` / `conversation_id`；客户端入站帧中出现的 `tenant_id` / `account_id` / `session_key` / `channel` 等归属字段 SHALL NOT 参与授权或影响入队消息的归属，SHALL 被忽略。

#### Scenario: hello 携带账号与规范会话

- **WHEN** dev 模式客户端完成握手
- **THEN** `hello` 同时携带服务端派生的 `account_id`、`tenant_id` 与 `conversation_id`（规范会话），并保留 `session_key` 与 `latest_seq`

#### Scenario: 客户端声明他人 tenant 不改变归属

- **WHEN** 客户端在 `send` 帧中携带与当前 dev 身份不同的 `tenant_id`（或 `account_id` / `session_key`）
- **THEN** 服务端入队的 `InboundMessage` 仍使用服务端派生身份，客户端声明的字段被忽略且不产生错误

#### Scenario: 非 UUID 的 client_message_id 被拒绝

- **WHEN** `send` 帧缺失 `client_message_id` 或该值不是 UUID
- **THEN** 服务端回 `error{code="bad_client_message_id"}`，不提交任何入站消息

### Requirement: client_message_id 强制幂等

系统 SHALL 以 `client_message_id` 作为 WebChat 入站的强制幂等键：同一 `client_message_id` 重复提交 SHALL NOT 产生第二条入站消息，SHALL 重放首次的 `message.accepted`（含同一 `seq`）；幂等 SHALL NOT 只依赖连接顺序或内存去重语义之外的假设，且在 durable acceptance 之前的 overload 拒绝 SHALL NOT 缓存幂等结果（客户端可重发）。

#### Scenario: 重复 client_message_id 只入队一次

- **WHEN** 同一连接以相同 `client_message_id` 连续发送两次 `send`
- **THEN** 入站队列只出现一条消息，客户端收到两条 `message.accepted` 且二者 `seq` 相同

#### Scenario: 过载拒绝不消耗幂等键

- **WHEN** admission 队列满载导致 `publish_inbound` 抛 overload
- **THEN** 服务端回结构化 `error{code="overload"}`，不分配 `seq`、不缓存该 `client_message_id`，客户端重发可成功

### Requirement: 按 last_sequence 游标的重连补拉

系统 SHALL 为可重放帧（`message.accepted` / `turn.completed` / `turn.failed`）分配会话内单调递增 `seq` 并缓存于有界重放 buffer；客户端 SHALL 能在重连时以 `replay{after_seq}` 请求补拉；buffer 不覆盖请求游标时服务端 SHALL 回 `replay_required{after_seq}`，由客户端经 REST 从 canonical message 重建；补拉 SHALL NOT 产生重复或乱序帧。

#### Scenario: 断线重连补拉无重复无乱序

- **WHEN** 客户端断线期间服务端产出若干可重放帧，客户端重连并以最后收到的 `seq` 请求 `replay`
- **THEN** 服务端按 seq 升序补发 `after_seq` 之后且仅之后的帧，客户端不收到重复帧或乱序帧

#### Scenario: 游标超出 buffer 时指引 REST 重建

- **WHEN** 客户端请求的 `after_seq` 早于重放 buffer 最旧 `seq` 之前
- **THEN** 服务端回 `replay_required`，不静默补发部分帧

#### Scenario: delta 与 tool 帧不参与重放

- **WHEN** 服务端发送 `message.delta` / `tool.started` / `tool.completed`
- **THEN** 这些帧不分配 `seq`、不进入重放 buffer，也不出现在任何补拉结果中

### Requirement: 慢消费者分级降级与 overload close

系统 SHALL 对 per-connection outbound 实施分级降级：出队深度达 soft 上限（默认 192）时 SHALL 丢弃可丢帧（delta/tool）并首次发 `replay_required`；深度达 hard 上限（默认 256）或累计 payload 达 1 MiB 时 SHALL 以 `CLOSE_OVERLOAD`(1013) 明确断开；canonical final 与 turn 终态 SHALL NOT 因队列满被删除。

#### Scenario: soft 档丢弃 delta 并要求补拉

- **WHEN** 某连接 outbound 深度达到 soft 上限，服务端广播一帧 `message.delta`
- **THEN** 该帧不入队，连接收到一次 `replay_required`，连接保持开启且终态帧仍照常入队

#### Scenario: hard 档与 1 MiB payload 触发 overload close

- **WHEN** 某连接 outbound 深度达到 hard 上限，或累计入队 payload 达到 1 MiB
- **THEN** 服务端以 close code 1013 关闭该连接，终态帧仍保留在重放 buffer 供重连补拉

### Requirement: 连接生命周期与空闲回收

系统 SHALL 在客户端断开、协议错误或心跳/空闲超时时清理连接状态（从连接表移除并释放 outbound 队列），SHALL NOT 因连接清理而取消服务端正在执行的 turn 或 tool。

#### Scenario: 静默连接被心跳超时回收

- **WHEN** 一条连接在空闲超时窗口内没有收到任何客户端帧
- **THEN** 服务端以 `CLOSE_IDLE_TIMEOUT` 关闭该连接并清理其在连接表中的条目

#### Scenario: 断线只影响显示不取消 turn

- **WHEN** 客户端在 turn 执行期间断开连接
- **THEN** 服务端 turn/tool 继续执行至终态，终态帧仍进入重放 buffer 供重连补拉

### Requirement: dev-only 暴露门禁

系统 SHALL 默认关闭 WebChat 通道，且 SHALL 只在显式 dev 模式下允许启用；启用时 SHALL 拒绝非回环 host 绑定（除非显式允许公网绑定）并在运行期拒绝非回环客户端；在 P1 认证与 tenant 隔离落地前，WebChat SHALL NOT 公网暴露。

#### Scenario: 非 dev 模式启用通道即失败

- **WHEN** 配置启用 `channels.chat` 但 `agent.dev_mode` 为 false
- **THEN** 启动 fail-fast 且不创建 WebChat 通道（不静默降级为可用通道）

#### Scenario: 非回环绑定与运行期非回环请求被拒

- **WHEN** dev 模式下 host 被配置为非回环地址且未显式允许公网绑定，或非回环客户端访问已启动的 WebChat 入口
- **THEN** 前者拒绝启动，后者以 HTTP 403 / WebSocket 1008 拒绝，均不放行消息
