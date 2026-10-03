# task-5.1 / 5.2 / 5.3 — 凭据过期处置（ADR-4）

日期：2026-10-03　测试文件：`tests/retention/test_credential_purge.py`（10 passed）

## 清除谓词（三条件同时满足，缺一不清）

```
revoked_at IS NOT NULL
∧ expires_at IS NOT NULL AND expires_at < now()   （token 的 NULL expires_at = 永不过期，不清）
∧ revoked_at < now() - purge_grace_s              （宽限锚点 = 撤销时刻）
∧ digest IS NOT NULL                              （已清除行天然不再入选 → 幂等）
```

处置 = `UPDATE digest = NULL`，**不删行**；`account_id/principal_type/created_at/
expires_at/revoked_at/revoked_reason/digest_version` 等归属与时间 metadata 全部保留。

## Scenario / 验证项对照

| spec Scenario / task | 用例 | 结果 |
| --- | --- | --- |
| 已撤销且已过期 + 超宽限 → 摘要清除、行保留 | `test_revoked_expired_past_grace_cleared_metadata_kept` | digest 不可再查、归属/时间/原因 metadata 完整可查 |
| 活跃会话不被触碰 | `test_active_credentials_untouched` | purge 后 `validate_session` 照常通过 |
| 已撤销但未过期不清除 | `test_revoked_but_not_expired_not_cleared` | digest 原样 |
| token 无 expires_at 不清除 | `test_token_without_expiry_never_cleared` | revoked 100 天仍不清 |
| 宽限窗口内不清除 | `test_grace_window_holds_purge` | 撤销 10 天 < 30d 宽限 → 本轮只清 token（40 天） |
| **fail-closed**（5.2） | `test_fail_closed_after_purge` | 按原摘要 `validate_session` 抛 `SessionInvalidError`（401 语义），NULL 摘要不会被等值查询命中 |
| 既有查询不受影响（5.2） | `test_count_active_admin_sessions_unaffected` | purge 前后 `count_active_admin_sessions` 一致 |
| **NULLS DISTINCT 固化**（1.3） | `test_null_digest_rows_coexist_nulls_distinct` | 两条不同 digest 的已清除行（均 NULL）在 UNIQUE 下共存；若部署 PG 行为不同此用例失败 → 回 ADR-4 改 partial unique index |
| 幂等与批次（5.3） | `test_purge_idempotent_and_batched` | batch_size=1 三行分三批收敛；第二轮 cleared=0 |
| 演练零变更 | `test_dry_run_purge_counts_without_mutating` | dry-run 报"将清除 2"，两表零变更 |

迁移（task 1.1）与模型同步（task 1.2）见 `task-1.1-migration.md`。
