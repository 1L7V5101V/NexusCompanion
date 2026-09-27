# WebChat 认证接线 — 实施任务

> 对应 proposal.md 的验收标准与 design.md 的 ADR-1..6；证据统一落
> `openspec/evidence/webchat-auth-wiring/`。
> 管理闭环：任务 checkbox → `openspec status` → evidence → 仅在有 evidence 时更新
> `PILOT_ROADMAP_PROJECT_CHECKLIST` 状态。

## 1. WS 握手认证（校验已接线，本节固化契约）

- [x] 1.1 **确认并锁定**现有接线：`bootstrap/chat_api.py` 的 `/ws` 在 `auth_runtime` 非空时于 `channel.handle_websocket` 之前调用 `check_ws_handshake`（Cookie `__Host-nexus_session` + Origin allowlist）；未启用 auth 时跳过（维持 C4 行为）。验证：代码路径断言 + 回归测试
- [x] 1.2 握手失败以协议级关闭表达（`close(4401)`），只区分 `auth` / `origin` 两类且不泄露具体原因；SHALL NOT 进入 `handle_websocket`、SHALL NOT 入队。验证：`tests/test_web_chat_ws_auth.py` 负向用例
- [x] 1.3 确认握手失败不产生 `InboundMessage`（零写入断言）。验证：bus 侧 mock 断言未调用
- [x] 1.4 若实际实现与 ADR-1/ADR-4 不一致（例如在通道内部重复校验或泄露原因），在 change 范围内收敛到本契约。验证：diff 范围检查

## 2. 身份派生

- [x] 2.1 启用 auth 时，`bootstrap/app.py` 构造通道不再传常量 `WebChatIdentity()`，改为按 session 经 C1 解析得到 `account_id → tenant_id → canonical_conversation_id`。验证：集成测试
- [x] 2.2 `hello` 在认证模式下返回解析三元组；客户端帧中 `tenant_id` / `account_id` / `session_key` 仍被忽略。验证：负向测试
- [x] 2.3 跨账号越权负向：账号 A 的 session 无法读到账号 B 的 tenant/会话。验证：双账号集成测试
- [x] 2.4 未启用 auth 时保持显式 dev 回退身份（`DEV_ACCOUNT_ID` / `DEFAULT_TENANT`），且不因认证不可用而静默降级（design ADR-6）。验证：门禁/回退测试

## 3. 门禁与配置

- [x] 3.1 `bootstrap/app.py` 与 `bootstrap/chat_api.py` 的门禁改为 design ADR-3 三态矩阵：`auth.enabled` → 允许；否则 `dev_mode` → 允许；否则 fail-fast。验证：八组合矩阵测试
- [x] 3.2 非回环 host 绑定仍拒绝，除非显式 `allow_public_bind`（C4 语义不变）。验证：启动拒绝测试
- [x] 3.3 `config.example.toml` 写清 `[auth]` 与 `[channels.chat]` 联动，含 `origin_allowlist` 必须覆盖实际来源（公网部署时为该域名）。验证：文档审查 + 配置断言
- [x] 3.4 断言认证模式不打开 `payload_snapshot`（不得以 `dev_mode=true` 作为生产放行手段）。验证：配置/装配断言

## 4. 前端构建与登录入口

- [x] 4.1 `frontend/chat` 新增邀请 Token 登录流程：提交 Token → `POST /api/auth/exchange` → 依赖 HttpOnly Cookie；SHALL NOT 落 localStorage/sessionStorage/URL/日志（design ADR-5）。验证：前端契约测试 + 静态检查
- [x] 4.2 刷新/重开浏览器凭 Cookie 重新建立 WS；未登录时只呈现登录入口，不泄露会话数据。验证：e2e
- [x] 4.3 `static/chat` 纳入构建（`package.json` 的 `build` 已含 `build:chat`；确认 `Dockerfile` 产出 `static/chat` 并与 `static/dashboard` 同级）。验证：镜像内路径存在性断言
- [x] 4.4 WS 端点路径与前端一致（`/ws`），且 C4 的协议帧契约测试仍双向通过。验证：`test:chat-protocol`

## 5. 测试、证据与回归

- [x] 5.1 握手负向矩阵：无 Cookie / 无效 / 过期 / 已撤销 / 账号 suspended / Origin 不在白名单 → 全部拒绝且零入队
- [x] 5.2 端到端：真实 uvicorn + 真实 WebSocket 客户端 + 真实 Cookie：exchange → handshake → hello → 收发一轮（含流式与终态帧）——部署后于服务器实跑（见 evidence `deploy-e2e.txt`）
- [x] 5.3 回归：C4 既有 dev-only 测试全绿；`pytest -q -W error tests/`；`pyright --level error` 对齐 main 基线
- [x] 5.4 `openspec validate 2026-09-21-webchat-auth-wiring --strict` 通过
- [x] 5.5 运维边界写入证据与 checklist：明确「本 change 只解决认证闭环；对外部用户开放 MUST 等 C7 工具隔离 + 存储切换」
