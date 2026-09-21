# C5 auth/provisioning/admin — Design

> 冻结输入：PILOT_ROADMAP §5.9.3 / §5.9.9 / §5.9.13 / §5.4 / §10 DECIDED。
> 本 design 只补齐 roadmap 未冻结的字段级契约（§10 OPEN FOR P-1 SPEC「Digest/encryption/key
> rotation」在本 change 的部分）。

## 1. 数据模型（五表，revises `f3c8a9d2e7b4`）

```sql
CREATE TABLE access_tokens (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id     UUID         NOT NULL,
    token_digest   VARCHAR(64)  NOT NULL,
    digest_version SMALLINT     NOT NULL DEFAULT 1,
    display_note   VARCHAR(255) NOT NULL DEFAULT '',
    issued_by      VARCHAR(64)  NOT NULL DEFAULT '',
    expires_at     TIMESTAMPTZ  NULL,
    consumed_at    TIMESTAMPTZ  NULL,
    revoked_at     TIMESTAMPTZ  NULL,
    revoked_reason VARCHAR(255) NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_access_tokens_digest UNIQUE (token_digest),
    CONSTRAINT fk_access_tokens_account_id
        FOREIGN KEY (account_id) REFERENCES test_accounts (id) ON DELETE RESTRICT,
    CONSTRAINT ck_access_tokens_digest_sha256 CHECK (char_length(token_digest) = 64)
);
CREATE INDEX ix_access_tokens_account ON access_tokens (account_id);

CREATE TABLE auth_sessions (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_type     VARCHAR(16)  NOT NULL,
    account_id         UUID         NULL,
    session_digest     VARCHAR(64)  NOT NULL,
    digest_version     SMALLINT     NOT NULL DEFAULT 1,
    idle_timeout_s     INTEGER      NOT NULL,
    absolute_timeout_s INTEGER      NOT NULL,
    user_agent         VARCHAR(255) NOT NULL DEFAULT '',
    last_seen_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at         TIMESTAMPTZ  NOT NULL,
    revoked_at         TIMESTAMPTZ  NULL,
    revoked_reason     VARCHAR(255) NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_auth_sessions_digest UNIQUE (session_digest),
    CONSTRAINT ck_auth_sessions_principal CHECK (
        principal_type = 'admin' OR (principal_type = 'user' AND account_id IS NOT NULL)),
    CONSTRAINT fk_auth_sessions_account_id
        FOREIGN KEY (account_id) REFERENCES test_accounts (id) ON DELETE RESTRICT
);
CREATE INDEX ix_auth_sessions_account ON auth_sessions (account_id);

CREATE TABLE admin_credentials (
    id              SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    recovery_digest VARCHAR(64) NULL,
    digest_version  SMALLINT    NOT NULL DEFAULT 1,
    revision        INTEGER     NOT NULL DEFAULT 0,
    rotated_at      TIMESTAMPTZ NULL,
    disabled_at     TIMESTAMPTZ NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_admin_credentials_digest CHECK (
        recovery_digest IS NULL OR char_length(recovery_digest) = 64)
);

CREATE TABLE admin_audit_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor       VARCHAR(64) NOT NULL,
    action      VARCHAR(64) NOT NULL,
    target_type VARCHAR(32) NOT NULL DEFAULT '',
    target_id   VARCHAR(64) NOT NULL DEFAULT '',
    detail      JSONB       NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_admin_audit_created ON admin_audit_events (created_at DESC);

CREATE TABLE tenant_provisioning_jobs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id    UUID        NOT NULL,
    tenant_id     VARCHAR(64) NULL,
    operation     VARCHAR(32) NOT NULL DEFAULT 'provision_agent',
    status        VARCHAR(16) NOT NULL DEFAULT 'pending',
    attempt_count INTEGER     NOT NULL DEFAULT 0,
    last_error    TEXT        NULL,
    started_at    TIMESTAMPTZ NULL,
    finished_at   TIMESTAMPTZ NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_tenant_provisioning_jobs_tenant UNIQUE (tenant_id),
    CONSTRAINT ck_tenant_provisioning_jobs_status
        CHECK (status IN ('pending', 'running', 'ready', 'failed')),
    CONSTRAINT ck_tenant_provisioning_jobs_tenant_present
        CHECK (status IN ('pending', 'failed') OR tenant_id IS NOT NULL),
    CONSTRAINT fk_tenant_provisioning_jobs_account
        FOREIGN KEY (account_id) REFERENCES test_accounts (id) ON DELETE RESTRICT
);
```

- `uq_tenant_provisioning_jobs_tenant`：PG 唯一索引不约束 NULL，pending（尚未分配 tenant）
  多行合法；job 开始执行时先原子分配 tenant_id 再置 `running`。
- timeout 值在**会话创建时固化**到行内（`idle_timeout_s`/`absolute_timeout_s`），配置后续修改
  只影响新会话，避免存量会话语义漂移。
- `admin_credentials` 单行由 `CHECK (id = 1)` 强制（Pilot 单一 admin principal）。
- seed：not_applicable（admin 行由 `pilot-admin bootstrap` 显式创建，不做迁移 seed）。
- downgrade：C5 未 cutover，直接 DROP 五表（§5.9.9 安全）。

## 2. ADR

### ADR-1 digest 算法与 key source（关闭 §10 OPEN 的 C5 部分）

- 凭据原值 = `prefix + secrets.token_urlsafe(32)`（256-bit CSPRNG）。邀请 Token `nxt_`，
  admin recovery token `nad_`，session cookie 值 `ns_`。
- digest = `HMAC-SHA-256(pepper, utf8(原值))` hex（64 字符）；pepper ≥32 字节 CSPRNG。
- pepper 来源（优先级）：env `NEXUS_AUTH_PEPPER` → `<workspace>/secrets/auth_pepper`（首次使用
  自动生成 64 hex 字符并落盘，文件权限尽力收紧）。不进 `config.toml`、不进数据库、不打日志
  （§5.9.3）。
- rotation：pepper 不支持在线轮换——轮换即全部既有 digest 失效（所有 session/token 失效），
  属维护窗口操作，写 runbook；`digest_version` 列保留为未来 per-version pepper map 的演进位，
  当前恒为 1。
- CSRF token：`HMAC-SHA-256(pepper, "csrf:" || session_id.bytes)` hex——session-bound（会话行
  消失即失效）、服务端可随时重算（`GET /api/auth/csrf` 幂等重发同一值，前端存内存）、无明文落库。
- 明文只在签发/兑换响应中出现一次；数据库、日志、审计只允许 digest。

### ADR-2 一次性兑换原子性

单事务：`SELECT ... FOR UPDATE` 锁 token 行 → 校验 `consumed_at IS NULL AND revoked_at IS NULL
AND (expires_at IS NULL OR expires_at > now())` 且账号 `status='active'` → `UPDATE
consumed_at=now()` → `INSERT auth_sessions` → COMMIT。并发兑换第二个连接阻塞在同一行锁上，
获得锁后看到 `consumed_at` 已置 → 兑换失败（401，不泄露原因）。不做「先查后插」两段式。

### ADR-3 401/403 与不泄露存在性

- 401：无 Cookie、session 未知/过期/已撤销、邀请 Token 或 recovery token 无效/已消费/已过期。
  统一错误体 `{"detail": "authentication required"}` 或 `{"detail": "invalid credentials"}`，
  不区分「不存在 vs 已过期 vs 已撤销」。
- 403：principal 有效但被禁止——账号 `suspended`/`revoked`、CSRF 失败、Origin/Referer 不在
  allowlist、admin route 非回环来源、capability 不足。统一 `{"detail": "forbidden"}`。
- 兑换响应不回显账号 id/tenant id 之外的最小身份；`/api/auth/me` 只返回
  `account_id/display_name/status`。

### ADR-4 CSRF + Origin 组合校验

mutation（POST/PUT/PATCH/DELETE）依序校验：
1. `Origin`（优先）或 `Referer` 的 origin 部分必须命中 `auth.origin_allowlist`（精确 scheme+host+port）；
2. `X-CSRF-Token` 头必须等于当前 session 的派生 CSRF 值。
两者独立失败均 403。GET/HEAD/OPTIONS 只需有效 session（不改状态）。WS handshake 校验 Cookie
+ Origin allowlist（guard 函数由 C4 通道在 accept 前调用）。

### ADR-5 admin 网络边界

`/api/admin/*` 在 FastAPI 依赖中校验 `request.client.host ∈ auth.admin_allow_ips`（默认
`127.0.0.1`/`::1`），不满足一律 403 通用文案。文档要求反代/Tunnel 不得转发 `/api/admin/*`
（Cloudflare Tunnel 同机转发会表现为回环来源，因此 allowlist 不是唯一防线，运维边界写入
runbook）；admin API 独立于公网 WebChat 挂载点。

### ADR-6 provisioning job 执行模型

- 创建账号 = 单事务写 `test_accounts(status='provisioning')` + `tenant_provisioning_jobs(pending)`。
- 执行：进程内 `ProvisioningService.run_pending()` 认领（`FOR UPDATE SKIP LOCKED`，pending 或
  崩溃残留 running）→ 原子分配 tenant_id（`pilot-<short-uuid>`）置 running → 执行 executor →
  单事务置 job `ready` + 账号 `active`。
- executor 为 Protocol seam：C5 内置实现 = 复用 `infra/storage/provisioning.py` 分区 provisioning
  + `CanonicalIdentityRepository.create_agent()`（幂等，tenant 唯一约束兜底）+ persona seed 留
  C9 接缝（当前 not_applicable）。失败 → `failed` + `last_error`，可由 admin retry（同 job 行
  attempt_count++，tenant_id 不变，不产生第二个 agent）。
- 启动恢复：进程启动扫描 `status IN ('pending','running')` 重新入队（running 视为崩溃残留）。
- turn 入口 `require_ready()` fail-closed 仅纵深防御（§5.9.13），本 change 提供 seam 不改 Passive 入口。
- `suspended`/`revoked` 不删 partition/历史数据；`revoked` 拒绝签发 Token 与新 session。

### ADR-7 挂载与配置

- `/api/auth/*` 挂 WebChat Gateway（`create_chat_app` 新增可选 auth 装配，默认关闭时行为与
  P0.5 完全一致）；`/api/admin/*` 挂 Dashboard API。共享 `bootstrap/auth/runtime.py`
  （engine/session_factory 来自 `config.storage.postgres_url`，服务单例）。
- `[auth]` 配置节：`enabled`（Enable 步总开关，默认 false）、`cookie_secure`、
  `origin_allowlist`、`admin_allow_ips`、`session_idle_hours=168`、`session_absolute_hours=720`、
  `admin_idle_minutes=30`、`admin_absolute_hours=12`、`invitation_token_ttl_hours=168`。
  timeout 数值与 §10 PROPOSED DEFAULT 一致，上线前按部署记录。

## 3. 测试策略

- PG 集成（scratch DB，同 C1 conftest 模式）：migration 约束、并发兑换原子性（asyncio.gather
  多连接抢同一 token）、session timeout/撤销、provisioning 状态机 + 崩溃恢复（running 残留重扫）、
  suspend/unsuspend/revoke 负向。
- HTTP 契约（FastAPI TestClient/httpx ASGI）：401/403 矩阵、CSRF/Origin 矩阵、Cookie 属性、
  admin 回环边界、错误体不泄露存在性。
- 静态/单元：digest-only（grep 明文落库/日志）、CLI（TTY 门禁、命令面、明文只回显一次）、
  crypto 单元。
