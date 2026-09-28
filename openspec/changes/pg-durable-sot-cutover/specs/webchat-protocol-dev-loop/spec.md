# webchat-protocol-dev-loop Specification（delta）

> 仅两条 requirement 的行为升级：幂等与重连补拉从进程内语义升级为 durable 承接。
> 帧协议字段、错误码、close code 均不变（C4 冻结契约保持）。

## MODIFIED Requirements

### Requirement: client_message_id 强制幂等

系统 SHALL 以 `client_message_id` 作为 WebChat 入站的强制幂等键：同一 `client_message_id` 重复提交 SHALL NOT 产生第二条入站消息，SHALL 重放首次的 `message.accepted`（含同一 `seq`）；幂等 SHALL 由 durable 存储（数据库唯一约束）承接并在进程重启后存续，SHALL NOT 只依赖连接顺序或进程内存；且在 durable acceptance 之前的 overload 拒绝 SHALL NOT 缓存幂等结果（客户端可重发）。

#### Scenario: 重复 client_message_id 只入队一次

- **WHEN** 同一连接以相同 `client_message_id` 连续发送两次 `send`
- **THEN** 入站队列只出现一条消息，客户端收到两条 `message.accepted` 且二者 `seq` 相同

#### Scenario: 重启后重复 client_message_id 仍重放原 ack

- **WHEN** 服务进程重启后，同一账号以重启前已接受的 `client_message_id` 再次发送 `send`
- **THEN** 不产生第二条入站消息，客户端收到重放的 `message.accepted` 且 `seq` 与首次相同

#### Scenario: 过载拒绝不消耗幂等键

- **WHEN** admission 队列满载导致 `publish_inbound` 抛 overload
- **THEN** 服务端回结构化 `error{code="overload"}`，不分配 `seq`、不缓存该 `client_message_id`，客户端重发可成功

### Requirement: 按 last_sequence 游标的重连补拉

系统 SHALL 为可重放帧（`message.accepted` / `turn.completed` / `turn.failed`）分配会话内单调递增 `seq`，且 `seq` 分配与帧记录 SHALL 随对应 durable 事务持久化（进程重启后存续）；客户端 SHALL 能在重连时以 `replay{after_seq}` 请求补拉，补拉 SHALL 从持久层按 seq 升序服务；持久窗口不覆盖请求游标时服务端 SHALL 回 `replay_required{after_seq}`，由客户端经 REST 从 canonical message 重建；补拉 SHALL NOT 产生重复或乱序帧。

#### Scenario: 断线重连补拉无重复无乱序

- **WHEN** 客户端断线期间服务端产出若干可重放帧，客户端重连并以最后收到的 `seq` 请求 `replay`
- **THEN** 服务端按 seq 升序补发 `after_seq` 之后且仅之后的帧，客户端不收到重复帧或乱序帧

#### Scenario: 重启后补拉仍可用

- **WHEN** 客户端断线期间服务进程重启，重连后以断线前最后 `seq` 请求 `replay`
- **THEN** 服务端从持久层按 seq 升序补发其后全部可重放帧，不因重启丢失或重置 `seq`

#### Scenario: 游标超出 buffer 时指引 REST 重建

- **WHEN** 客户端请求的 `after_seq` 早于持久窗口内最旧 `seq` 之前（或对应数据已被 retention 清理）
- **THEN** 服务端回 `replay_required`，不静默补发部分帧

#### Scenario: delta 与 tool 帧不参与重放

- **WHEN** 服务端发送 `message.delta` / `tool.started` / `tool.completed`
- **THEN** 这些帧不分配 `seq`、不进入持久重放记录，也不出现在任何补拉结果中
