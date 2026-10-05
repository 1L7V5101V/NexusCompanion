# task-1.1 / 1.2 / 5.4 — 迁移、模型与仓储

日期：2026-10-04　分支：`feature/c9-persona-relationship`（基于 `feature/p0-retention-wiring` tip）　worktree：`D:/1/wt-c9`

## 变更

| 文件 | 内容 |
| --- | --- |
| `alembic/versions/e1f3a5c7b9d2_c9_persona_relationship.py` | **新**（expand-only，head 实测 `d0a9b7c3e1f5`）：`persona_templates`（name UNIQUE + 三块 TEXT + enabled）、`tenant_persona_profiles`（tenant_id PRIMARY KEY 即一 tenant 一份；source CHECK ∈ ('template','custom')；template_id FK SET NULL 软引用；三块 TEXT NOT NULL；**只有 created_at 无 updated_at**——不可变行的 schema 表达）、`persona_audit_events`（actor/action CHECK + turn_id NULLABLE + detail JSONB + (tenant_id, created_at) 索引）。**无任何 revision/version/history 表**（5.4 断言） |
| `bootstrap/db/models/persona.py` | 三表 ORM（`__init__.py` 注册 + `__all__`） |
| `bootstrap/db/repository/persona_repo.py` | `PersonaRepository`：模板 create/list/disable（写审计）；`submit_onboarding` 单事务（profile 抢位 + `memory_items` self 种子 + `onboarding_submit` 审计；`OnboardingAlreadyCompletedError` = 409 语义）；**无任何修改已提交快照的方法**；审计读取面 |

## 迁移闭环（真实 PG 5433，`nexus` 库）

```
alembic upgrade head   → Running upgrade d0a9b7c3e1f5 -> e1f3a5c7b9d2
alembic downgrade -1   → Running downgrade e1f3a5c7b9d2 -> d0a9b7c3e1f5
alembic upgrade head   → Running upgrade d0a9b7c3e1f5 -> e1f3a5c7b9d2
information_schema 断言：三表存在；persona 域无 revision/version/history 表
（tests/persona/test_repository.py::test_no_revision_tables，task 5.4）
```

## 实测前置事实（写回 design ADR-1）

- `memory_items` 是 tenant LIST 分区表：种子写入以 provisioning 分区就绪为前置
  （真实时序 provisioning ready → Token → 登录 → onboarding）；onboarding 内不做 DDL。
- `memory_items.embedding` 为 PG `vector` 列而 ORM 声明 Text：同步 psycopg 栈自适应、
  asyncpg 下 NULL 被绑成 VARCHAR 拒绝 → 种子/写缝用显式列名原生 SQL（不含 embedding）。
- JSONB detail 参数 asyncpg 不收 dict → 绑定 JSON 字符串（repo 侧）或
  `CAST(:detail AS jsonb)`（原生 SQL 侧）。

## 测试证据

`tests/persona/test_repository.py`（6 passed）：onboarding 原子 + 并发双提交拒绝 +
审计同事务；模板停用不改已建快照（spec「模板变更不覆盖已有 tenant」）；跨 tenant
隔离；无 onboarding → 快照 None；io 写读审计；无 revision 表断言。
汇总见 `task-5.1-targeted.txt`。
