## Why

当前 WebChat 登录只有「管理员先建号 → 签发一次性 Token → 用户粘贴 Token 兑换」一条路径：管理员要为每个用户手工建号、等 provisioning 收敛再发 Token，用户也没有可持续的登录凭据（Token 用完即弃）。Pilot 目标 10–30 个受邀账号，手工建号模式不可扩展到租户自助开通，也没有邮箱+密码这种常规登录方式。

## What Changes

- **邀请码语义变更**：管理员签发的邀请码不再要求预先存在 `active` 账号，改为携带预指定的**租户名**；用户凭「邀请码 + 邮箱 + 密码」自助注册，一次性消费该码并建立账号 + 租户。
- **新增注册流程**：`POST /api/auth/register`（邀请码 + 邮箱 + 密码）→ 单事务锁定并消费邀请码、创建 `provisioning` 账号与持久 provisioning job → 同步执行至 `active` → 直接建立登录会话。provisioning 失败走既有 job retry（幂等，不产生第二个 tenant），账号失败状态对管理员可见。
- **新增密码登录**：账号表增加 `email`（全局唯一）与 `password_digest`（argon2 哈希，不落明文）字段；`POST /api/auth/login`（邮箱 + 密码）校验后建立会话。错误响应不区分「邮箱不存在 / 密码错误 / 账号禁用」。
- **前端登录页替换**：`LoginPanel` 改为两个表单——「邀请码注册（邀请码 + 邮箱 + 密码）」与「邮箱密码登录」；移除纯 Token 兑换入口。**BREAKING**（面向用户：旧兑换入口从 WebChat 页面移除）。
- **后端兼容**：`POST /api/auth/exchange` 与 `pilot-admin` 签发接口保留（存量账号仍可用旧 Token 登录；新用户走注册）。管理员签发接口扩展为支持「租户名 + 一次性」模式。
- **密码策略**：最小长度等约束可配置（默认 8 位）；注册即生效，不做邮箱验证。

## Capabilities

### New Capabilities

（无新 capability；本改动为既有 `auth-provisioning` 能力的行为变更。）

### Modified Capabilities

- `auth-provisioning`: 邀请 Token 的签发前提从「账号已 active」放宽为「携带预指定租户名的租户邀请码（签发时无账号）」；新增邮箱+密码注册与登录凭据契约；一次性兑换语义扩展为「注册请求即消费」；账号模型新增 email/password 凭据生命周期。

## Impact

- **数据库**：新增 alembic 迁移 —— `test_accounts` 加 `email`（唯一索引，nullable 兼容存量）、`password_digest`（nullable）；`access_tokens.account_id` 改 nullable（签发时未兑现）并新增 `tenant_name` 列。
- **后端**：`bootstrap/auth/service.py`（签发/注册/登录方法）、`bootstrap/auth/api.py`（`/api/auth/register`、`/api/auth/login`、admin 签发签名扩展）、`bootstrap/auth/cli.py`（签发命令）、`bootstrap/db/repository/auth_repo.py`（token 签发/消费放宽账号前提、email/password 读写）、`bootstrap/db/repository/provisioning_repo.py`（注册流程同步 provisioning 收束）。
- **前端**：`frontend/chat/src/LoginPanel.tsx`、`frontend/chat/src/auth.ts`（register/login API 封装）；渲染登录/注册两个表单。
- **依赖**：新增 `argon2-cffi`（密码哈希）进 `requirements.txt` 与 `uv.lock`。
- **配置**：`[auth]` 增加密码策略参数（默认值即可，可空）。
- **兼容**：存量账号无 email/password，仍经既有 exchange 路径登录；无需数据回填。

## Non-Goals

- 不做邮箱验证、忘记密码/重置密码流程（Pilot 阶段明确排除，后续单独立项）。
- 不做「一个邀请码多人加入同一租户」的多用户租户模型（本 change 一个邀请码 = 一个租户，租户主即注册者）。
- 不迁移/强制存量账号补绑邮箱密码。
- 不引入邮件发送基础设施（SMTP 等）。
- 不改动 Telegram/QQ 等既有通道认证。