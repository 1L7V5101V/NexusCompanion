# C5 auth/provisioning/admin — 实施任务

> 对应 `openspec/openspec-tasks-bundle/task-05-auth-provisioning-admin.md`；证据统一落
> `openspec/evidence/c5-auth-provisioning-admin/`。
> 管理闭环：任务 checkbox → `openspec status` → evidence → 仅在有 evidence 时更新
> task-05 / PILOT_ROADMAP_PROJECT_CHECKLIST 状态。

## 1. 模型与 Migration

- [ ] 1.1 `bootstrap/db/models/auth.py`：`AccessTokenModel` / `AuthSessionModel` / `AdminCredentialModel` / `AdminAuditEventModel` / `TenantProvisioningJobModel`，约束命名按 design.md §1。验证：pyright 通过、alembic env 可导入
- [ ] 1.2 Alembic migration（revises `f3c8a9d2e7b4`）：五表 + 唯一约束 + FK(RESTRICT) + CHECK + 索引；downgrade DROP。验证：scratch PG `upgrade head`、约束断言、downgrade→upgrade 循环
  - 证据：`tests/auth_provisioning/test_migration.py` + evidence `pytest-pg-integration.txt`（4 passed in 47 项 suite）
- [ ] 1.3 `alembic/env.py` 注册新模型。验证：autogenerate 无意外 diff

## 2. crypto 与 repository

- [ ] 2.1 `bootstrap/auth/crypto.py`：pepper provider（env → workspace secrets 文件）、`new_token`（256-bit CSPRNG）、`digest_value`（HMAC-SHA-256）、`csrf_for_session`。验证：单元测试 + pepper 不落日志
- [ ] 2.2 `bootstrap/db/repository/auth_repo.py`：token（签发/原子消费/撤销）、session（创建/按 digest 校验/touch/撤销）、admin（bootstrap/rotate/enable/disable）、audit、provisioning job（认领 SKIP LOCKED/状态推进/retry）。验证：集成测试覆盖
- [ ] 2.3 并发兑换原子性测试：asyncio 多连接并发兑换同一 Token 仅 1 成功（§5.9.3 ADR-2）
  - 证据：`tests/auth_provisioning/test_exchange_atomic.py` + evidence `pytest-pg-integration.txt`（2 passed in 47 项 suite）

## 3. service 层

- [ ] 3.1 `bootstrap/auth/service.py`：`AuthService`（签发/兑换/validate/logout/撤销账号凭据）、timeout 契约（idle 7d/absolute 30d；admin 30min/12h，创建时固化）
- [ ] 3.2 `AdminAuthService`：bootstrap/status/rotate-recovery-token(--force-local)/revoke-sessions --all/disable/enable；token 轮换与 session 撤销独立；audit 全量落 `admin_audit_events`
- [ ] 3.3 `ProvisioningService`：create_account（provisioning+pending job 单事务）、run_pending/recover（启动扫描）、retry、suspend/unsuspend/revoke（不删数据、revoked 拒新）
  - 证据：`tests/auth_provisioning/test_provisioning_lifecycle.py` + evidence `pytest-pg-integration.txt`（8 passed in 47 项 suite）

## 4. HTTP API 与 CLI

- [ ] 4.1 `bootstrap/auth/api.py`：`/api/auth/exchange|csrf|logout|me` + `/api/admin/auth/exchange|csrf` + `/api/admin/test-accounts*` + `/api/admin/tokens/{id}/revoke`；401/403 契约、错误体不泄露存在性
- [ ] 4.2 Cookie 属性 + CSRF/Origin 中间件 + admin 回环边界（design ADR-4/5）；`__Host-nexus_session` vs `__Host-nexus_admin` 隔离
  - 证据：`tests/auth_provisioning/test_http_contract.py` + evidence `pytest-pg-integration.txt`（6 passed in 47 项 suite）
- [ ] 4.3 挂载：`create_chat_app` 可选 auth 装配（默认关闭 = P0.5 行为不变）；dashboard 挂 admin 路由；`[auth]` 配置节（`agent/config_models.py` + `agent/config.py`）
- [ ] 4.4 `main.py pilot-admin` 子命令：bootstrap/status/rotate-recovery-token/--force-local/revoke-sessions --all/disable/enable；TTY 门禁；明文仅 TTY 回显一次、不进 argv/env
  - 证据：`tests/auth_provisioning/test_cli_layer.py` + evidence `pytest-auth-unit.txt`（10 passed in 27 项非 PG 套件）

## 5. 验收回归与 runbook

- [ ] 5.1 digest-only 静态检查：grep 五表写入路径与日志调用无明文凭据；日志审计负向测试
- [ ] 5.2 WS handshake guard（Cookie + Origin）单元测试（guard 函数层，通道接线归 C4 消费）
- [ ] 5.3 recovery runbook 三路径演练记录（lost-token / suspected-leak / database-restore）→ `openspec/evidence/c5-auth-provisioning-admin/runbook-drill.md`
- [ ] 5.4 回归：`pyright --level error`（project + tests）、`pytest -q -W error tests/`；对齐 main 基线（36 既有 pyright 错误 + chat_api 1 环境性失败）
- [ ] 5.5 PR diff 范围检查：未触碰 attachment(C6)/工具 allowlist(C7)/Persona(C9)/Telegram binding(C10)/schedule(C11)
- [ ] 5.6 `openspec validate` 通过；evidence 齐全后按 §8 更新 task-05 与 checklist 状态
- [ ] 5.7 部署态冒烟（云 canary 容器真实 HTTP 全生命周期）
  - 证据：`openspec/evidence/c5-auth-provisioning-admin/canary-deploy-smoke.txt`（20/20 PASS）
