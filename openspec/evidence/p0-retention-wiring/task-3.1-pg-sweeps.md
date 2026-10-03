# task-3.1(游标落地) / 3.2 / 3.4 / 3.5 — 仓储侧分批删除与水位不回退

日期：2026-10-03

## 变更

| 文件 | 内容 |
| --- | --- |
| `alembic/versions/d0a9b7c3e1f5_retention_replay_consumed_seq.py` | **新**（expand-only）：`webchat_replay_counters.consumed_seq BIGINT NULL`（task 3.1 游标持久化载体；纯加列，downgrade drop 该列） |
| `bootstrap/db/models/control_plane.py` | `WebchatReplayCounterModel.consumed_seq: Mapped[int \| None]` |
| `bootstrap/db/repository/control_plane_repo.py` | `WebchatReplayRepository`：`record_consumed_cursor`（GREATEST(既有, LEAST(after_seq, 水位))，只进不退、声明超前不推高；计数器行不存在 no-op）、`consumed_seq`（读）、`list_frame_conversations`、`count_replay_frames`、`delete_frames_window_batch`（ADR-8 三判据单批：非最近 N 帧 ∧ (seq≤游标 ∨ created_at<天花板)，dry_run 只统计）；`ToolAuditRepository`：`count_audit_rows`/`count_expired_audit`/`delete_expired_audit_batch`；`WorkItemRepository`：work_attempts 三件套（按 `started_at`，**只删审计流行，background_work_items 不动**） |
| `bootstrap/db/repository/auth_repo.py` | `AdminRepository`：admin_audit_events 三件套（按现有列裁剪，ADR-6 不统一字段形状）；`CredentialRepository`：`purge_expired_credential_digests`（UPDATE 置 NULL 不删行；宽限锚点 = revoked_at）、`count_purgeable_credentials`（dry-run，判据与实删逐字一致）、`count_credential_rows` |

## 关键实现语义（对照 tasks 验证项）

- **DB 时钟**（ADR-5）：全部时间比较用 `now() - (:seconds * interval '1 second')`，
  与 claim/now() 同源；删除批 = `ORDER BY <ts> ASC LIMIT :batch` CTE DELETE RETURNING
  （顺带取回 `octet_length` 作体量估算），单轮 `max_batches` 循环由 sweeper 编排。
- **水位不回退**（3.4）：`delete_frames_window_batch` 只删 `webchat_replay_frames` 行，
  计数器行零触碰；`current_seq()`（读 counter.next_seq-1）与 counter.next_seq
  在删除前后不变（测试断言）。帧缺失读侧语义复用既有
  `replay_after`（`after_seq < oldest-1 → None → replay_required`），
  对照 `tests/auth_provisioning/test_webchat_rebuild_reconcile.py` 同源降级路径。
- **consumed_seq NULL 语义**：无客户端声明 → 消费侧判据不生效（`COALESCE(consumed_seq,0)`
  使 `seq<=0` 恒假），帧只受下限/天花板约束。

## 测试证据（tests/retention/test_pg_sweeps.py 10 passed + test_replay_window.py 6 passed）

- 窗口内保留 / 窗口外删除 / 计数如实 / 第二轮为 0（幂等）；
- batch_size=5 × max_batches=2 上限：13 条过期首轮删 10、次轮收敛 3、第三轮 0（3.5）；
- dry-run 零变更且与实删轮计数一致；
- 死信 intent + 业务消息/inbox/turn 跑完整轮行数不变（见 `task-3.2-no-delete-set.md`）;
- 删除后 `current_seq` 与 `counter.next_seq` 不变（3.4，`test_replay_deletion_does_not_regress_seq_watermark`）;
- 补发窗口四 Scenario（3.6，见 `task-3.6-replay-window.md`）。
