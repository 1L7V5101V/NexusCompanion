# C15 WorkItemRepository 验证（真 PG18，2026-09-21）

## 方法

`WorkItemRepository` 尚未进入仓库测试路径能覆盖的范围（本机无 pgvector，`tests/control_plane/`
的完整 alembic 链不可用），因此在本地 PG18 + 最小 schema 上**用真实仓储代码**（不是 SQL
复制件）跑了一组语义验证：

```bash
# 前置：本地 PG18（无 pgvector）+ minimal_schema.sql + C15 迁移 + c15_side_effects 表
NEXUS_TEST_PG_URL=postgresql://nexus:nexus_dev@127.0.0.1:5433/nexus \
  PYTHONPATH=<repo> python verify_work_queue_repo.py
```

- 脚本：`verify_work_queue_repo.py`（本目录，可直接复跑）
- 原始输出：`work-queue-repo-verification.txt`（**43 PASS / 0 FAIL**，exit 0）
- 仓库内的正式回归版本：`tests/control_plane/test_work_queue_state_machine.py`（PG-gated，
  与下面各条一一对应，待 task 7.2 在带 pgvector 的 dev 库上执行）

## 覆盖的语义（与 spec requirement 对应）

| 组 | 断言要点 |
| --- | --- |
| 认领（ADR-4） | 每租户每轮至多 1 条；**在途互斥**（该租户已有有效租约 → 本轮不认领它的任何项，即 ADR-4 条件①反例回归）；认领**不改** `attempt_count` |
| 租约 | 心跳成功；他人续租返回 False（失租） |
| 同事务副作用（ADR-6） | `mutate` 与 `succeeded` 同提交（副作用行确实落库）；`mutate` 抛错 → **整体回滚**（终态仍 `in_progress`、副作用 0 行） |
| 失败与死信（ADR-3 (B)） | 逐次失败 `attempt_count` 1→2→3→4→5；前 4 次回 `queued` + 退避；第 5 次进 `failed`；**死信不再被认领**；逐次失败各留 1 行审计 |
| redrive | 复位 `attempt_count=0`、回 `queued`；**历史保留**（`failed×5 + redrive`）；非死信 redrive 被拒；redrive 后可再认领 |
| 延后（ADR-5） | `release_for_retry` 回 `queued`、**不消耗预算**、留 `released` 审计行 |
| 崩溃恢复（ADR-3） | stale 被清扫复位为 `queued`、带回前 owner、**不碰 `attempt_count`**、留 `recovered` 审计行、可再认领；租约未过期不受影响 |
| **(B) 核心不变量** | **连续 7 次崩溃 → 仍 `queued`、`attempt_count=0`、从未判死、始终可认领** |
| 失租禁写 | 失租后 `record_work_failed` / `record_work_succeeded` / `release_for_retry` 均抛 `LeaseLostError` |
| 租户隔离 | 跨租户 `get_work_item` 不可见、`list_work_attempts` 为空、推进抛 `WorkItemNotFoundError` |
| 审计流 | `released → failed` 顺序稳定；`list_work_attempts` 与库内一致（只追加） |

## 为什么这组验证值得单独留档

1. **claim SQL 的形状无法靠阅读确认**：ADR-4 的两个 per-tenant 条件、`FOR UPDATE OF w
   SKIP LOCKED` 与相关子查询 `NOT EXISTS` 同层是否合法，只能实测；当前形状已验证可用。
2. **(B) 定案的核心不变量被直接证明**：「反复崩溃不消耗预算、不判死」是这条设计选择的
   全部意义，上面的 7 次崩溃用例就是它的证据。
3. **同事务副作用确实回滚**：ADR-6 的 effectively-once 承诺需要「终态与副作用要么都在
   要么都不在」，上面的 `mutate` 抛错用例证明了两者一起回滚。

## 与正式路径的差距（待 task 7.2 补）

- 用**手工最小 schema**，未覆盖 `background_work_items` 之外的表与完整 alembic 链；
- 未覆盖 `WorkQueueWorker`（task 3）与 bootstrap 接线（task 4）；
- 因此 task 7.2「带 PG 全量回归」仍是必要证据：

```bash
python scripts/regression.py --start-pg \
  --evidence openspec/evidence/c15-work-queue-consumer/pytest-regression.txt
```
