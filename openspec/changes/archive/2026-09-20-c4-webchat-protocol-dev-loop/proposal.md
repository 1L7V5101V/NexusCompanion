# C4 WebChat protocol + dev-only channel/Gateway/frontend

> 对应任务计划：`openspec/openspec-tasks-bundle/task-04-webchat-protocol-dev-loop.md`（PILOT_ROADMAP §5.9.10 第 4 项）。
> 输入的已冻结决策（§5.9.4 WS 协议/游标/慢消费者、§5.9.1 WS replay 硬冲突、§5.9.5 per-connection outbound、§5.6 双入口同步、§10 DECIDED 消息顺序与幂等）不在此重复论证，design.md 逐条引用。
> 前置已落地：C1 canonical identity（commit `e124dbf8`，account→tenant→canonical conversation resolver + canonical message/sequence）；C2 durable control plane（`7ed6897d`：inbox/outbox/delivery 三事务）；C3 admission（`7c405e8`：tenant lane + 有界队列 + WS outbound 分级降级）。

## Why

P0.5 的 dev-only WebChat 闭环在 C1/C2/C3 之前就有一版实现（commit `4e40e510`，2026-09-03）。它把「能收发」跑通了，但对照本 task 的验收标准仍有明确缺口：

- **`hello` 不携带账号与规范会话**：只回 `session_key`（dev 常量），客户端无法知道服务端派生的账号/tenant/canonical conversation，也没有任何测试断言「客户端 payload 里的 tenant/session 字段不参与授权」——§5.9.1 的硬冲突正是靠这条负向语义防住的。
- **无 dev-only 暴露门禁**：`channels.chat` 只要 `enabled` 就绑 `host`（配置里可以写 `0.0.0.0`），没有任何代码路径阻止「非 dev 模式 + 非回环地址」的启动或请求。P0.5 出口要求「dev-only 暴露门禁，P1 前不得公网」。
- **无异常连接回收**：`handle_websocket` 只靠客户端断开退出；客户端静默掉线（拔网线/休眠）会永久占住连接与 outbound 队列，没有心跳超时或空闲回收。
- **协议 contract fixture 只被后端单向消费**：`tests/fixtures/chat_protocol_frames.json` 由 `tests/test_web_chat_channel.py` 参数化，前端 `frontend/chat/src/protocol.ts` 只是「人工保持一致」，没有任何可执行断言；task 要求「前后端共用同一组向量」双向执行。
- **重连补拉与断线不取消 turn 没有测试**：`_ReplayBuffer` 有单元测试，但没有「断线 → 重连 → 按 `last_sequence` 补拉无重复、无乱序」的端到端断言，也没有「断线只影响显示、不取消服务端 turn/tool」的负向断言。
- **`tests/test_chat_api.py` 在当前 `.venv` 下无法收集**（`-W error` + 第三方 `anyio.abc.BlockingPortal` DeprecationWarning），C4 自己的 Gateway 路由测试处于不可运行状态。

## What Changes

- **协议帧契约补齐身份字段**：`hello` 增加服务端派生的 `account_id` / `tenant_id` / `conversation_id`（dev 模式为显式单用户身份），保留 `connection_id` / `protocol_version` / `session_key` / `latest_seq`；新增 `CLOSE_IDLE_TIMEOUT` 与 dev-only 单用户身份常量。
- **入站帧授权边界显式化**：客户端 `send` 帧中的 `tenant_id` / `account_id` / `session_key` 等字段一律忽略，服务端只用显式 dev 身份；补负向断言。
- **dev-only 门禁（feature flag）**：`chat` 通道仅在 `agent.dev_mode = true` 时可启用；`build_chat_server` 拒绝非 dev 模式启动、拒绝非回环 host（除非显式 `allow_public_bind`）；HTTP/WS 运行期再加一层「客户端地址必须回环」中间件。默认关闭、显式 opt-in 才开放。
- **连接生命周期**：服务端空闲读超时（`idle_timeout_s`）主动 close（`CLOSE_IDLE_TIMEOUT`）并回收连接与 outbound 队列；前端增加 keepalive `ping`，配合既有 `pong` 保持长连接。
- **协议 contract fixture 双向执行**：扩展 `tests/fixtures/chat_protocol_frames.json`（错误用例带预期错误码），后端 `tests/test_web_chat_protocol_contract.py` 参数化驱动；前端新增 `frontend/chat/scripts/protocol-contract.test.mjs` 用 esbuild 就地打包 `protocol.ts` 后对同一 fixture 做形状/常量/解析断言，`package.json` 新增 `test:chat-protocol`。
- **端到端 dev 测试**：新增 `tests/test_web_chat_e2e_dev.py`，用真实 `create_chat_app` + 真实 uvicorn + 真实 WebSocket 客户端，覆盖收发/流式/终态、重连补拉无重复、断线不取消 turn。
- **修 `tests/test_chat_api.py` 收集失败**：对第三方 anyio 弃用告警做局部抑制（不改 pytest 全局 `-W error`），使 C4 自己的路由测试可运行。

## Capabilities

### New Capabilities

- `webchat-protocol-dev-loop`：dev-only WebChat 的协议帧契约（`hello/send/message.accepted/message.delta/tool.*/turn.completed/turn.failed/replay_required/error/pong`）、`client_message_id` 幂等、按 `last_sequence` 游标的重连补拉（终态从 canonical message 经 REST 补拉）、慢消费者分级降级（soft 192 丢 delta + `replay_required`；hard 256 或 1 MiB → overload close）、连接生命周期（空闲回收）、`hello` 身份三元组与「客户端不得声明可信 tenant」的授权边界、dev-only 暴露门禁。

### Modified Capabilities

- 无（对既有 spec 只消费不修改：`canonical-identity` 的 resolver 与 sequence、`durable-control-plane` 的 inbox/outbox、`admission-queue-recovery` 的 WS outbound 限幅均为单向依赖本 change 的输入）。

## Non-Goals（明确不做）

- **不做 C5 auth**：不建 principal/session/invitation provision，不实现 Token 登录；dev 身份是显式单用户回退路径，不是认证。WebChat 承载认证（E3）留待 C5。
- **不做 C10 Telegram 同步推送**：不实现跨通道同步；本 change 只交付可被 C10 复用的实时推送通道。
- **不篡改 C2/C3 语义**：不新建 durable 表、不改 admission 容量冻结值、不把 delta 变成可重放；dev 模式的进程内 ring buffer 与 REST 补拉保持与 §5.9.4「delta 不跨重启重放、终态从持久层补拉」一致。
- **不做 C14 memory engine selector**：前端只交付 WebChat bundle 本体。
- **不做公网部署**：不配置反向代理/证书/公网域名；P1 前 WebChat 不得公网暴露是负向验收项而非功能项。
- **不做大规模前端重构**：只在既有 `frontend/chat`（React + assistant-ui）上补协议字段、keepalive 与 contract 测试。

## Impact

- **代码**：修改 `infra/channels/web_chat_protocol.py`、`infra/channels/web_chat_channel.py`、`bootstrap/chat_api.py`、`bootstrap/app.py`、`agent/config_models.py`、`agent/config.py`、`config.example.toml`；前端修改 `frontend/chat/src/protocol.ts`、`frontend/chat/src/connection.ts`，新增 `frontend/chat/scripts/protocol-contract.test.mjs`、`package.json` script。
- **测试**：新增 `tests/test_web_chat_protocol_contract.py`、`tests/test_web_chat_e2e_dev.py`、`tests/test_web_chat_dev_gate.py`；扩展 `tests/test_web_chat_channel.py`、`tests/fixtures/chat_protocol_frames.json`；修复 `tests/test_chat_api.py` 收集。
- **配置**：`[channels.chat]` 新增 `idle_timeout_s`（默认 90）、`allow_public_bind`（默认 false）；`enabled=true` 现在要求 `agent.dev_mode=true`。
- **依赖**：无新增第三方依赖（e2e 用既有 `uvicorn` + `websockets`；前端 contract 用既有 `esbuild`）。
- **行为变化**：非 dev 模式下 `channels.chat.enabled=true` 从「能启动」变为「启动即失败」；空闲连接从「永久占用」变为「超时回收」。
