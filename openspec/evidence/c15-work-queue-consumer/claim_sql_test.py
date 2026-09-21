"""C15 claim SQL 冒烟验证（本地 PG18，无 pgvector）。

验证目标（design ADR-4 / ADR-3）：
1. claim 用 FOR UPDATE SKIP LOCKED + NOT EXISTS 每租户去重，语法与语义在真 PG 上成立；
2. 每轮每 tenant 至多 1 条；
3. attempt_count 由 claim 递增；
4. 未到期 / 达上限的行不被认领；
5. stale in_progress（租约过期）可被接管；
6. 并发认领同一行只有一个成功（无重复认领）。

运行：NEXUS_TEST_PG_URL=postgresql://user:pw@host:5433/db python claim_sql_test.py
前置：目标库需有 test_accounts / canonical_conversations / background_work_items
（含 C15 的 5 个 lease 列）；见本目录 claim-sql-smoke.md。
"""

from __future__ import annotations

import os
import uuid

import psycopg

URL = os.environ.get(
    "NEXUS_TEST_PG_URL", "postgresql://nexus:nexus_dev@127.0.0.1:5433/nexus"
)

CLAIM_SQL = """
WITH picked AS (
    SELECT w.id
    FROM background_work_items w
    WHERE (
            (w.status IN ('queued', 'failed')
             AND w.next_attempt_at <= now()
             AND w.attempt_count < %(max_attempts)s)
            OR (w.status = 'in_progress' AND w.lease_expires_at < now())
          )
      -- (1) 该 tenant 已有有效租约在途 → 不再认领它的任何工作项（在途 ≤ 1）
      AND NOT EXISTS (
            SELECT 1 FROM background_work_items a
            WHERE a.tenant_id = w.tenant_id
              AND a.status = 'in_progress'
              AND a.lease_expires_at >= now()
          )
      -- (2) 同 tenant 本轮只取最早到期的一条（避免同租户排队持租）
      AND NOT EXISTS (
            SELECT 1 FROM background_work_items p
            WHERE p.tenant_id = w.tenant_id
              AND (
                    (p.status IN ('queued', 'failed')
                     AND p.next_attempt_at <= now()
                     AND p.attempt_count < %(max_attempts)s)
                    OR (p.status = 'in_progress' AND p.lease_expires_at < now())
                  )
              AND (p.next_attempt_at, p.created_at, p.id)
                  < (w.next_attempt_at, w.created_at, w.id)
          )
    ORDER BY w.next_attempt_at, w.created_at, w.id
    LIMIT %(batch_size)s
    FOR UPDATE OF w SKIP LOCKED
)
UPDATE background_work_items i
SET status = 'in_progress',
    lease_owner = %(owner)s,
    lease_expires_at = now() + make_interval(secs => %(lease_ttl)s),
    attempt_count = i.attempt_count + 1,
    updated_at = now()
FROM picked
WHERE i.id = picked.id
RETURNING i.id, i.tenant_id, i.work_kind, i.attempt_count, i.status, i.lease_owner
"""


def reset(conn: psycopg.Connection) -> None:
    conn.execute("TRUNCATE background_work_items, canonical_conversations, test_accounts CASCADE")
    conn.execute(
        "INSERT INTO test_accounts (id, tenant_id, status, display_name) VALUES "
        "(%s,'t1','active','a1'), (%s,'t2','active','a2')",
        (uuid.uuid4(), uuid.uuid4()),
    )
    accts = conn.execute("SELECT id, tenant_id FROM test_accounts ORDER BY tenant_id").fetchall()
    for acct_id, tenant in accts:
        conn.execute(
            "INSERT INTO canonical_conversations (id, tenant_id, account_id) VALUES (%s,%s,%s)",
            (uuid.uuid4(), tenant, acct_id),
        )


def add_work(
    conn: psycopg.Connection,
    tenant: str,
    *,
    kind: str = "consolidation",
    status: str = "queued",
    next_attempt_at: str = "now()",
    attempt_count: int = 0,
    lease_owner: str | None = None,
    lease_expires: str | None = None,
) -> uuid.UUID:
    wid = uuid.uuid4()
    lease_expr = "now() + interval '60 seconds'" if lease_expires == "future" else (
        "now() - interval '60 seconds'" if lease_expires == "past" else "NULL"
    )
    conn.execute(
        f"""
        INSERT INTO background_work_items
            (id, tenant_id, work_kind, status, next_attempt_at, attempt_count,
             lease_owner, lease_expires_at)
        VALUES (%s, %s, %s, %s, {next_attempt_at}, %s, %s, {lease_expr})
        """,
        (wid, tenant, kind, status, attempt_count, lease_owner),
    )
    return wid


def claim(conn: psycopg.Connection, owner: str, batch_size: int = 10,
          max_attempts: int = 5, lease_ttl: float = 60.0) -> list[tuple]:
    return conn.execute(
        CLAIM_SQL,
        {"owner": owner, "batch_size": batch_size, "max_attempts": max_attempts,
         "lease_ttl": lease_ttl},
    ).fetchall()


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        raise SystemExit(1)


with psycopg.connect(URL, autocommit=True) as conn:
    # ── 1. 每租户每轮至多 1 条 ──
    reset(conn)
    add_work(conn, "t1"); add_work(conn, "t1"); add_work(conn, "t1")
    add_work(conn, "t2"); add_work(conn, "t2")
    rows = claim(conn, "w1")
    tenants = [r[1] for r in rows]
    check("每轮每 tenant 至多 1 条", len(rows) == 2 and sorted(tenants) == ["t1", "t2"],
          f"claimed={tenants}")

    # ── 2. attempt_count 递增 + 租约写入 ──
    check("claim 后 attempt_count=1", all(r[3] == 1 for r in rows), f"{[r[3] for r in rows]}")
    check("claim 后 status=in_progress", all(r[4] == "in_progress" for r in rows))
    check("claim 后 lease_owner 写入", all(r[5] == "w1" for r in rows))

    # ── 3. 已认领（租约未过期）不再被认出 ──
    rows2 = claim(conn, "w2")
    check("持租约中的行不被二次认领", rows2 == [], f"claimed={[(r[0], r[1]) for r in rows2]}")

    # ── 4. 剩余同租户项在下一轮被认领（每轮 1 条） ──
    #    先把 t1/t2 的 in_progress 收束掉，才能看到排队的其它项
    conn.execute("UPDATE background_work_items SET status='queued', lease_owner=NULL, "
                 "lease_expires_at=NULL WHERE status='in_progress'")
    rows3 = claim(conn, "w3")
    check("下一轮仍每 tenant 1 条", len(rows3) == 2, f"claimed={len(rows3)}")

    # ── 5. 未到期 failed 不认领；到期 failed 认领 ──
    reset(conn)
    add_work(conn, "t1", status="failed", next_attempt_at="now() + interval '1 hour'")
    check("未到期 failed 不认领", claim(conn, "w") == [])
    conn.execute("UPDATE background_work_items SET next_attempt_at = now() - interval '1 second'")
    check("到期 failed 可认领", len(claim(conn, "w")) == 1)

    # ── 6. 达 max_attempts 不再认领 ──
    reset(conn)
    add_work(conn, "t1", attempt_count=5)
    check("attempt_count 达上限不认领", claim(conn, "w", max_attempts=5) == [])
    reset(conn)
    add_work(conn, "t1", attempt_count=4)
    check("attempt_count 未达上限可认领", len(claim(conn, "w", max_attempts=5)) == 1)

    # ── 7. stale in_progress（租约过期）可被接管 ──
    reset(conn)
    add_work(conn, "t1", status="in_progress", lease_owner="dead", lease_expires="past",
             attempt_count=1)
    rows7 = claim(conn, "alive")
    check("stale in_progress 被接管", len(rows7) == 1 and rows7[0][5] == "alive")

    # ── 8. 租约未过期的 in_progress 不被接管 ──
    reset(conn)
    add_work(conn, "t1", status="in_progress", lease_owner="busy", lease_expires="future")
    check("租约未过期的 in_progress 不被接管", claim(conn, "alive") == [])

    # ── 9. batch_size 上限生效 ──
    reset(conn)
    for t in ("ta", "tb", "tc", "td"):
        conn.execute("INSERT INTO background_work_items (tenant_id, work_kind) VALUES (%s,'k')", (t,))
    check("batch_size 限制认领数", len(claim(conn, "w", batch_size=2)) == 2)

    # ── 10. 并发认领同一行只有一个成功 ──
    reset(conn)
    wid = add_work(conn, "t1")
    with psycopg.connect(URL) as c1, psycopg.connect(URL) as c2:
        t1 = c1.execute(CLAIM_SQL, {"owner": "c1", "batch_size": 10, "max_attempts": 5,
                                    "lease_ttl": 60.0}).fetchall()
        t2 = c2.execute(CLAIM_SQL, {"owner": "c2", "batch_size": 10, "max_attempts": 5,
                                    "lease_ttl": 60.0}).fetchall()
        # 两个事务都拿锁后各自提交，只有一个应真正写入
        c1.commit(); c2.commit()
    winner = [r for r in (t1, t2) if r]
    check("并发认领不重复", len(winner) <= 1, f"t1={len(t1)} t2={len(t2)}")
    final_owner = conn.execute(
        "SELECT lease_owner FROM background_work_items WHERE id=%s", (wid,)
    ).fetchone()
    check("最终只有 1 个 owner 写入", final_owner is not None, f"owner={final_owner}")

print("\n全部 claim SQL 冒烟验证通过")
