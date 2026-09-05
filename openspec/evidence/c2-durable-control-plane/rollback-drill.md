# C2 回滚演练记录（Create → Verify → Enable → rollback）

- 日期：2026-09-06
- 执行脚本：`rollback_drill.py`（可重跑；scratch DB `nexus_c2drill`，演练结束保留数据以验证「数据保留」语义）
- 原始输出：`rollback-drill-output.txt`（13 项全 PASS）
- 依据：design.md §5 Rollback 策略 / PILOT_ROADMAP §5.9.9（首次启用 + rollback = 关入口 + 保留 PG 数据，不反向同步旧单体库）

## 演练步骤与结果

| 阶段 | 动作 | 验证点 | 结果 |
| --- | --- | --- | --- |
| Create | 空库 `alembic upgrade head`（链路 `47460ba069a5 → … → f3c8a9d2e7b4`） | 七表齐备；seed = not_applicable（outbox 空基线） | PASS |
| Verify | 关键约束/索引断言 | `uq_message_dedup_keys_source` / `uq_message_dedup_keys_client`（部分唯一）、`uq_outbound_delivery_intents_idempotency_key`、`ix_outbound_delivery_intents_claim` 在位 | PASS |
| Enable | 模拟入口开放：T1（dedupe + canonical user message + inbox + queued turn）→ T2（final + turn terminal + pending intent）→ delivery worker 投递 | 每表行数符合预期（dedupe ×1 / inbox ×1 / turn ×1 / message ×2 / intent ×1 / attempt ×1）；intent 由 ack 推进到 `sent`（provider receipt `drill-receipt-001`） | PASS |
| rollback | 关闭入口 + 停 worker（进程级开关），不删数据 | 行数与 Enable 时逐表一致；intent 终态/attempt_count/sent_at 保留；`test_accounts` 数据保留，无任何反向同步 | PASS |

## 结论

- migration 层可回退：`downgrade()` 删除七表（见 `tests/control_plane/test_migration.py::test_downgrade_upgrade_cycle`，C2 未 cutover、无运行时读取者）。
- 运行层回滚 = 关闭 Pilot 入口 + 停止 delivery worker，已产生的 PostgreSQL 数据**保留**用于修复后继续；不反向同步 SQLite、不把旧单体库当回滚目标（§10 DECIDED）。
- 演练可重跑：重复执行脚本会先 DROP `nexus_c2drill` 再走全流程。
