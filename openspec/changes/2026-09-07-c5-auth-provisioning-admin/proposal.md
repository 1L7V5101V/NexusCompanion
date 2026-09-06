# C5 invitation auth + provisioning/readiness + admin/browser security

> 对应 `openspec/openspec-tasks-bundle/task-05-auth-provisioning-admin.md`（auth seam：principal/session/provisioning）。

## Why

Pilot 面向 10–30 个受邀测试账号开放 WebChat（PILOT_ROADMAP §1）。当前 WebChat 仅 dev 单用户
`chat:local`（DEFAULT_TENANT），没有认证、邀请 Token、tenant-bound principal、CSRF/Origin 和账号
管理能力。P1 公网门禁（§6）要求：用户只输入一次 Token；越权请求全拒；重复兑换同一 Token 不产生
多个账号。本 change 提供下游 C6/C7/C9/C10/C11/C14 依赖的 **auth seam**（principal/session/provisioning）。

## What Changes

- **DB schema**（新 Alembic revision，revises `f3c8a9d2e7b4`）：`access_tokens`、`auth_sessions`、
  `admin_credentials`、`admin_audit_events`、`tenant_provisioning_jobs` 五表；token/session 仅存
  带 pepper 的 HMAC-SHA-256 digest（digest-only）。
- **认证服务**（`bootstrap/auth/`）：邀请 Token 签发/**一次性原子兑换**→ HttpOnly
  `__Host-nexus_session` Cookie；普通/admin 分离 session（`__Host-nexus_admin`）；session-bound
  CSRF；Origin/Referer allowlist；401/403 契约（错误不泄露存在性）；idle/absolute timeout。
- **Admin 能力**：`pilot-admin` CLI（bootstrap/status/rotate-recovery-token/revoke-sessions/disable/
  enable，§10 DECIDED Admin bootstrap）；admin HTTP API（测试账号签发/查询、suspend/unsuspend/
  revoke、Token 撤销），默认仅本机回环访问；admin audit。
- **Provisioning/readiness**：账号创建即 `provisioning`，job（partition + canonical conversation）
  ready 后才进 `active` 并允许签发 Token；pending/failed 从 PG 恢复、幂等 retry；
  `suspended`/`revoked` 不删数据、`revoked` 拒绝新 work。
- **接入缝**：`/api/auth/*` 挂到 WebChat Gateway（`bootstrap/chat_api.py`），`/api/admin/*` 挂到
  Dashboard API（`bootstrap/dashboard_api.py`）；`[auth]` 配置节 + `enabled` 总开关（Enable 步），
  默认关闭，dev 闭环行为不变。

## 冻结输入（不在本 change 重新决策）

- §5.9.3（Cookie/CSRF/Origin/timeout/401-403/Admin CLI 语义/runbook）
- §5.9.13（provisioning 生命周期）、§5.9.9（实体清单与约束）、§5.4 端点、§5.5 管理端
- §10 DECIDED（Admin bootstrap：本地强制恢复仅 trusted-host TTY；token 轮换默认不撤销 browser
  sessions；明文不写进程参数/仓库/配置/DB）、§10 OPEN FOR P-1 SPEC「Digest/encryption/key
  rotation」由本 change design §ADR-2 指定算法与 key source。

## 范围边界（不触碰）

- attachment 上传/读取/清理（C6）；工具 allowlist 执行（C7）；Persona onboarding 流程本体
  （C9，只提供 principal）；Telegram binding（C10）；schedule（C11）；WebChat 协议帧契约
  （C4，本 change 只提供 WS handshake 认证 guard 供其消费）。

## 验收标准

复刻 task-05「验收标准」清单（并发兑换原子性、digest-only、401/403、CSRF/Origin 矩阵、
provisioning 状态机与重启恢复、Cookie 隔离、CLI 命令面、runbook 演练、timeout 契约、
PR diff 范围检查），证据落 `openspec/evidence/c5-auth-provisioning-admin/`。
