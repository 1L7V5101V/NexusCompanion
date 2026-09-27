# Design — 邀请码租户注册（一次性邀请码 = 一个租户）

## Context

现状（见 `openspec/specs/auth-provisioning/spec.md`）：邀请 Token 必须绑定**已 active 的账号**，
签发前管理员须先建号 + 等 provisioning 收敛；用户粘贴 Token 一次性兑换即登录，无邮箱/密码凭据。
本 change 把「邀请码」从「已存在账号的兑换钥匙」扩展为「租户自助开通的注册凭证」：
管理员签发时预指定租户名（不建号），用户凭「邀请码 + 邮箱 + 密码」注册，一次调用完成
消费邀请码 + 建号 + provisioning + 登录。

关键约束（既是机会也是限制）：
- 现有 `access_tokens.account_id` 为 NOT NULL FK，签发强依赖账号存在 —— 本 change 的核心数据模型改动。
- provisioning 是持久 job + 幂等 executor seam（`CanonicalAgentExecutor`），注册需复用而非另起炉灶。
- 会话/CSRF/Cookie 契约（§5.9.3）与 browser 边界不因凭据类型变化，必须复用同一套。
- 兼容面：存量账号（Pilot 中 1 个使用者）无 email/password，仍需经既有 exchange 路径登录。

## Goals / Non-Goals

**Goals**
- 一次性邀请码 = 一个租户：签发免费账号模型，注册时三合一（消费 + 建号 + provisioning + 会话）。
- 邮箱全局唯一、密码 argon2 哈希、登录与兑换共用同一会话体系。
- 存量账号不受影响，不回填数据。

**Non-Goals（设计级）**
- 不引入多用户共租户模型（一个租户一个账号，注册者即租户主）。
- 不做邮箱验证/找回/改密（Pilot 范围外，另有 Non-Goal 记录）。
- 不改动 Telegram 等通道认证。
- 不做「给存量账号补设邮箱密码」。若未来需要，另行迁移。

## Decisions

### D1: 复用 `access_tokens` 表承载租户邀请码，`account_id` 改 nullable + 新增 `tenant_name`

- 签发时：`account_id=None`、`tenant_name=<管理员指定>`、digest-only、一次性。
- 注册（消费）时：单事务「锁定 Token 行 → 校验 → 创建账号 + provisioning job → 回填
  `account_id`、标记 consumed」。
- 原文对照 `consume_token_for_session`（锁行 → 标记消费 → 建会话）扩展为「注册消费」变体。

> 备选 1：新建独立 `tenant_invites` 表。→ 拒绝：与 `access_tokens` 的 digest/revoke/audit
> 逻辑重复，且消费语义（锁行+一次性）完全一致，拆表徒增迁移与维护。
> 备选 2：签发时即建 `provisioning` 账号再绑 token。→ 拒绝：把「租户开通」提前到签发侧，
> 与 spec「签发不要求账号存在」矛盾，且 create-account 的重试/失败语义侵入签发路径。

**迁移**：
- `test_accounts`：`+ email VARCHAR UNIQUE NULL`、`+ password_digest TEXT NULL`。
  存量行两者皆 NULL，行为同旧版。
- `access_tokens`：`account_id` 解除 NOT NULL（FK 保留）；`+ tenant_name VARCHAR NULL`
  （旧 token 为 NULL，走旧兑换逻辑）。
- 因 `issue_token` 原签名要求 account_id，需新增/改造 repo 方法支持
  `issue_tenant_invite(tenant_name=...)`（account_id 为空）。

### D2: 注册 = 单事务消费 + 幂等 provisioning 收束 + 会话，失败路径与 Admin create-account 对齐

注册流程（`AuthService.register_tenant`）：
1. 校验 Origin（全 mutation 通用，见既有 `_require_origin`）；校验 email 格式与密码策略。
2. `repo.register_from_invite(...)`：单事务 `SELECT ... FOR UPDATE` 锁定 token 行 →
   校验未消费/未撤销/未过期 → 校验 email 全局唯一（`test_accounts.email` 唯一索引兜底，
   IntegrityError → 统一认证错误）→ 创建 `provisioning` 账号（`email`、`password_digest`、
   `display_name` 默认取自 email 前缀或 tenant_name）→ 创建 pending provisioning job →
   回填 token.account_id + consumed_at。此事务只做开账，不执行 provisioning。
3. 复用 `ProvisioningService.run_pending(max_jobs=16)`（与 `POST /api/admin/test-accounts`
   相同的同步收束 seam）：job `ready` → 账号 `active`。
4. 账号 active 后创建登录会话（复用 `consume` 之后的 `create_user_session` 路径）；
   会话 Cookie 与 timeout 契约 = 现有 §5.9.3。
5. 失败语义：
   - token 无效/已用/过期 → `CredentialExchangeError` → 401 统一文案（不泄露原因）。
   - provisioning 失败 → 账号 `failed`、邀请码已消费 → 返回 409/422 + job 失败原因
     （仅对管理员可见，见 spec 场景）；管理员对同一 job retry（幂等）。

> 备选：注册事务内同步跑 provisioning。→ 拒绝：provisioning executor 可能慢/IO（分区 DDL +
> canonical agent），事务持有 token 行锁 + job 锁时间过长；且失败回滚整个事务会让「消费邀请码
> 但没建成租户」的用户得到可用邀请码，与「一次性 + retry 语义」不符。现有 admin 流程也是
> 事务内只开账、事务外 run_pending，保持一致。

### D3: 密码哈希 = argon2（`argon2-cffi`），账号表存哈希

- 新增依赖 `argon2-cffi`（requirements.txt + uv.lock）。
- `AuthService` 增加 `hash_password`/`verify_password` 封装；`password_digest` 列存
  argon2 编码串（自含 salt/参数）。
- 登录失败统一一条错误路径（邮箱不存在 / 密码错 / 禁用 → 同一 `CredentialExchangeError`），
  杜绝枚举。为规避 timing oracle：邮箱不存在时也执行一次无意义 verify（恒定时间，
  burn a dummy digest）。

> 备选：`bcrypt`/`pbkdf2`。→ argon2（OWASP 当前推荐，argon2id 默认参数）即可；项目
> 已有 pyproject 生态无 bcrypt 依赖，argon2-cffi 纯 wheel 无编译痛点。

### D4: 登录 = 独立新端点；两个端点共享会话创建 seam

- `POST /api/auth/login`（邮箱 + 密码，Origin 校验，免 CSRF——与 exchange 相同：会话前无
  session-bound CSRF 可 bind，仅 Origin）+200 会话 Cookie。
- `POST /api/auth/register`（邀请码 + 邮箱 + 密码，同免 CSRF 仅 Origin）+200 会话 Cookie。
- 二者复用同一 `create_user_session`（现有 `consume_token_for_session` 的会话创建部分抽出自用）。
- API 层新增 `RegisterRequest`/`LoginRequest`（pydantic，长度/格式约束），响应轮廓与
  exchange 一致 `{session_id, account_id}`。

### D5: 存量路径不动

- `POST /api/auth/exchange` 与 `pilot-admin` 签发/兑换保留原语义；旧 token（tenant_name NULL）
  走旧路径，新租户邀请码（account_id NULL + tenant_name）走注册路径。
- 前端登录页把「粘贴邀请码」换成「注册表单 + 登录表单」；无 email/password 的存量账号
  理论上无法从前端登录 —— 接受（Pilot 仅 1 个使用者，管理员侧工具不受影响），并记录于风险。

## Risks / Trade-offs

- [存量账号（无 email/password）经前端不可登录] → 前端移除纯 Token 兑换入口是 **BREAKING**
  变更；Pilot 现有唯一使用者 Malix 由会话 Cookie 维持登录（7d idle / 30d absolute），
  到期后需管理员补发邀请码或经 exchange API 换新会话。已在 proposal 标注 BREAKING 与兼容说明。
- [前端无密码重置，用户忘密即锁死] → Pilot 范围明确 Non-Goal；管理员可 suspend/revoke 并
  重新发码。Pilot 用户量（10–30）可控。
- [注册时同步 run_pending 可能拉长请求] → 与其他 admin API 相同限制；provisioning 幂等，
  超时后账号 `failed`，管理员 retry。不做请求内异步（会引入「注册成功但没登录会话」的中间态）。
- [argon2 依赖进部署镜像] → Dockerfile 用 pip 安装 requirements.txt，argon2-cffi 为
  manylinux wheel，无编译需求；构建 CI 会覆盖（本 change 不涉部署，仅依赖声明）。
- [email 唯一性竞态] → 依赖 `test_accounts.email` 唯一索引兜底 + 事务回滚，
  并发注册同邮箱仅一个成功（IntegrityError → 统一认证错误）。

## Migration Plan

1. **迁移**：新 alembic revision（`test_accounts.email/password_digest`、`access_tokens.account_id
   nullable + tenant_name`）。存量行自动兼容（均为 NULL / 保留原值）。
2. **部署顺序**：代码 + 迁移一起发布（迁移前旧代码不引用新列，迁移后新代码才读）——
   与既有 alembic 流程一致（`scripts/migrate/alembic_util.upgrade_head`）。
3. **回滚**：降级迁移删列 + 前端回退到纯 Token 兑换版本即可（旧代码不感知新列；
   已注册新账号有 email/password 但 exchange 路径不受影响）。没有不可逆数据变更。
4. **验证**：pytest 新用例（注册/登录/并发/唯一性/provisioning 失败 retry/不泄露文案）+
   部署态 curl 冒烟。

## Open Questions

无（与用户确认的 4 项决策 + 上表设计决策已覆盖所有会影响 spec/任务拆解的未知）。