# Task-05 — invitation auth + provisioning/readiness + admin/browser security（auth-provisioning-admin）

> 编号对应 PILOT_ROADMAP §5.9.10 第 5 项。状态标记复用 §8。

## 元数据

- **所属阶段**：主要里程碑 = P1（公网门禁）
- **§5.9 引用**：§5.9.3（auth/admin credential/浏览器安全）、§5.9.13（provisioning/readiness）、§5.9.9（test_accounts/access_tokens/auth_sessions 实体与约束）、§5.4 端点、§5.5 管理端、§10 DECIDED（Admin bootstrap）
- **§6 出口条件引用**：P1 出口「用户只输入一次 Token；首次进入完成一次性人设设置；刷新/重开浏览器仍能登录；越权请求全拒；重复兑换同一 Token 不产生多个账号」
- **状态**：planned

## 目标

实现受邀用户低摩擦登录 + 服务端身份派生：邀请 Token **一次性原子兑换** → HttpOnly `__Host-nexus_session` Cookie；普通/admin 分离 session；CSRF + Origin allowlist；401/403 契约（含错误响应不泄露存在性）；`pilot-admin` CLI（bootstrap/status/rotate-recovery-token/revoke-sessions/disable/enable，§10 DECIDED Admin bootstrap）；account 先 `provisioning`、tenant ready 后进 `active` 才签发 Token；pending/failed 可恢复、可幂等 retry；`suspended`/`revoked` 不删 partition 或历史数据，`revoked` 终止新 work。

## 输入

- 上游 change 产出：C1（account→tenant 映射 + canonical 表）、C4（WebChat channel 承载认证，E3）
- roadmap 冻结决策：§5.9.3（Cookie/CSRF/Origin/timeout/401-403）、§5.9.13（provisioning 生命周期）、§5.4（推荐端点）、§5.5（复用 Dashboard 为管理端）、§10 DECIDED（Admin bootstrap：token 轮换与 session 撤销独立操作）
- 现有代码锚点：`bootstrap/dashboard_api.py`（Dashboard 骨架）、`frontend/dashboard/src/api.ts`（前端 API 层）
- 依赖前置：C1 + C4(E3)

## 输出

- 端点：`/api/auth/*`（exchange/me/logout 等）、`/api/admin/*`（管理操作）
- DB schema：`access_tokens` / `auth_sessions` 表 + token/session **digest-only** 存储（带 pepper 的 HMAC-SHA-256，算法细节归 §10 OPEN FOR P-1 SPEC「Digest/encryption/key rotation」）
- 代码：一次性兑换 + Cookie 签发、provisioning jobs + readiness gate、admin CLI、admin audit
- 测试/证据：并发兑换测试、digest-only grep + 日志审计、错误码测试、CSRF/Origin 测试、provisioning 状态机测试、Cookie 隔离测试、CLI 测试、runbook 演练
- 交付物：recovery runbook（lost-token / suspected-leak / database-restore，§10 DECIDED）

## 验收标准

- [ ] 一次性兑换原子性：并发兑换同 Token 仅一个成功（§5.9.13「retry 不生成第二个 tenant/conversation/seed」） — 验证：并发兑换测试
- [ ] 明文 Token/session 不落库/不落日志/不落前端持久（digest-only） — 验证：grep digest-only + 日志审计
- [ ] 401（无 principal/session 过期）vs 403（封禁/capability 不足/资源非本 tenant）契约；错误响应不泄露存在性 — 验证：错误码负向测试
- [ ] mutation API 同时检查 Origin/Referer + session-bound CSRF token；WS handshake 校验 Cookie + Origin — 验证：CSRF/Origin 测试矩阵
- [ ] provisioning pending/failed 幂等 retry，ready 前不发 Token；pending 在进程启动时从 PG 恢复 — 验证：provisioning 状态机 + 重启恢复测试
- [ ] 普通/admin 凭据分离（`__Host-nexus_session` vs `__Host-nexus_admin`） — 验证：Cookie 隔离测试
- [ ] `pilot-admin` CLI 命令面完整且明文不进进程参数/仓库/配置/DB（local 强制恢复仅 trusted-host TTY） — 验证：CLI 测试 + grep
- [ ] runbook 演练：recovery token 丢失 / 疑似泄露 / 数据库恢复三条路径 — 验证：runbook 测试记录
- [ ] timeout 契约：普通 idle 7d / absolute 30d；admin idle 30min / absolute 12h（数值按 §10 PROPOSED DEFAULT 于 P-1 复核） — 验证：配置 + timeout 测试
- [ ] 本 task 不触碰 attachment（C6）、工具 allowlist 执行（C7） — 验证：PR diff 范围检查

> 判定「真正完成」而非「执行过」：并发兑换原子性、digest-only 存储、401/403 区分三条是安全闸门，必须有负向测试证据；runbook 演练需真实按步骤执行并记录。

## 独立性边界（不与其他任务重复）

- 本任务拥有：auth/admin 端点、token/session digest 存储、provisioning readiness、admin CLI、admin audit
- 本任务不触碰：attachment 上传/读取/清理（C6）、工具 allowlist 执行（C7）、Persona onboarding 流程本体（C9，只提供 principal）、Telegram binding（C10）、schedule 表（C11）
- 共享 seam 协议：为 C6/C7/C9/C10/C11/C14 提供 auth principal/session/provisioning（D3/E5）；token 轮换与 session 撤销实现为独立操作（§10 DECIDED，不相互隐式触发）

## 依赖

- **左依赖（必须先完成）**：C1（D1：account→tenant 映射）+ C4（E3：公网 WebChat = C4 通道 + C5 认证）
- **右依赖（本任务前置于）**：C6/C7/C10/C11（D3：5 是 6/7/10/11 对普通 tenant 开放的前置）、C9（E5：首次登录人设设置需 auth principal）、C14（D7：WebChat auth）
- **可并行**：C13（D6 独立）

## 风险与需冻结决策

- §10 OPEN FOR P-1 SPEC「Digest/encryption/key rotation」：token/session 只存 digest、tenant secret 静态加密已冻结，**具体算法、key source、rotation/recovery** 由本 task 与 C8 的 design 指定。
- §10 DECIDED「Admin bootstrap」：本地强制恢复只允许 trusted-host TTY；token 轮换默认不撤销有效 browser sessions；明文不写进程参数/仓库/配置/DB。
- 风险：provisioning 被第一轮 turn 隐式触发（§5.9.13 禁止）→ turn 入口保留 `require_ready()` fail-closed 仅作纵深防御；suspended/revoked 删数据 → 负向测试锁定「不删 partition/历史」。