# C4 WebChat protocol + dev-only loop — 设计冻结

> 门禁依据：PILOT_ROADMAP §5.9.4（WS 协议/游标/慢消费者）、§5.9.1（WS replay 硬冲突）、§5.9.5（per-connection outbound 限幅）、§5.6（双入口同步）、§10 OPEN FOR P-1 SPEC「WebSocket frame/error schema」、§10 DECIDED「消息顺序与幂等」。
> 本文档把 §10 的 open item「精确 JSON 字段/版本/错误码/close code」收敛为可执行契约，并给出 dev-only 门禁、连接生命周期、前后端共享 fixture 的实现决策。

## 1. §5.9 冻结语义 → 本 change 落地

| 冻结语义 | 落地 |
| --- | --- |
| §5.9.4 客户端带 `last_sequence` 游标重连，服务端按游标补拉终态 | 客户端 `replay{after_seq}` → `_ReplayBuffer.frames_after()`；buffer 不覆盖时回 `replay_required{after_seq}`，前端转 REST `GET /api/chat/sessions/{key}/messages` 重建（终态来自 canonical message） |
| §5.9.4 delta 是在线优化、不进入恢复承诺 | `message.delta` / `tool.*` 不盖 seq、不入 replay buffer、soft 区间可丢；`message.accepted` / `turn.completed` / `turn.failed` 盖 seq 且可重放 |
| §5.9.1「WS replay 硬冲突」（客户端不能声明可信租户/会话） | `hello` 的 `account_id/tenant_id/conversation_id` 全部服务端派生；`send` 帧里的同名字段被忽略并有负向测试 |
| §5.9.5 per-connection outbound 限幅 | 复用 C3 已落地的 `_Connection`：soft 192（丢可丢帧 + `replay_required`）、hard 256 或累计 1 MiB（`CLOSE_OVERLOAD` 1013）；本 change 只补「默认值档位的矩阵断言」与文档 |
| §5.6 双入口同步 | dev 模式单 canonical session，事件 handler 已按 `event.channel == self.name` 过滤；本 change 不新增过滤逻辑，只补端到端断言 |
| §10 DECIDED 幂等键 | `client_message_id` 强制 UUID、重复帧重放原 `message.accepted`（同 seq）、overload 发生在 durable acceptance 之前故不缓存幂等 |

## 2. ADR 记录

### ADR-1 `hello` 回服务端派生身份三元组，不回任何客户端可影响的值

- **决策**：`hello` 新增 `account_id` / `tenant_id` / `conversation_id` 三个字段，全部由服务端在 accept 后立即决定：P0.5 dev 模式取显式单用户身份（`dev:local` / `default` / `chat:local`）；P1 由 C5 认证 + C1 `CanonicalIdentityResolver` 产出。三个字段都随帧返回给客户端仅供展示/调试，**不构成授权来源**。
- **备选**：`hello` 只回 `session_key`，账号信息等 C5 再补。**不选**：task-04 验收明确要求 `hello` 携带账号与规范会话；且前端需要在 P1 接 selector（C14 D7）前就拿到稳定身份字段，晚补会破坏 fixture 兼容性。
- **后果**：fixture 的 `hello` 向量与前端 `HelloFrame` 类型同步扩展；协议版本保持 `0`（P0.5 内前后端同仓库同发布，未对外冻结，不需要 bump）。

### ADR-2 客户端 payload 的授权字段「静默忽略 + 负向断言」，不报错

- **决策**：`send` 帧出现 `tenant_id` / `account_id` / `session_key` / `channel` 等授权相关字段时，服务端**忽略**它们，按 dev 身份构造 `InboundMessage`，并补测试断言「即使客户端声明了别的 tenant，落库/入队仍是服务端身份」。
- **备选**：见到这些字段就回 `error` 拒绝。**不选**：客户端 JS 对象天然可能带旧字段（向前兼容），且「拒绝」会把「客户端的错误声明」变成可观测的失败，反而诱导攻击者探测；静默忽略 + 服务端恒定身份更符合 fail-closed 的「永不信任客户端归属」。
- **后果**：负向测试是这条语义的唯一防线，必须断言而非仅靠注释（task 明文要求）。

### ADR-3 dev-only 门禁 = 三层（配置 ∧ 绑定 ∧ 运行期），默认关闭

- **决策**：
  1. **配置层**：`[channels.chat].enabled=true` 且 `agent.dev_mode=true` 才创建 `WebChatChannel`；否则 fail-fast 抛错（不静默降级）。
  2. **绑定层**：`build_chat_server` 要求 `dev_mode=True`；host 不在回环集合（`127.0.0.1` / `::1` / `localhost`）时必须显式 `allow_public_bind=true`，否则拒绝启动。
  3. **运行期**：HTTP/WS 中间件校验 `request.client.host` 属于回环集合（含 `testclient`），非回环请求直接 403 / WS 1008 关闭。
- **备选 1**：只查配置。**不选**：`host` 可被配置成 `0.0.0.0`，只查 `enabled` 挡不住公网暴露。
- **备选 2**：只查绑定。**不选**：进程被放在反向代理后时绑定是回环、真实客户端是公网，绑定层看不见。
- **后果**：P1 接 C5 认证时，只需把「运行期回环校验」替换为「principal 认证」，前两层仍作为额外兜底。

### ADR-4 空闲回收 = 服务端读超时 + 前端 keepalive ping（不引入服务端 ping 帧）

- **决策**：服务端对每条连接做 `asyncio.wait_for(receive_text(), timeout=idle_timeout_s)`；超时即 `close(CLOSE_IDLE_TIMEOUT=1001)` 并从 `_connections` 移除。前端 `ChatConnection` 在 `online` 期间每 `KEEPALIVE_INTERVAL_MS=25000` 发一帧 `ping`，服务端沿用既有 `pong` 回复。
- **备选 1**：服务端主动 ping、客户端 pong（需要新帧类型 + 新 pending 状态机）。**不选**：协议面变大且 dev v0 无收益；既有 `ping/pong` 已够表达「客户端仍在」。
- **备选 2**：不做空闲回收，只等 TCP 超时。**不选**：task 验收要求「异常连接能够清理（心跳超时/静默连接回收）」；且静默连接会占满 soft/hard outbound 预算，放大慢消费者问题。
- **后果**：`idle_timeout_s` 必须显著大于前端 keepalive 周期（默认 90s vs 25s，3.6 倍余量）；配置校验要求 `idle_timeout_s > 0`。

### ADR-5 前后端共享同一份 fixture，前端用 esbuild 就地打包 `protocol.ts` 执行断言

- **决策**：`tests/fixtures/chat_protocol_frames.json` 是唯一契约源。后端 `tests/test_web_chat_protocol_contract.py` 参数化消费；前端 `frontend/chat/scripts/protocol-contract.test.mjs` 用既有 devDependency `esbuild` 的 JS API 把 `src/protocol.ts` 打成内存 bundle（`write:false`）→ `data:` URL 动态 import → 对同一 fixture 断言常量/形状/解析/错误用例。新增 `npm run test:chat-protocol`。
- **备选 1**：引入 vitest。**不选**：为一次契约断言引入测试框架 + 配置 + lockfile 变更，超出 P0.5 需要。
- **备选 2**：前端只做类型声明、靠 tsc 保证。**不选**：类型不校验运行时形状与常量值，fixture 漂移不会被发现；task 要求「双向执行」。
- **后果**：C4 的 CI 证据需要同时跑 pytest 与 `npm run test:chat-protocol`；fixture 改动必须让两边同时通过。

### ADR-6 修复 `tests/test_chat_api.py` 收集失败：局部抑制第三方弃用告警

- **决策**：不改 `pytest.ini` 的全局 `-W error`（项目把告警当错误是刻意策略），而在 `tests/test_chat_api.py` 里用 `warnings.catch_warnings()` + `simplefilter("ignore")` 包住 `from fastapi.testclient import TestClient`。告警来自第三方 `anyio 4.15` 的 lazy alias（经 `starlette.testclient`），与仓库代码无关。
- **备选**：升级/降级 anyio 或 starlette。**不选**：改环境依赖会让本地与 CI 基线漂移，且不是本 task 的范围；`-W error` 覆盖 ini `filterwarnings`（已实测），ini 层无法局部豁免。
- **后果**：C4 的 Gateway 路由测试恢复可运行；抑制范围限定在 import 点，其它任何告警仍会使测试失败。
