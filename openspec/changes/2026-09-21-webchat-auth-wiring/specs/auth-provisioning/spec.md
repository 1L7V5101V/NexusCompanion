# auth-provisioning 增量规格

## Purpose

C5 在 `bootstrap/auth/ws_guard.py` 中实现了 WebSocket 握手的校验函数（`check_ws_handshake`：Cookie + Origin），但该函数在 C5 范围内只交付到函数层——C5 change 的 `tasks.md` 5.2 明确「通道接线归 C4 消费」。由于 C4 已归档且为 dev-only，该接线实际无人承接，导致认证能力落在纸面上。本增量把该 guard 提升为**通道边界的强制契约**，使「HTTP 与 WebSocket 使用同一套凭据语义」成为可验收的要求。

## ADDED Requirements

### Requirement: WebSocket 握手认证

系统 SHALL 在 WebSocket 通道 `accept` 之前校验连接凭据，校验对象 SHALL 与 HTTP 面同一套：普通用户会话 Cookie（`__Host-nexus_session`）与 Origin allowlist。校验失败 SHALL NOT 完成 `accept`、SHALL NOT 提交任何入站消息。拒绝 SHALL 只区分「凭据」与「来源」两类，SHALL NOT 回显会话不存在/过期/已撤销/账号被封等具体原因。校验 SHALL 使用与 HTTP 面相同的 session 有效性判定（含 timeout、撤销、账号状态），SHALL NOT 维护第二套会话状态。

#### Scenario: 无有效会话的连接被拒绝

- **WHEN** 客户端发起 WebSocket 握手但未携带 `__Host-nexus_session`，或该会话不存在/已过期/已撤销/所属账号已 suspended
- **THEN** 握手以「凭据」类协议级拒绝关闭，未完成 `accept`，且不产生任何入站消息

#### Scenario: Origin 不在白名单的连接被拒绝

- **WHEN** 握手携带有效会话，但 Origin 不在 `origin_allowlist` 内（或缺失）
- **THEN** 握手以「来源」类协议级拒绝关闭，不完成 `accept`，且不产生任何入站消息

#### Scenario: 有效会话与合法来源的握手成功

- **WHEN** 握手携带有效会话且 Origin 在 allowlist 内
- **THEN** 握手完成 `accept`，后续 `hello` 返回按该会话派生的服务端身份三元组

#### Scenario: 拒绝不泄露账户状态

- **WHEN** 分别以「会话不存在」「会话已撤销」「账号已 suspended」三种情形发起握手
- **THEN** 三者得到同一类拒绝语义，响应中不出现可区分三者的字段或文案

#### Scenario: 未启用认证的实例不引入握手门禁

- **WHEN** 实例未启用 `[auth]`（dev-only 路径）
- **THEN** 握手不执行凭据校验，行为与本增量引入前一致（dev 回退身份），且该路径 SHALL NOT 在启用认证的实例上生效
