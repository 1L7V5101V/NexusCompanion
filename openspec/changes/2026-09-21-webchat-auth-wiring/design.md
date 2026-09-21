# WebChat 认证接线 — Design

> 本 change 只接通 C4（通道/协议）与 C5（认证）之间的接缝，不重新决策两者已冻结的语义。
> 引用：PILOT_ROADMAP §5.9.1（服务端派生身份）、§5.9.3（Cookie/CSRF/Origin/401-403）、§5.9.4（WS 生命周期）、§5.6（双入口）。

## 1. 现状与目标接缝

```
邀请 Token ──POST /api/auth/exchange──▶ HttpOnly __Host-nexus_session   （C5，已实现）
                                              │
                                              │  ① 缺失：握手不校验
                                              ▼
浏览器 ──WebSocket /ws──▶ chat 通道 ──▶ InboundMessage(归属?)            （C4，dev-only）
                                              ▲
                                              │  ② 缺失：identity 硬编码
                                        WebChatIdentity(DEV_ACCOUNT_ID, DEFAULT_TENANT)
```

本 change 填 ① 与 ②，并把驱动门禁从 `dev_mode` 换成 `auth.enabled`。

## 2. ADR

### ADR-1 握手校验发生在通道边界，不在 ASGI 中间件

`check_ws_handshake` SHALL 在通道 `accept` **之前**调用，参数为握手 HTTP 头与查询串。

理由：C4 的门禁中间件（`_DevOnlyGuardMiddleware`）只按客户端地址判回环，与凭据无关；把凭据校验塞进同一中间件会让两类拒绝（来源 / 凭据）语义混淆，且 C5 design ADR-4 已把「Cookie + Origin」定义为**通道 accept 前**的调用契约。保留两层并存：来源门禁（可选，`allow_public_bind`）与凭据门禁（强制）。

### ADR-2 身份派生经由 C1 解析器，不由通道自行拼装

启用认证时，握手中的 session 记录 SHALL 经 C1 的身份链解析得到 `account_id → tenant_id → canonical_conversation_id`，再构造 `WebChatIdentity`。

理由：§5.9.1 要求 tenant 只能由服务端可信身份派生；`infra/storage/tenancy.py` 已声明「本模块是唯一合法的派生入口，业务代码不得自行解析 session key 反推 tenant」。通道传入 `WebChatIdentity` 是既有 seam（`bootstrap/app.py` 已这样构造），本 change 只是把「常量」换成「解析结果」，不新增派生路径。

### ADR-3 门禁语义：`auth.enabled` 取代 `dev_mode` 作为放行条件

| `auth.enabled` | `dev_mode` | `channels.chat.enabled=true` 结果 |
|---|---|---|
| true | 任意 | **允许**（凭据门禁生效，身份来自 session） |
| false | true | 允许（C4 原语义：dev 单用户回退身份） |
| false | false | **启动 fail-fast**（维持 C4 负向语义） |

理由：C4 的门禁注释本身写明「P1 认证与 tenant 隔离落地前不得启用」；C5 即为 P1 认证。因此把前置条件从「处于 dev」改为「已有认证」是语义对齐，而非放宽。`dev_mode` 不再承担放行职责，避免为了让 WebChat 上线而打开 `payload_snapshot`（LLM 全量落盘）。

### ADR-4 拒绝语义：协议级关闭 + 不泄露原因

握手失败 SHALL NOT 完成 `accept`；SHALL 以协议级拒绝表达，且只区分 `auth`（凭据无效）与 `origin`（来源禁止）两类，不回显「Cookie 不存在 / 过期 / 已撤销 / 账号被封」等细节。

理由：与 C5 的 401/403「不泄露存在性」一致（`auth-provisioning` 的 `浏览器安全边界` 要求）；避免把 WebChat 变成账号状态探测器。

### ADR-5 前端只持有 HttpOnly Cookie

前端登录 SHALL 只通过 `POST /api/auth/exchange` 建立会话；SHALL NOT 把 Token 或 session 写入 `localStorage` / `sessionStorage` / URL / 日志；刷新后凭 Cookie 重新建立 WS。

理由：C5 已把 `__Host-` 前缀 + HttpOnly + CSRF 作为冻结的安全边界（ADR-4）。前端引入任何长期凭据都会绕过该边界。

### ADR-6 dev 回退身份是显式路径，不是隐式默认

无 `[auth]` 时，通道使用显式 dev 身份（`DEV_ACCOUNT_ID` / `DEFAULT_TENANT`），并在启动日志与 `hello` 中保持可识别；SHALL NOT 在启用认证的实例上回退到该身份（失败即拒绝）。

理由：避免「认证服务不可用 → 静默降级为 dev 单用户」这种在高价值实例上最危险的失效模式。

## 3. 接口与配置变化

- `[auth]`：新增/明确 `enabled`（放行 chat 的前置）、`origin_allowlist`（须含实际来源，公网部署时为该域名）。
- `[channels.chat]`：`enabled` 的语义由「dev-only」变为「auth-gated 或 dev-only」；`host` 默认仍 `127.0.0.1`（生产由反向代理前置）；`allow_public_bind` 语义不变（非回环绑定的显式许可）。
- `hello`：`account_id` / `tenant_id` / `conversation_id` 在启用认证时来自 session 解析结果。
- `static/chat`：成为镜像构建产物（与 `static/dashboard` 同级）。

## 4. 测试策略

| 面 | 用例 |
|---|---|
| 握手负向 | 无 Cookie / Cookie 无效 / session 过期 / session 已撤销 / 账号 suspended / Origin 不在白名单 → 全部拒绝且零入队 |
| 握手正向 | 有效 session + 合法 Origin → 完成 accept，`hello` 三元组等于解析结果 |
| 身份派生负向 | 账号 A 的 Cookie 不得读到账号 B 的 tenant；客户端帧声明 `tenant_id`/`account_id`/`session_key` 不改变归属 |
| 门禁矩阵 | (auth, dev, enabled) 八种组合的启动结果断言，含「非回环 host 未显式允许 → 拒绝启动」 |
| 端到端 | 真实 uvicorn + 真实 WebSocket + 真实 Cookie：exchange → handshake → hello → 收发一轮 |
| 回归 | `dev_mode` 路径行为不变（C4 既有 dev 测试全绿）；`payload_snapshot` 在认证模式保持关闭 |

## 5. 风险

- **本 change 不等于「可开放给他人」**：认证闭环成立 ≠ 授权边界成立。工具隔离（C7）未落地前，持有效 session 者拥有实例全部工具能力（生产当前 `toolsets` 含 `spawn`，`agent/tools/shell.py` 存在）。对外部用户开放 MUST 等 C7 与存储切换。
- **首次真实运行该路径**：C5 演练自述 chat 通道未启用，因此 chat + auth 的组合是首跑；端到端用例是主要缓解。
- **改 public 门禁属安全相关变更**：需要独立 review，并把 ADR-3 的三态矩阵写成负向测试，避免后续被误简化为「只要 enabled 就放行」。
