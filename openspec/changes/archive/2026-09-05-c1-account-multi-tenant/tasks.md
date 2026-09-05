# C1 account→N tenant(account-multi-tenant)— 实施任务

> 对应 `openspec/openspec-tasks-bundle/task-01-canonical-identity.md`(follow-up);
> 证据统一落 `openspec/evidence/c1-account-multi-tenant/`。
> 管理闭环:任务 checkbox → `openspec status` → evidence → 仅在有 evidence 时更新
> task-01 注记 / PILOT_ROADMAP_PROJECT_CHECKLIST 状态。

## 1. 模型与 Migration

- [x] 1.1 `bootstrap/db/models/canonical.py`:`TestAccountModel` 移除 `tenant_id` 列与
  `uq_test_accounts_tenant_id`;`CanonicalConversationModel` 语义=一行一个
  agent(tenant 资源域,账号可多行)。验证:pyright + 集成测试。
- [x] 1.2 Alembic migration `c4d8f2a6e9b3`(下行 `e2b4d6f8a0c2`):upgrade 删
  `test_accounts.tenant_id` 及其唯一约束;downgrade 守卫回填(每账号恰一条会话),
  多 agent fail-closed。验证:空 PG `upgrade head` + `downgrade→upgrade` 循环 →
  `tests/canonical_identity/test_migration.py`(证据见 §5)。

## 2. Repository 与 resolver

- [x] 2.1 `canonical_repo.py`:`create_account` / `create_agent`(跨账号占用 →
  `TenantAlreadyBoundError`)/ `provision_account_with_agent`(替代
  `create_account_with_conversation`)/ `list_conversations_by_account`;删
  `get_account_by_tenant`。验证:集成测试正/负用例覆盖。
- [x] 2.2 `bootstrap/identity.py`:`resolve_by_tenant` 恒单一三元组(conversation→
  account);`list_agents(account)` 0..N 枚举,删除单三元组 `resolve_by_account`。
  验证:负向测试(fail-closed,无 DEFAULT_TENANT) + 静态契约测试。

## 3. 契约 fixture 与静态防回归

- [x] 3.1 `tests/fixtures/canonical_identity_chain.json` 升 version=2:账号级正向
  用例改「账号 + agent 列表」;约束声明改 account→N。验证:
  `tests/test_canonical_identity_contract.py` 消费该 fixture。
- [x] 3.2 静态契约测试:`CANONICAL_MODULES` 纳入新 migration;无 sqlite /
  `default_tenant` token。验证:pytest(无 PG 依赖,CI 可跑)。

## 4. 新增语义测试(验收核心)

- [x] 4.1 一账号多 agent 正用例:同一账号两个 agent(不同 tenant)各自解析独立
  三元组、独立空消息流、互不串、账号级枚举确定性(created_at/id 升序)。验证:
  `tests/canonical_identity/test_identity.py` 内 `test_one_account_many_agents_independent`。
- [x] 4.2 同 tenant 跨账号占用被拒:repo 守卫 `TenantAlreadyBoundError` + DB 唯一
  约束两层拒绝;同账号重复建同 tenant 会话仍拒绝。验证:
  `test_same_tenant_on_second_account_rejected` / `test_duplicate_tenant_conversation_rejected`。
- [x] 4.3 账号无 agent = 空枚举(非错、非默认租户);未知/空账号/未知 tenant/
  `default` 依旧 fail-closed。验证:负向测试 + fixture 负向用例。

## 5. 验收回归与证据

- [x] 5.1 PG 集成:`pytest -q -W error tests/canonical_identity/`(本地 PG 5433,含
  downgrade→upgrade 循环与多 agent/跨账号负用例)。证据:`pytest-canonical.txt`(28 passed)。
- [x] 5.2 静态契约:`pytest -q -W error tests/test_canonical_identity_contract.py`。
  证据:`pytest-static-contract.txt`(3 passed)。
- [x] 5.3 无 SQLite fallback / DEFAULT_TENANT grep(canonical 三模块 + 两条 migration)。
  证据:`grep-no-sqlite-fallback.txt`(均 no matches)。
- [x] 5.4 pyright(project + tests 两配置),触碰文件零 error。证据:`pyright.txt`
  (project 36 / tests 30 为既有基线)。
- [x] 5.5 全量回归 `pytest -q -W error tests/`,无新增失败。证据:`pytest-regression.txt`
  (1168 passed / 1 既有环境性失败——C1 基线同款
  `test_index_returns_status_json_without_bundle`,与本次改动无关)。
- [x] 5.6 downgrade→upgrade 循环(gated by c1_reset)。证据:并入 `pytest-canonical.txt`
  (`test_migration.py::test_downgrade_then_upgrade_cycle`)。
- [x] 5.7 状态收尾:task-01 注记更新;主 spec 与 roadmap 同步(account→N 措辞);归档本 change
  (移入 `openspec/changes/archive/`);`openspec validate --all` 5/5 通过。commit 由用户确认后执行。
