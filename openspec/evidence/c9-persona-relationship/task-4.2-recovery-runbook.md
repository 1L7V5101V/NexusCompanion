# task-4.2 — Persona/Relationship 异常恢复 runbook（交付物）

## 恢复语义（§10 DECIDED）

PersonaProfile（提交后固定）与 RelationshipState（当前演化值）的异常恢复**只走
PostgreSQL backup / PITR**：

- **没有**产品内 revision 浏览、回滚或版本切换入口（spec「无版本表」——schema 断言
  `tests/persona/test_repository.py::test_no_revision_tables`）；
- **没有** config.toml 恢复路径：`[agent.persona]` 只是新 tenant 的 onboarding
  seed 与单体兼容默认，多租户运行时不读它作为 tenant 当前值；
- 恢复期间该 tenant 的 onboarding 门禁与 prompt 注入按 PG 恢复点的状态行事。

## Runbook 片段（并入 P3 备份恢复演练）

```text
1. 定位恢复点：pg_basebackup / WAL PITR 到目标实例（与 canonical/memory 同一
   consistency point，见 C12 backup manifest）。
2. 恢复后核验（psycopg 直查）：
   - SELECT tenant_id, source, created_at FROM tenant_persona_profiles;   -- 快照在
   - SELECT tenant_id, summary FROM memory_items WHERE memory_type='self'; -- 关系状态在
   - SELECT count(*) FROM persona_audit_events WHERE tenant_id=...;        -- 审计链在
3. 恢复点晚于某 tenant 的 onboarding → 该 tenant 回到 onboarding-required 态
   （门禁自动生效，用户重新走一次性设置；属预期行为，不是缺陷）。
4. 与账号恢复的联动（C5 runbook）：恢复点上的 auth_sessions/access_tokens
   digest 状态以 p0-retention-wiring 的凭据处置语义为准；旧凭据不复活。
5. 演练记录进 openspec/evidence/c9-persona-relationship/（P3 演练时补录）。
```

## 单写者约束的恢复含义

RelationshipState 写入 = `bootstrap/persona.py::PgRelationshipIO.write`
（memory_items self 行 upsert + persona_audit_events 追加，单事务）。恢复后
无需重放任何写操作——当前值即最近一次提交（`ORDER BY updated_at DESC`）。
