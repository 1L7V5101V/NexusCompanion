# Tasks — 邀请码租户注册

> 参考：`openspec/change/invite-code-tenant-registration/` 的 proposal / design / specs。
> 规格契约由 `specs/auth-provisioning/spec.md` 定义（MODIFIED 邀请 Token 原子兑换 + ADDED 邮箱密码注册登录）。
> 完成后同步更新 `PILOT_ROADMAP_PROJECT_CHECKLIST.md` 并归档本 change。

## 1. 依赖与数据模型

- [x] 1.1 在 `requirements.txt` 加 `argon2-cffi`，运行 `uv lock` 更新 `uv.lock`；验证 `pip install -e .`/`uv sync` 后 `import argon2` 成功
- [x] 1.2 新增 alembic 迁移：`test_accounts` 加 `email VARCHAR(255) UNIQUE NULL`、`password_digest TEXT NULL`；`access_tokens.account_id` 解除 NOT NULL（FK 保留）、加 `tenant_name VARCHAR(255) NULL`；验证 `alembic upgrade head` 成功且不影响存量行
- [x] 1.3 在 `bootstrap/db/models/canonical.py`（TestAccountModel）与 `bootstrap/db/models/auth.py`（AccessTokenModel）同步新字段；验证模型可 autogenerate 且 pyright 通过
- [x] 1.4 为既有 auth 测试跑通（`pytest -q -W error tests/auth_provisioning/`）确认无回归

## 2. 仓库层（CredentialRepository / ProvisioningRepository）

- [x] 2.1 `auth_repo.py` 新增 `issue_tenant_invite(tenant_name, token_digest, ...)`：account_id 为 NULL、校验 tenant_name 非空；验证签发返回明文、digest 落库、不使用任何账号
- [x] 2.2 `auth_repo.py` 新增 `register_from_invite(...)`：单事务 FOR UPDATE 锁 token 行 → 校验未消费/未撤销/未过期 → 校验 email 全局唯一（唯一索引兜底，IntegrityError 转统一认证错误）→ 创建 `provisioning` 账号（email/password_digest/display_name）→ 创建 pending provisioning job → 回填 token.account_id + consumed_at；验证：并发同码仅一个成功、同 email 仅一个成功、消费后重放失败
- [x] 2.3 `auth_repo.py` 新增 `find_account_by_email(email)`（供登录校验）；验证只返回 email 匹配且非 revoked 的账号（或返回 None，不泄露）
- [x] 2.4 现有 `issue_token`/`consume_token_for_session` 保持原语义不动；验证既有 exchange 测试仍通过

## 3. Service 层（AuthService）

- [x] 3.1 `service.py` 封装 `hash_password` / `verify_password`（argon2id，自含 salt）；验证单元测试：哈希含 argon2 前缀、verify 对错密码 False、哈希不可逆恢复
- [x] 3.2 `AuthService.issue_tenant_invitation(tenant_name=...)`：包装仓库层签发，明文只返回一次；验证返回 (token 行, 明文) 且不落日志
- [x] 3.3 `AuthService.register_tenant(invite_token, email, password, ...)`：完成 2.2 调用 → `run_pending` 同步收束 → 账号 active 后创建用户会话；失败路径映射（token 无效→统一认证错误；provisioning 失败→返回账号 failed 状态）；验证新 pytest 用例覆盖注册成功/失败/重试
- [x] 3.4 `AuthService.login(email, password, ...)`：查账号 → verify → 创建会话；邮箱不存在/密码错/禁用 → 同一 `CredentialExchangeError`；验证对比测试确认三种输入错误响应一致、response 不出现差异字段
- [x] 3.5 会话创建 seam：抽出并复用现有会话 row 创建逻辑（供 register/login 使用，不改变 exchange）；验证会话 timeout 契约与既有测试一致

## 4. API 层（bootstrap/auth/api.py）

- [x] 4.1 新增 `RegisterRequest`（invite_token, email, password）与 `LoginRequest`（email, password）pydantic 模型（长度/格式约束）；验证非法输入 422
- [x] 4.2 新增 `POST /api/auth/register`：Origin 校验 → runtime.register_tenant → 成功 Set-Cookie（HttpOnly、Secure、SameSite），响应 `{session_id, account_id}`；失败 401 统一文案；验证 curl/pytest 覆盖
- [x] 4.3 新增 `POST /api/auth/login`：Origin 校验 → runtime.login → 成功 Set-Cookie；失败 401 统一文案；验证登录后 `/api/auth/me` 返回该账号身份
- [x] 4.4 admin 签发接口扩展：`POST /api/admin/test-accounts` 或新增签发接口支持「仅租户名」（不建号）；返回一次性邀请码；验证签发成功且 `test_accounts` 无新行
- [x] 4.5 确认既有 `POST /api/auth/exchange` 仍工作（存量兼容）；验证一个旧版 token 兑换成功

## 5. 前端（WebChat）

- [x] 5.1 `frontend/chat/src/auth.ts` 新增 `registerTenant(inviteToken, email, password)` / `login(email, password)` fetch 封装（same-origin、不持久化凭据）；验证 `npm run typecheck` 通过
- [x] 5.2 `LoginPanel.tsx` 改为两表单：注册（邀请码+邮箱+密码）与登录（邮箱+密码），含 tab/切换、错误文案统一（不区分邮箱不存在/密码错）；验证 `npm run typecheck` 通过
- [x] 5.3 移除纯 Token 兑换入口（保留后端接口）；验证成功后进入聊天界面、刷新不丢会话、logout 后回到登录页（见 6.3 端到端）
- [x] 5.4 `npm run build:chat` 通过；验证产物含新表单（`static/chat/` 内存在 注册/登录 文案）

## 6. 验证与文档

- [x] 6.1 新写 pytest：注册成功/失败/并发唯一/email 唯一/provisioning 失败 retry/登录不泄露（覆盖 spec 全部 scenario）；运行 `pytest -q -W error tests/auth_provisioning/` 全绿（82 passed = 64 存量 + 14 注册/登录用例 + 4 HTTP 契约用例）
- [x] 6.2 pyright 两配置通过（project + tests），与既有 C5 门禁一致（两配置均 0 error）
- [x] 6.3 本地端到端冒烟：注册新租户 → 立即进入会话 → 发新租户消息可用/身份正确 → logout → 邮箱密码重新登录 → /me 身份一致（含 tenant 归属）。HTTP 层闭环由 `test_http_contract.py` 覆盖；身份派生见 webchat 身份测试。
- [x] 6.4 更新 `openspec/PILOT_ROADMAP_PROJECT_CHECKLIST.md`（新增能力完成情况）并按流程归档本 change（`openspec archive change`）→ checklist 已更新；change 已归档至 `openspec/changes/archive/2026-09-27-invite-code-tenant-registration/`，delta spec 已同步进 `openspec/specs/auth-provisioning/spec.md`

## 7. 部署（生产，114.55.243.189）

- [x] 7.1 rsync 代码到 `/opt/NexusCompanion`（保留 `config.toml`、`static/chat/`；`git archive` 或 tar 生成明文包）；验证容器重启后 `docker compose ps` 健康 → tar 包部署（源码+migration+static/chat），`docker compose build` 重建镜像（运行期以 requirements.txt 安装依赖，新增 argon2；补 greenlet 依赖），容器 Up、日志正常
- [x] 7.2 在服务器跑新 alembic 迁移（容器内 `alembic upgrade head` 或既有 alembic_util）；验证 `docker exec NexusCompanion python ...` 检查新列存在 → 迁移 `b7e2f9a4c1d8 → c9d7e3a5f2b1` 成功；`test_accounts.email/password_digest`、`access_tokens.account_id nullable + tenant_name`、`ck_test_accounts_status` 含 failed、`uq_test_accounts_email` 唯一索引均验证
- [x] 7.3 端到端验收：通过 `https://nexus.il7510n.dpdns.org/` 用邀请码注册新租户 → 发消息收到回复（LLM provider 可用前提下）→ logout → 邮箱密码再登录成功 → 全链路通过（注册/发消息回复/退出/重登，新租户 pilot-9iw1ijbu18al，WS 身份派生正确）