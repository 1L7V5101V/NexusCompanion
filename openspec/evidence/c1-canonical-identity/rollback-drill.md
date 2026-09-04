# C1 首次启用 Rollback 演练记录（task 6.1）

- **日期**：2026-09-05
- **环境**：本地便携 PostgreSQL 17.5（localhost:5433，`nexus` 管理库，含 vector + pg_trgm 扩展），见 `env-localpg.txt`
- **可复现脚本**：`rollback_drill.py`（本目录）；原始输出：`rollback-drill-output.txt`

## 演练步骤与结果（Create → Verify → Enable → Rollback）

| 步骤 | 动作 | 冻结依据 | 结果 |
| --- | --- | --- | --- |
| Create | scratch DB `nexus_c1_drill` 上 `alembic upgrade head`（head = `e2b4d6f8a0c2`），只建 Pilot 新表/约束/seed，全程未连接旧单体库 | §5.9.9 Create | PASS |
| Verify.revision | `alembic_version = e2b4d6f8a0c2` | §5.9.9 Verify | PASS |
| Verify.tables | `test_accounts` / `canonical_conversations` / `canonical_messages` 三表存在 | §5.9.9 | PASS |
| Verify.constraints | 三条唯一约束齐备（tenant 两侧 1:1 + `(conversation_id, sequence)` 唯一） | §5.9.9 | PASS |
| Verify.empty_baseline | `canonical_messages` 0 行（新账号从空历史开始） | §5.9.2 | PASS |
| Verify.seed | dev seed 账号存在且 `active` | ADR-4 | PASS |
| Enable | 模拟入口开放：向 dev 规范会话写入 3 条 canonical message | §5.9.9 Enable | PASS |
| Rollback.data_retained | 关闭入口 + 停止 provisioning（模拟：此后无新写入/开户）后，PG 数据**保留**（messages=3, accounts=1），供修复后继续使用 | §10 DECIDED | PASS |
| Rollback.no_reverse_sync | 无任何向旧单体 SQLite 的反向同步路径（演练全程未连接旧库） | §10 DECIDED | PASS |

**RESULT: PASS** —— 演练退出码 0。

## Rollback 语义（冻结，§5.9.9 / §10 DECIDED）

- 回滚 = **关闭 Pilot 入口 + 停止新 provisioning/work**，保留已产生的 PostgreSQL Pilot 数据。
- 不反向同步 SQLite；不把旧单体库当作 Pilot 回滚目标。
- 需要恢复业务数据时使用 PostgreSQL backup/PITR（P3 演练项）。
- migration 层：C1 未 cutover、无人读取，`downgrade()` 删除三张新表是安全操作（由 `tests/canonical_identity/test_migration.py::test_downgrade_then_upgrade_cycle` 验证 downgrade→upgrade 循环）。

## 与验收标准的对应

- 验收 6「Create→Verify→Enable 演练可回滚（关闭入口 + 停 provisioning，保留 PG 数据）」：本记录 + `rollback_drill.py` + `rollback-drill-output.txt`。
