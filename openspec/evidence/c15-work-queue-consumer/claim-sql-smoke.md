# C15 claim SQL 真 PG 冒烟验证（2026-09-21）

## 目的与方法

本机无 docker、无 pgvector，`tests/control_plane/conftest.py` 的完整 alembic 路径
不可用（迁移链中 `a3d5c7e9f1b2` 需要 `vector(1024)`），全部 control-plane 集成测试
会整组 skip。为了**在写仓储代码之前**验证 claim SQL 的语法与语义，本机搭了一个
**不含 pgvector** 的 PG18 实例，只建了 C15 需要的三张表（`test_accounts` /
`canonical_conversations` / `background_work_items`），并用
`alembic stamp f3c8a9d2e7b4` + `alembic upgrade head` 只跑 C15 那一个迁移。

- 环境：PostgreSQL 18.6（Windows x86_64，`D:/pg18local/data`，端口 5433，trust 认证）
- 迁移验证：`upgrade` 建出 5 列 + `ix_background_work_items_claim`；
  `downgrade -1` 后两计数均为 0；再 `upgrade head` 成功 —— **expand/rollback 双向可逆**
- SQL 验证脚本：`D:/pg18local/claim_sql_test.py`（15 项断言，全部 PASS）

> 该实例**不是**仓库测试路径，仅为本次 SQL 形状验证；正式回归仍须按
> `scripts/regression.py --start-pg` 在带 pgvector 的 dev 库上跑（见 §差距）。

## 关键发现（本 change 在实现阶段修正的设计缺陷）

**「每轮每租户至多 1 条」不足以实现「每租户在途 ≤ 1」。**

第一次实测（claim SQL 只含「轮内去重」一个 per-tenant 条件）：

```
PASS  每轮每 tenant 至多 1 条  [claimed=['t1', 't2']]
PASS  claim 后 attempt_count=1  [[1, 1]]
PASS  claim 后 status=in_progress
PASS  claim 后 lease_owner 写入
FAIL  持租约中的行不被二次认领  [claimed=[(UUID('fe2cdf23…'), 't1'), (UUID('b215dd67…'), 't2')]]
```

原因：tenant `t1` 有 3 条 `queued`。第一次 claim 取走 1 条 → `in_progress`（租约有效）；
第二次 claim 时，**「已持租的那条」不再满足 due 判据，于是退出候选集**，该 tenant 的
下一条 `queued` 行成了它自己的最小候选 → 被认领。结果：**同租户两条同时在途**。

这正是 design ADR-4 要排除的陷阱：若这两条投入同一 tenant lane 排队，排队那条
**持租却不心跳**（心跳只在执行期运行），等待超过 `lease_ttl` 后会被清扫复位并被
另一轮认领 → **同一条 work 被重复执行**。

**修正**：claim SQL 必须同时含两个 per-tenant 条件 ——
（1）**在途互斥**：该 tenant 已有 `in_progress` 且租约未过期的行 → 本轮不认领它的任何工作项；
（2）**轮内去重**：同 tenant 本轮至多 1 条（取 `(next_attempt_at, created_at, id)` 最小者）。

## 修正后复测（15/15 PASS）

```
PASS  每轮每 tenant 至多 1 条                    [claimed=['t1', 't2']]
PASS  claim 后 attempt_count=1                   [[1, 1]]
PASS  claim 后 status=in_progress
PASS  claim 后 lease_owner 写入
PASS  持租约中的行不被二次认领                    [claimed=[]]
PASS  下一轮仍每 tenant 1 条                     [claimed=2]
PASS  未到期 failed 不认领
PASS  到期 failed 可认领
PASS  attempt_count 达上限不认领
PASS  attempt_count 未达上限可认领
PASS  stale in_progress 被接管
PASS  租约未过期的 in_progress 不被接管
PASS  batch_size 限制认领数
PASS  并发认领不重复                             [t1=1 t2=0]
PASS  最终只有 1 个 owner 写入                    [owner=('c1',)]
```

其中两项是纯语法/并发风险，无法通过阅读代码确认，只能实测：

1. **`FOR UPDATE OF w SKIP LOCKED` 与相关子查询 `NOT EXISTS` 可同层使用** —— 这是最终采用
   的 SQL 形状。反例：`row_number() OVER (PARTITION BY tenant_id …)` **不能**与
   `FOR UPDATE` 同层（PostgreSQL 直接拒绝：`FOR UPDATE is not allowed with window
   functions`），`DISTINCT ON` 同理。因此「用窗口函数去重」的写法本就不可行。
2. **并发认领同一行恰好只有一个成功**（`t1=1, t2=0`，最终 owner 为 `c1`）——
   两个事务同时认领同一行时不出现重复认领。

## 与正式测试路径的差距（必须补）

- 本验证用的是**手工最小 schema**，未覆盖 `background_work_items` 之外的表；
- 未覆盖 ORM 映射（`BackgroundWorkItemModel`）与 `_work_item_to_dict` 的字段一致性；
- 未覆盖 heartbeat / succeeded / failed / sweep 四组仓储方法（本次只验了 claim SQL）。

因此 task 7.2「带 PG 全量回归」仍是**必要**证据，须在带 pgvector 的 dev 库上执行：

```bash
python scripts/regression.py --start-pg \
  --evidence openspec/evidence/c15-work-queue-consumer/pytest-regression.txt
```
