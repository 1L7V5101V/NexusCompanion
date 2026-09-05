# C1 canonical identity — 实施任务

> 对应 `openspec/openspec-tasks-bundle/task-01-canonical-identity.md`；证据统一落 `openspec/evidence/c1-canonical-identity/`。
> 管理闭环：任务 checkbox → `openspec status` → evidence → 仅在有 evidence 时更新 task-01 / PILOT_ROADMAP_PROJECT_CHECKLIST 状态。

## 1. 实现准备

- [x] 1.1 本地 PostgreSQL 测试实例就绪（便携 PG 17.5 @ localhost:5433，`nexus` 库含 vector + pg_trgm 扩展，与 `tests/migration/conftest.py` 默认 URL 一致）。验证：psycopg 连接成功 + `select version()` 输出存 evidence
  - 证据：`openspec/evidence/c1-canonical-identity/env-localpg.txt`

## 2. 模型与 Migration（P0 基础）

- [x] 2.1 `bootstrap/db/models/canonical.py`：`TestAccountModel` / `CanonicalConversationModel` / `CanonicalMessageModel` 三模型，约束命名按 design.md §ADR-7。验证：pyright 通过；模型可被 alembic env 导入
- [x] 2.2 Alembic migration（revises `d6e1cd9205cd`）：三表 + 唯一约束 + FK(RESTRICT) + CHECK + 索引 + dev seed（`tenant_id='dev'`）；downgrade 删表。验证：空 PG `upgrade head` 成功、`pg_constraint`/`pg_indexes` 断言、seed 存在、downgrade→upgrade 循环
  - 证据：`tests/canonical_identity/test_migration.py` + `openspec/evidence/c1-canonical-identity/pytest-migration.txt`（3 passed）
- [x] 2.3 `alembic/env.py` 注册新模型导入。验证：pyright 对模型/迁移零新增错误（`evidence/.../pyright.txt`）；`upgrade head` 后表结构与模型一致（集成测试断言约束/索引/seed）

## 3. Repository 与 resolver

- [x] 3.1 `bootstrap/db/repository/canonical_repo.py`：账号/会话/消息 CRUD + `create_account_with_conversation`（单事务幂等）+ `append_message`（单事务 `UPDATE...RETURNING` 取号 + INSERT）+ `fetch_messages`（游标补拉）+ `latest_sequence`。验证：单元/集成测试覆盖
- [x] 3.2 `bootstrap/identity.py`：`CanonicalIdentity` 不可变三元组 + `CanonicalIdentityResolver`（按 account/tenant 查表）+ `IdentityResolutionError`；fail-closed，不 import `DEFAULT_TENANT`。验证：负向测试 + 静态契约测试
  - 证据：`openspec/evidence/c1-canonical-identity/pytest-identity.txt`（19 passed）

## 4. 并发与负向测试（验收核心）

- [x] 4.1 并发 sequence 分配测试：asyncio 并发对会话 A/B 各 N 条追加 → A=0..N-1、B=0..N-1、无重复、跨会话独立、无空洞。验证：行级断言 + 并发输出存 evidence
  - 证据：`tests/canonical_identity/test_sequence.py` + `openspec/evidence/c1-canonical-identity/pytest-concurrency.txt`（5 passed，含 20×2 并发追加）
- [x] 4.2 唯一约束负向测试：重复 tenant_id 账号/会话插入 → IntegrityError；直接插入重复 `(conversation_id, sequence)` → IntegrityError；非法 role → CHECK 拒绝；孤儿 FK → 拒绝。验证：pytest 断言
- [x] 4.3 无 binding 拒绝测试：未知 account/tenant 解析 → `IdentityResolutionError`；空 principal → 拒绝；未知会话 append → 失败且零写入；跨 tenant 读取不可见。验证：pytest 断言
  - 证据：`openspec/evidence/c1-canonical-identity/pytest-identity.txt`（负向用例含未知 principal/空 principal/默认租户拒绝/零写入/跨租户不可见）

## 5. 契约 fixture 与静态防回归

- [x] 5.1 `tests/fixtures/canonical_identity_chain.json`：派生链正/负用例 fixture（含 seeded dev 身份与负向拒绝用例），供 C2/C4/C5/C9/C10/C14 复用。验证：`tests/test_canonical_identity_contract.py` 消费该 fixture
- [x] 5.2 静态契约测试：扫描 `bootstrap/db/repository/canonical_repo.py` + `bootstrap/identity.py` + `bootstrap/db/models/canonical.py` 无 sqlite 引用、无 `DEFAULT_TENANT` 引用。验证：pytest 通过（无 PG 依赖，CI 可跑）
  - 证据：`openspec/evidence/c1-canonical-identity/grep-no-sqlite-fallback.txt` + `pytest-static-contract.txt`

## 6. 回滚演练与验收回归

- [x] 6.1 Create→Verify→Enable→rollback 演练：scratch DB `upgrade head`（Create）→ 校验约束/seed/空基线（Verify）→ 模拟入口开放与写入 → 关闭入口 + 停 provisioning、**保留 PG 数据**断言行数不变（rollback drill）。验证：演练脚本可重跑，记录存 evidence
  - 证据：`rollback-drill.md` + `rollback_drill.py` + `rollback-drill-output.txt`（9 项全 PASS）
- [x] 6.2 PR diff 范围检查：未触碰 inbox/outbox/turn/delivery/admission/binding/auth 表与模块。验证：`git diff --stat` 记录 + 范围声明存 evidence
- [x] 6.3 回归：`pyright --level error`（project + tests 两配置）、`pytest -q -W error tests/`（全量，PG 集成在 5433 可用时执行）。验证：输出存 evidence，无新增失败
  - 证据：`pytest-regression.txt` + `pyright.txt`
- [x] 6.4 状态更新：task-01 状态置 `in_progress`（本 change 生效后）；全部 evidence 齐全且合并后按 §8 置 `verified`；`openspec validate` 通过
