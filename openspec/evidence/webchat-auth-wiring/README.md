# webchat-auth-wiring 证据与边界

> change：`openspec/changes/2026-09-21-webchat-auth-wiring/`
> 承接 C4（`webchat-protocol-dev-loop`）与 C5（`auth-provisioning`）之间的接缝。

## 1. 落地内容（代码）

| 文件 | 变更 |
|---|---|
| `bootstrap/auth/identity.py` | **新**：`resolve_webchat_identity(runtime, session)` — 已认证 session 经 C1 身份链映射为 `account_id → tenant_id → canonical conversation_id`；fail-closed（无账号 / 无 canonical conversation 一律抛 `WebChatIdentityError`，**不回落** `DEFAULT_TENANT`） |
| `bootstrap/auth/runtime.py` | 暴露 `canonical_repo`（C5 的 `check_ws_handshake` docstring 已声明其返回 session「供 C4 通道派生 tenant 归属」） |
| `bootstrap/chat_api.py` | `/ws` 握手通过后按该 session 派生身份，失败 `close(4403)`；用户面 HTTP 端点（`/api/chat/sessions*`、`/uploads`、`/media`）加 `_require_user_session`（与 WS 同一套 Cookie+session+403 语义）；门禁改 ADR-3 三态；`static_root` seam（测试隔离构建产物，生产路径不变） |
| `bootstrap/app.py` | 通道启用门禁同步为 `auth.enabled or dev_mode` |
| `infra/channels/web_chat_channel.py` | `WebChatIdentity` 增 `chat_id`（session 路由键，dev 回退仍 `"local"`）；`_Connection` 承载本连接身份；`handle_websocket(identity=...)`；`_handle_send` 用 `conn.identity or self._identity`（缺省回退通道身份，保持改动前语义） |
| `frontend/chat/src/auth.ts` | **新**：邀请码兑换（仅 HttpOnly Cookie，无 localStorage/sessionStorage/URL/日志） |
| `frontend/chat/src/LoginPanel.tsx` | **新**：登录面板 |
| `frontend/chat/src/App.tsx` | `checking → anonymous(登录页) → authenticated(聊天)`；未登录不建 WS |
| `frontend/chat/src/connection.ts` | `hello.session_key` 驱动历史拉取（不再硬编码 `chat:local`）；`close(4401)` 或用户面 401/403 → 回登录入口 |
| `frontend/chat/src/store.ts` | `useChatRuntime(onUnauthorized?)`；回调放 ref 避免连接重建 |
| `Dockerfile` | 构建并复制 `static/chat`（此前用户面前端从未进镜像） |
| `config.example.toml` | 新增 `[auth]` 节 + `[channels.chat]` 门禁联动说明 |

## 2. 验证证据

| 文件 | 内容 |
|---|---|
| `pytest-webchat-auth.txt` | 定向集（auth_provisioning + webchat 通道/API/协议契约/门禁）：**98 passed, 28 skipped** |
| `pytest-regression.txt` | 全量 `tests/`：**1244 passed, 195 skipped**（改动前基线 1222 passed） |

跳过的 28 + 195 项均为 `postgres` marker（本地无 PG，按设计 skip）。

**前端侧验证（在部署服务器上执行，本机 DLP 环境无法运行 node/pyright）**：

- `docker build` 成功，日志可见 `npm run build:dashboard && npm run build:chat` 均通过；
  镜像内 `/app/static/chat/` 存在 `index.html` + hashed js/css。
- `tsc --noEmit` → **TSC_OK**（TS/TSX 类型检查通过）。
- `npm run test:chat-protocol` → **31/31 checks passed**（C4 协议契约 fixture 无回归）。

## 3. 关键断言（负向优先）

- 握手失败（无 Cookie / 会话无效 / 已撤销 / 账号 suspended / Origin 不在 allowlist）
  → `close(4401)`，**四类失败的 code 与 reason 逐字一致**（不泄露失败原因与账号状态），
  且 SHALL NOT 进入通道（零入队）。
- 凭据有效但账号无 canonical conversation → `close(4403)`，不回落默认租户。
- 客户端帧中声明的 `tenant_id` / `account_id` / `session_key` 不改变入队归属。
- 两个不同账号解析出不同 tenant/conversation（越权负向）。
- 非回环 host 需显式 `allow_public_bind`（auth 模式同样不豁免）。
- 只开 `[auth]` 时 `dev_mode` 必须保持 `False`（不得以打开 LLM payload 全量落盘为代价放行 WebChat）。

## 4. 未完成 / 明确边界

- **task 5.2（真实 uvicorn + 真实 Cookie 的端到端）未做**：需要真实 session，
  即需要 PostgreSQL；本地无 PG。计划在部署阶段（见下）于服务器上补做。
- **本 change 只解决认证闭环**。在 **C7（工具隔离）** 与存储切换完成之前，
  任何拿到 WebChat session 的主体即拥有本实例全部工具能力：
  生产 `config.toml` 的 `toolsets` 含 `spawn`，且 `agent/tools/shell.py` 存在。
  因此**不得对非 owner 开放**。
- 部署前置：C5 的 `AuthRuntime` 依赖 `config.storage.postgres_url`；当前生产
  `config.toml` 无 `[storage]` 节，走默认值 `…@localhost:5433/nexus`（该 PG 不存在），
  故启用 `[auth]` 前必须先起 PostgreSQL 并跑 `alembic upgrade head`。
- 容器部署注意：`[channels.chat]` 的 `host` 必须为 `0.0.0.0` 且
  `allow_public_bind = true`（Docker 端口发布走 DNAT 到容器 eth0，容器内 loopback
  不可达；来源层放行后真正的闸门是 `[auth]` 凭据 + 宿主侧反向代理）。

## 5. 环境备注

- 本机部分源文件曾被 DLP 透明加密（`%TSD-Header-###%`），现已解除；
  期间使用的 `git show` → 变换 → 写回 CRLF 工作流不再需要。
- 本机 `pytest.ini` 有 `addopts = -W error`，而本地 anyio 版本在导入
  `starlette.testclient` 时抛第三方 `DeprecationWarning`，会使相关文件收集失败
  （既有环境问题，与本次改动无关）。证据文件中的运行使用
  `-W "ignore::DeprecationWarning"` 覆盖。
- 本机 `pyright` 不可用（子进程读到 DLP 密文，报 `\ufffd` 假错）；类型检查在
  服务器侧 node 容器内以 `tsc --noEmit` 完成。
