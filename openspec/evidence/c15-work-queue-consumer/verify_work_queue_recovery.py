"""C15 task 1.3 + 4.4 真 PG 验证（用真实 worker + 真实仓储，端到端）。

- 1.3 expand-only 兼容性：只写旧列的行必须能取到新列 DEFAULT/NULL 且可被认领。
- 4.4 崩溃恢复 e2e：认领 → 模拟崩溃（租约过期）→ worker 启动清扫复位 → 重新认领 →
  handler 成功 → 终态 succeeded，且期间 attempt_count 始终为 0（崩溃不消耗预算）。

前置：本地 PG18 + minimal_schema.sql + C15 迁移 + c15_side_effects 表。
运行：PYTHONPATH=<repo> python verify_work_queue_recovery.py
"""

from __future__ import annotations

import asyncio
import os
import uuid

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bootstrap.db.repository.control_plane_repo import (
    TurnControlRepository,
    WorkItemRepository,
)
from bootstrap.work_queue_worker import (
    WorkItemEnvelope,
    WorkQueueWorker,
    WorkQueueWorkerConfig,
)

SYNC_URL = os.environ.get("NEXUS_TEST_PG_URL", "postgresql://nexus:nexus_dev@127.0.0.1:5433/nexus")
ASYNC_URL = SYNC_URL.replace("postgresql://", "postgresql+asyncpg://")

FAILS = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILS
    if not cond:
        FAILS += 1
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))


def sql(stmt: str, params: tuple = ()) -> list[tuple]:
    conn = psycopg.connect(SYNC_URL, autocommit=True)
    try:
        cur = conn.execute(stmt, params)
        return cur.fetchall() if cur.description else []
    finally:
        conn.close()


def reset() -> None:
    sql("TRUNCATE work_attempts, background_work_items, canonical_conversations, "
        "test_accounts, c15_side_effects CASCADE")
    sql("INSERT INTO test_accounts (id, tenant_id, status, display_name) VALUES "
        "(%s,'t1','active','a1')", (uuid.uuid4(),))
    acct = sql("SELECT id FROM test_accounts")[0][0]
    sql("INSERT INTO canonical_conversations (id, tenant_id, account_id) VALUES (%s,'t1',%s)",
        (uuid.uuid4(), acct))


class _Handler:
    """最小 handler：persist 写一行副作用（与终态同事务）。"""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.persisted: list[str] = []

    async def execute(self, envelope: WorkItemEnvelope) -> object:
        self.executed.append(envelope.work_item_id)
        return f"result-{envelope.work_item_id}"

    async def persist(self, session, envelope: WorkItemEnvelope, result: object) -> None:
        await session.execute(
            text("INSERT INTO c15_side_effects (note) VALUES (:n)"),
            {"n": f"persisted-{envelope.work_item_id}"},
        )
        self.persisted.append(envelope.work_item_id)


async def main() -> None:
    engine = create_async_engine(ASYNC_URL)
    repo = WorkItemRepository(async_sessionmaker(engine, expire_on_commit=False))
    try:
        # ── 1.3 expand-only 兼容性 ──
        reset()
        wid = str(uuid.uuid4())
        # 只写旧列（C2 时代的 INSERT 形态）：不指定任何 C15 新列
        sql("INSERT INTO background_work_items (id, tenant_id, work_kind, status) "
            "VALUES (%s,'t1','maintenance','queued')", (wid,))
        row = sql("SELECT attempt_count, next_attempt_at, last_error, flow, lease_owner, "
                  "lease_expires_at FROM background_work_items WHERE id=%s", (wid,))[0]
        check("旧列 INSERT → attempt_count 取 DEFAULT 0", row[0] == 0, f"{row[0]}")
        check("旧列 INSERT → next_attempt_at 非空（DEFAULT now()）", row[1] is not None)
        check("旧列 INSERT → last_error/flow/lease 为 NULL",
              row[2] is None and row[3] is None and row[4] is None and row[5] is None)
        claimed_old = await repo.claim_batch("probe-expand")
        check("旧列 INSERT 的行可被正常认领",
              len(claimed_old) == 1 and claimed_old[0]["id"] == wid, f"{len(claimed_old)}")

        # ── 4.4 崩溃恢复 e2e ──
        reset()
        wid2 = str(uuid.uuid4())
        sql("INSERT INTO background_work_items (id, tenant_id, work_kind, flow, status) "
            "VALUES (%s,'t1','maintenance','consolidation','queued')", (wid2,))

        first = _Handler()
        worker = WorkQueueWorker(
            repo,
            {"consolidation": first},
            config=WorkQueueWorkerConfig(lease_ttl_seconds=60.0, heartbeat_interval_seconds=20.0),
            worker_id="w1",
        )
        # 第一步：认领成功（不执行 handler），随后模拟进程崩溃
        claimed = await repo.claim_batch("w1")
        check("首次认领成功", len(claimed) == 1 and claimed[0]["id"] == wid2)
        sql("UPDATE background_work_items SET lease_expires_at = now() - interval '1 second' "
            "WHERE id=%s", (wid2,))
        st = sql("SELECT status, attempt_count FROM background_work_items WHERE id=%s", (wid2,))[0]
        check("崩溃后仍是 in_progress 且 attempt_count=0（claim 不递增）",
              st[0] == "in_progress" and st[1] == 0, f"{st[0]}/{st[1]}")

        # 第二步：worker 启动清扫（recover_stale）→ 复位 queued
        swept = await worker.recover_stale()
        check("启动清扫复位 1 条", swept == 1, f"{swept}")
        st = sql("SELECT status, attempt_count, lease_owner FROM background_work_items WHERE id=%s",
                 (wid2,))[0]
        check("复位为 queued、lease 清空、attempt_count 仍为 0",
              st[0] == "queued" and st[1] == 0 and st[2] is None, f"{st}")
        outcomes = [r[0] for r in sql(
            "SELECT outcome FROM work_attempts WHERE work_item_id=%s ORDER BY started_at", (wid2,))]
        check("清扫留下 recovered 审计行", outcomes == ["recovered"], f"{outcomes}")

        # 第三步：重启后的 worker 重新认领并成功执行
        second = _Handler()
        worker2 = WorkQueueWorker(
            repo, {"consolidation": second}, worker_id="w2"
        )
        processed = await worker2.process_once()
        check("重启后重新认领并执行成功", processed == 1 and second.executed == [wid2],
              f"processed={processed} executed={second.executed}")
        st = sql("SELECT status, attempt_count, finished_at FROM background_work_items "
                 "WHERE id=%s", (wid2,))[0]
        check("终态 succeeded 且 attempt_count=0（崩溃全程未消耗预算）",
              st[0] == "succeeded" and st[1] == 0 and st[2] is not None, f"{st}")
        side = sql("SELECT count(*) FROM c15_side_effects WHERE note=%s",
                   (f"persisted-{wid2}",))[0][0]
        check("副作用与终态同事务落库", side == 1, f"side={side}")
        outcomes = [r[0] for r in sql(
            "SELECT outcome FROM work_attempts WHERE work_item_id=%s ORDER BY started_at", (wid2,))]
        check("审计流 = recovered → succeeded", outcomes == ["recovered", "succeeded"], f"{outcomes}")
        # ── 5.2 入队幂等（idempotency_key）× 认领租约（lease）组合 ──
        reset()
        control = TurnControlRepository(
            async_sessionmaker(engine, expire_on_commit=False)
        )
        conv_id = sql("SELECT id FROM canonical_conversations WHERE tenant_id='t1'")[0][0]
        first = await control.create_work_item(
            "t1", "maintenance", flow="consolidation",
            conversation_id=conv_id, idempotency_key="dup-key",
        )
        second = await control.create_work_item(
            "t1", "maintenance", flow="consolidation",
            conversation_id=conv_id, idempotency_key="dup-key",
        )
        check("重复入队（同 idempotency_key）只产生一行",
              first["id"] == second["id"], f"{first['id']} vs {second['id']}")
        rows = sql("SELECT count(*) FROM background_work_items WHERE idempotency_key='dup-key'")[0][0]
        check("库中该键只有 1 行", rows == 1, f"{rows}")

        dup_handler = _Handler()
        dup_worker = WorkQueueWorker(
            repo, {"consolidation": dup_handler}, worker_id="w-dup"
        )
        await dup_worker.process_once()
        await dup_worker.process_once()  # 再跑一轮：不应有第二条可执行
        check("同键工作项只被执行一次", dup_handler.executed == [first["id"]],
              f"{dup_handler.executed}")
        side = sql("SELECT count(*) FROM c15_side_effects")[0][0]
        check("副作用只落一次（幂等 + 租约叠加）", side == 1, f"side={side}")
    finally:
        await engine.dispose()

    print()
    if FAILS:
        print(f"{FAILS} 项失败")
        raise SystemExit(1)
    print("1.3 + 4.4 验证通过")


asyncio.run(main())
