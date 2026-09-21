# WebChat 认证接线 — auth-gated channel + session-derived identity + chat bundle

> 承接 C5（`auth-provisioning`，合并 `0753628`，change 归档 `changes/archive/2026-09-07-c5-auth-provisioning-admin/`）与 C4（`webchat-protocol-dev-loop`，归档 `changes/archive/2026-09-20-c4-webchat-protocol-dev-loop/`）之间的**未认领接缝**。
> 输入的已冻结决策（PILOT_ROADMAP §5.9.1 服务端派生身份 / §5.9.3 Cookie+CSRF+Origin / §5.9.4 WS 握手、§5.6 双入口）不在此重复论证，design.md 逐条引用。

## Why

C5 交付了认证与授权的**后端**（邀请 Token、session、admin API、provisioning），并把 WS 握手的**校验函数** `bootstrap/auth/ws_guard.py::check_ws_handshake` 接在了 `bootstrap/chat_api.py` 的 `/ws` 入口。但身份侧与启用侧未接上，导致「用户只输入一次 Token」这一 P1 出口条件在实现上不可达：

- **通道层身份接线缺失（真正缺口）**：WS 凭据校验**已经接线**（`chat_api.py` 的 `/ws` 在 `auth_runtime` 存在时调用 `check_ws_handshake`，失败 `close(4401)`）。但**通道层**的身份/tenant 派生未接线：`bootstrap/app.py` 仍以 `identity=WebChatIdentity()`（即 `DEV_ACCOUNT_ID` / `DEFAULT_TENANT` 常量）构造通道，因此连接通过校验后仍拿到单用户 dev 三元组，`hello` 与登录者无关。`chat_api.py` 源码注释亦自述「C4 通道层持久/tenant 派生接线由 C4 消费（本层只做 app 入口门禁）」——而 C4 已归档且为 dev-only，故该接缝**无任何 change 认领**。
- **启动门禁仍为 dev-only**：`bootstrap/app.py` 与 `bootstrap/chat_api.py` 均要求 `agent.dev_mode=true` 才允许启用 `channels.chat`。而 `dev_mode=true` 会顺带打开 `bootstrap/providers.py` 的 `payload_snapshot_enabled`（LLM 请求/响应全量落盘），不能作为生产后门。
- **前端从未构建**：`frontend/chat` 存在但 `static/chat` 在生产宿主机与容器内均不存在（容器内 `static/` 只有 `dashboard`）。C4 交付了协议与前端源码，但没有构建产物，也没有登录入口。
- **集成路径从未跑通**：C5 的部署态演练自述「canary 未启用 chat 通道，`/api/auth/*` 未挂载」——即 chat 承载认证这条路径**从未被真实执行过**。

结果：`dashboard` 侧 `/api/admin/*`（回环专用）已可运行；用户侧 WebChat 因「门禁仍要求 `dev_mode` ∧ 通道层身份未派生 ∧ 前端从未构建」而不可达——即「用户只输入一次 Token」在实现上不成立（尽管 WS 入口已校验凭据）。

## What Changes

- **WS 认证接线校验与补齐**：确认并固化 `bootstrap/chat_api.py` 的 `/ws` 入口在 `accept` 之前调用 `check_ws_handshake`（Cookie `__Host-nexus_session` + Origin allowlist），失败以协议级拒绝关闭（`auth` / `origin` 两类，不泄露具体原因）且不做任何入队；把该行为提升为可验收契约（补负向矩阵），并确认未启用 auth 时不引入门禁。
- **身份从 session 派生（本 change 的主体缺口）**：启用 `[auth]` 时，`WebChatIdentity` SHALL 由已认证 session 经 C1 身份链解析得到 `account_id → tenant_id → canonical conversation`，`hello` 返回该三元组；客户端帧中的归属字段仍一律忽略。`dev_mode`（无 auth）保留显式单用户 dev 身份作为回退，语义与 C4 一致。**这是 `chat_api.py` 注释中显式留给「C4 消费」而实际无人承接的那一段。**
- **门禁改为认证驱动**：`channels.chat.enabled` 在 `auth.enabled=true` 时允许启用（不再要求 `dev_mode`）；非回环 host 绑定仍拒绝，除非显式 `allow_public_bind`。`auth.enabled=false` 时维持 C4 的 dev-only 行为（要求 `dev_mode`），不得静默降级。
- **chat 前端构建与登录入口**：构建 `static/chat` 并纳入镜像构建；前端新增邀请 Token 登录流程（提交 Token → `POST /api/auth/exchange` → HttpOnly Cookie → 建立 WS），不再依赖任何客户端持有的长期凭据。
- **配置与文档**：`[channels.chat]` 与 `[auth]` 的联动在 `config.example.toml` 写清；`origin_allowlist` 必须包含部署实际来源（含公网域名），否则握手/变更请求被拒。
- **测试与证据**：握手负向矩阵（无 Cookie / 过期 / 撤销 / Origin 不在白名单）、身份派生负向（跨账号越权、客户端声明归属无效）、门禁矩阵（dev/auth/两者皆无）、真实 uvicorn + 真实 WebSocket + 真实 Cookie 的端到端登录闭环。

## Capabilities

### New Capabilities

- 无。本 change 不引入新能力，只接通既有两份规格之间的接缝。

### Modified Capabilities

- `webchat-protocol-dev-loop`：
  - **MODIFIED** `dev-only 暴露门禁` → 改为认证驱动的暴露门禁（`auth.enabled` 即满足认证前提；dev-only 仅作为无 auth 时的回退，且仍需 `dev_mode`）。
  - **MODIFIED** `服务端派生身份与客户端归属不可信` → 补明启用认证时身份 SHALL 来自已认证 session；dev 身份仅是无 auth 时的显式回退。
- `auth-provisioning`：
  - **ADDED** `WebSocket 握手认证` → 把 C5 已实现的 guard 提升为通道边界的强制契约（Cookie + Origin，fail-closed，协议级拒绝且不泄露原因）。

## 范围边界（不触碰）

- **不做 C7 工具隔离**：不改工具 allowlist / `ToolExecutionContext` / 危险工具关闭。**本 change 后 WebChat 仍只允许「受信单一 owner」使用**，不得对非 owner 开放（见下节风险）。
- **不做存储切换**：不动 SQLite → PostgreSQL cutover；生产 `[storage]` 仍为 legacy SQLite 路径。
- **不做 C6 / C9 / C10 / C14**：附件、Persona onboarding、Telegram binding、memory engine selector 均不在范围。
- **不做公网暴露决策**：反向代理 / 证书 / 域名 / 边缘认证属运维动作，不在本 change（本 change 只保证应用层认证闭环成立）。

## 验收标准

- [ ] 未携带有效 session Cookie 的 WebSocket 握手被拒且零入队（负向测试）
- [ ] `__Host-nexus_session` 过期 / 被撤销 / 账号 suspended 时握手被拒（负向测试）
- [ ] Origin 不在 `origin_allowlist` 时握手被拒（负向测试）
- [ ] 启用认证时 `hello` 返回的 `account_id` / `tenant_id` / `conversation_id` 由该 session 派生，且两个不同账号互不可见（越权负向测试）
- [ ] 客户端帧中声明的 `tenant_id` / `account_id` / `session_key` 不改变入队归属（负向测试）
- [ ] `auth.enabled=true` 且 `channels.chat.enabled=true` 时无需 `dev_mode` 即可启动；`auth.enabled=false` 时仍要求 `dev_mode`（门禁矩阵测试）
- [ ] `static/chat` 可构建且镜像内存在；未登录访问只得到登录入口，不泄露会话数据
- [ ] 端到端：邀请 Token → exchange → Cookie → WS 握手 → `hello`（真实 uvicorn + 真实 WebSocket 客户端）
- [ ] `dev_mode` 不再被用作生产放行手段；`payload_snapshot` 保持关闭（验证：配置断言）
- [ ] `openspec validate --strict` 通过

## Impact

- **代码**：`bootstrap/app.py`（**主体**：身份解析 + 门禁）、`bootstrap/chat_api.py`（握手校验已存在；本节补契约断言与门禁）、`agent/config_models.py` / `agent/config.py`（门禁相关字段语义）、`bootstrap/auth/`（如需导出 session→身份解析入口）。
- **前端**：`frontend/chat/`（登录流程 + 凭据处理）、`package.json`（`build:chat` 纳入构建）、`Dockerfile`（构建 `static/chat`）。
- **测试**：新增握手/身份/门禁/端到端测试；扩展 `tests/fixtures/chat_protocol_frames.json`（如握手拒绝需要新 close code 向量）。
- **配置**：`config.example.toml` 增补 `[auth]` 与 `[channels.chat]` 联动说明。
- **行为变化**：启用认证后 `channels.chat` 可在非 dev 模式启动（此前启动即失败）；WS 从「无认证」变为「必须携带有效 session」。
- **风险（必须写入运维文档）**：本 change 只解决**认证闭环**。在 C7（工具隔离）与存储切换完成之前，**任何获得 WebChat session 的主体即拥有当前实例的全部能力**（含 `shell` / `spawn` / `mcp` toolsets，见生产 `config.toml:127` `toolsets = ["meta_common", "spawn", "schedule", "mcp"]`）。因此本 change 的对外部用户开放**不在范围内**。
