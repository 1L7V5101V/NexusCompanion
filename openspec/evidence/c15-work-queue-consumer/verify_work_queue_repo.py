"""C15 WorkItemRepository 真 PG 验证（用真实仓储代码，非 SQL 复制件）。

前置：本地 PG18（无 pgvector）+ 最小 schema（见 minimal_schema.sql）+ C15 迁移
（background_work_items 的 lease/flow 列、work_attempts 表）+ c15_side_effects 表。

运行：python verify_work_queue_repo.py
"""

from __future__ import annotations

import asyncio
import os
import uuid

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bootstrap.db.repository.control_plane_repo import (
    LeaseLostError,
    RedriveNotAllowedError,
    WorkItemNotFoundError,
    WorkItemRepository,
)

SYNC_URL = os.environ.get("NEXUS_TEST_PG_URL", "postgresql://nexus:nexus_dev@127.0.0.1:5433/nexus")
ASYNC_URL = SYNC_URL.replace("postgresql://", "postgresql+asyncpg://")

FAILS = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILS
    if not cond:
        FAILS += 1
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))


def reset() -> None:
    conn = psycopg.connect(SYNC_URL, autocommit=True)
    conn.execute("TRUNCATE work_attempts, background_work_items, canonical_conversations, "
                 "test_accounts, c15_side_effects CASCADE")
    conn.execute("INSERT INTO test_accounts (id, tenant_id, status, display_name) VALUES "
                 "(%s,'t1','active','a1'), (%s,'t2','active','a2')", (uuid.uuid4(), uuid.uuid4()))
    rows = conn.execute("SELECT id, tenant_id FROM test_accounts").fetchall()
    for acct_id, tenant in rows:
        conn.execute("INSERT INTO canonical_conversations (id, tenant_id, account_id) "
                     "VALUES (%s,%s,%s)", (uuid.uuid4(), tenant, acct_id))
    conn.close()


def add(tenant: str, *, kind: str = "maintenance", flow: str = "consolidation",
        status: str = "queued", next_at: str = "now()", attempt: int = 0,
        lease_owner: str | None = None, lease: str | None = None) -> str:
    wid = str(uuid.uuid4())
    lease_expr = ("now() + interval '60 seconds'" if lease == "future"
                  else "now() - interval '60 seconds'" if lease == "past" else "NULL")
    conn = psycopg.connect(SYNC_URL, autocommit=True)
    conn.execute(
        f"INSERT INTO background_work_items (id, tenant_id, work_kind, flow, status, "
        f"next_attempt_at, attempt_count, lease_owner, lease_expires_at) "
        f"VALUES (%s,%s,%s,%s,%s,{next_at},%s,%s,{lease_expr})",
        (wid, tenant, kind, flow, status, attempt, lease_owner),
    )
    conn.close()
    return wid


def row(wid: str) -> dict:
    conn = psycopg.connect(SYNC_URL, autocommit=True)
    r = conn.execute(
        "SELECT status, attempt_count, lease_owner, next_attempt_at, last_error, finished_at "
        "FROM background_work_items WHERE id=%s", (wid,)
    ).fetchone()
    conn.close()
    assert r is not None
    return {"status": r[0], "attempt_count": r[1], "lease_owner": r[2],
            "next_attempt_at": r[3], "last_error": r[4], "finished_at": r[5]}


def attempts(wid: str) -> list[str]:
    conn = psycopg.connect(SYNC_URL, autocommit=True)
    rs = conn.execute("SELECT outcome FROM work_attempts WHERE work_item_id=%s "
                      "ORDER BY started_at, id", (wid,)).fetchall()
    conn.close()
    return [r[0] for r in rs]


async def main() -> None:
    engine = create_async_engine(ASYNC_URL)
    repo = WorkItemRepository(async_sessionmaker(engine, expire_on_commit=False))
    try:
        # ── 1. 认领的两个 per-tenant 条件（ADR-4） ──
        reset()
        add("t1"); add("t1"); add("t1"); add("t2")
        got = await repo.claim_batch("w1")
        check("每租户每轮至多 1 条", len(got) == 2 and {g["tenant_id"] for g in got} == {"t1", "t2"},
              f"{[g['tenant_id'] for g in got]}")
        check("认领不改 attempt_count", all(g["attempt_count"] == 0 for g in got))
        got2 = await repo.claim_batch("w2")
        check("在途租户本轮不再被认领（条件①）", got2 == [], f"{[(g['tenant_id']) for g in got2]}")

        # ── 2. 心跳 ──
        wid = got[0]["id"]
        check("心跳成功", await repo.heartbeat("t1", wid, "w1") is True)
        check("他人续租失败（失租）", await repo.heartbeat("t1", wid, "w2") is False)

        # ── 3. record_work_succeeded：副作用与终态同事务（ADR-6） ──
        async def good_mutate(sess) -> None:
            await sess.execute(text("INSERT INTO c15_side_effects (note) VALUES ('ok')"))

        r = await repo.record_work_succeeded("t1", wid, "w1", mutate=good_mutate)
        conn = psycopg.connect(SYNC_URL, autocommit=True)
        n = conn.execute("SELECT count(*) FROM c15_side_effects").fetchone()[0]
        conn.close()
        check("成功副作用与终态同事务提交", r["status"] == "succeeded" and n == 1, f"status={r['status']} side={n}")
        check("成功写审计行", attempts(wid) == ["succeeded"], f"{attempts(wid)}")

        # 失败回滚：mutate 抛错 → 终态也不落
        reset()
        add("t1")
        w = (await repo.claim_batch("w1"))[0]["id"]

        async def bad_mutate(sess) -> None:
            await sess.execute(text("INSERT INTO c15_side_effects (note) VALUES ('boom')"))
            raise RuntimeError("handler 失败")

        try:
            await repo.record_work_succeeded("t1", w, "w1", mutate=bad_mutate)
            check("mutate 抛错应向上传播", False)
        except RuntimeError:
            check("mutate 抛错向上传播", True)
        st = row(w)
        conn = psycopg.connect(SYNC_URL, autocommit=True)
        n = conn.execute("SELECT count(*) FROM c15_side_effects").fetchone()[0]
        conn.close()
        check("整体回滚（终态未推进 + 副作用未落库）",
              st["status"] == "in_progress" and st["lease_owner"] == "w1" and n == 0,
              f"status={st['status']} side={n}")

        # ── 4. record_work_failed：attempt_count 唯一递增点 + 退避 + 死信 ──
        reset()
        w = add("t1")
        for i in range(1, 5):
            claimed = await repo.claim_batch(f"w{i}")
            check(f"第 {i} 次认领拿到该行", len(claimed) == 1 and claimed[0]["id"] == w, f"{len(claimed)}")
            res = await repo.record_work_failed("t1", w, f"w{i}", f"err{i}", max_attempts=5)
            check(f"第 {i} 次失败后 attempt_count={i} 且回 queued",
                  res["attempt_count"] == i and res["status"] == "queued",
                  f"count={res['attempt_count']} status={res['status']}")
            psycopg.connect(SYNC_URL, autocommit=True).execute(
                "UPDATE background_work_items SET next_attempt_at = now() - interval '1 second' WHERE id=%s",
                (w,))
        claimed = await repo.claim_batch("w5")
        check("第 5 次可认领", len(claimed) == 1)
        res = await repo.record_work_failed("t1", w, "w5", "err5", max_attempts=5)
        check("达上限进入死信终态", res["status"] == "failed" and res["finished_at"],
              f"status={res['status']}")
        check("死信不再被认领", await repo.claim_batch("w6") == [])
        check("逐次失败各留一行审计", attempts(w) == ["failed"] * 5, f"{attempts(w)}")

        # ── 5. redrive ──
        rd = await repo.redrive_work_item("t1", w, "已修好", operator="admin")
        check("redrive 复位 attempt_count 与状态",
              rd["attempt_count"] == 0 and rd["status"] == "queued", f"{rd['attempt_count']}/{rd['status']}")
        check("redrive 不抹除历史（5 失败 + 1 重投）",
              attempts(w) == ["failed"] * 5 + ["redrive"], f"{attempts(w)}")
        check("redrive 后可再认领", len(await repo.claim_batch("w7")) == 1)
        try:
            await repo.redrive_work_item("t1", w, "不该允许")
            check("非死信 redrive 应被拒", False)
        except RedriveNotAllowedError:
            check("非死信 redrive 被拒", True)

        # ── 6. release_for_retry（维护类延后不计失败） ──
        reset()
        w = add("t1")
        await repo.claim_batch("w1")
        rel = await repo.release_for_retry("t1", w, "w1", delay_seconds=30, note="interactive 忙")
        check("延后回 queued 且不消耗预算",
              rel["status"] == "queued" and rel["attempt_count"] == 0,
              f"{rel['status']}/{rel['attempt_count']}")
        check("延后写 released 审计行", attempts(w) == ["released"], f"{attempts(w)}")

        # ── 7. 崩溃：清扫复位不消耗预算 ──
        reset()
        w = add("t1", status="in_progress", lease_owner="dead", lease="past", attempt=0)
        swept = await repo.sweep_stale_leases()
        check("stale 被清扫复位", len(swept) == 1 and swept[0]["id"] == w, f"{len(swept)}")
        check("清扫带回前 owner", swept[0]["prev_lease_owner"] == "dead")
        st = row(w)
        check("清扫不碰 attempt_count", st["attempt_count"] == 0, f"{st['attempt_count']}")
        check("清扫写 recovered 审计行", attempts(w) == ["recovered"], f"{attempts(w)}")
        check("清扫后租约清空", st["lease_owner"] is None and st["status"] == "queued")
        check("清扫后可再认领", len(await repo.claim_batch("alive")) == 1)
        check("租约未过期不被清扫", await repo.sweep_stale_leases() == [])

        # 反复崩溃不消耗预算、也不判死
        reset()
        w = add("t1")
        for i in range(7):
            await repo.claim_batch(f"w{i}")
            psycopg.connect(SYNC_URL, autocommit=True).execute(
                "UPDATE background_work_items SET lease_expires_at = now() - interval '1 second' WHERE id=%s", (w,))
            await repo.sweep_stale_leases()
        st = row(w)
        check("7 次崩溃后仍 queued 且 attempt_count=0 且未判死",
              st["status"] == "queued" and st["attempt_count"] == 0,
              f"{st['status']}/{st['attempt_count']}")
        check("7 次崩溃仍可认领", len(await repo.claim_batch("final")) == 1)

        # ── 8. 失租不得写状态 ──
        reset()
        w = add("t1")
        await repo.claim_batch("owner-a")
        try:
            await repo.record_work_failed("t1", w, "owner-b", "x")
            check("失租记录失败应被拒", False)
        except LeaseLostError:
            check("失租记录失败被拒", True)
        try:
            await repo.record_work_succeeded("t1", w, "owner-b")
            check("失租推进成功应被拒", False)
        except LeaseLostError:
            check("失租推进成功被拒", True)

        # ── 9. 租户隔离 ──
        reset()
        w = add("t1")
        await repo.claim_batch("w")
        check("跨租户 get 不可见", await repo.get_work_item("t2", w) is None)
        check("同租户 get 可见", (await repo.get_work_item("t1", w)) is not None)
        check("跨租户审计不可见", await repo.list_work_attempts("t2", w) == [])
        try:
            await repo.record_work_succeeded("t2", w, "w")
            check("跨租户推进应被拒", False)
        except WorkItemNotFoundError:
            check("跨租户推进被拒（NotFound）", True)

        # ── 10. 只追加：审计流不被改写 ──
        reset()
        w = add("t1")
        await repo.claim_batch("w")
        await repo.release_for_retry("t1", w, "w", delay_seconds=1)
        psycopg.connect(SYNC_URL, autocommit=True).execute(
            "UPDATE background_work_items SET next_attempt_at = now() - interval '1 second' WHERE id=%s", (w,))
        await repo.claim_batch("w2")
        await repo.record_work_failed("t1", w, "w2", "boom", max_attempts=5)
        before = attempts(w)
        check("审计流含 released→failed 且顺序稳定", before == ["released", "failed"], f"{before}")
    finally:
        await engine.dispose()

    print()
    if FAILS:
        print(f"{FAILS} 项失败")
        raise SystemExit(1)
    print("全部 WorkItemRepository 验证通过")


asyncio.run(main())
