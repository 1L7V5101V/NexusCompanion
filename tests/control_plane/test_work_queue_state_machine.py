"""C15 work item 消费层验收：认领/租约/终态/崩溃清扫/死信/审计流（真 PG）。

design ADR-1..ADR-6；用例与 spec `durable-work-queue` 的 requirement/scenario 对应。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bootstrap.db.repository.control_plane_repo import (
    LeaseLostError,
    RedriveNotAllowedError,
    TurnControlRepository,
    WorkItemNotFoundError,
    WorkItemRepository,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def work_repo(c2_factory: async_sessionmaker) -> WorkItemRepository:
    return WorkItemRepository(c2_factory)


@pytest.fixture
def control(c2_factory: async_sessionmaker) -> TurnControlRepository:
    return TurnControlRepository(c2_factory)


async def _new_work(
    control: TurnControlRepository, tenant: dict[str, Any], **kw: Any
) -> dict[str, Any]:
    """建一条维护类 work item：`work_kind` 定 lane、`flow` 定 handler（ADR-5）。"""
    return await control.create_work_item(
        tenant["tenant_id"],
        "maintenance",
        flow="consolidation",
        conversation_id=tenant["conversation_id"],
        **kw,
    )


def _side_effects(exec_sql: Callable[..., Any]) -> int:
    return int(
        exec_sql(
            "SELECT count(*) FROM test_accounts WHERE display_name = 'side-effect'"
        )[0][0]
    )


async def _insert_side_effect(sess: AsyncSession, tenant_id: str) -> None:
    """handler 的业务副作用（ADR-6：必须与终态同事务）。"""
    await sess.execute(
        text(
            "INSERT INTO test_accounts (id, tenant_id, status, display_name) "
            "VALUES (gen_random_uuid(), :t, 'active', 'side-effect')"
        ),
        {"t": tenant_id},
    )


async def _expire_lease(exec_sql: Callable[..., Any], item_id: str) -> None:
    """模拟崩溃：把租约改成已过期（不可经仓储表达）。"""
    exec_sql(
        "UPDATE background_work_items SET lease_expires_at = now() - interval '1 second' "
        "WHERE id = %s",
        (item_id,),
    )


async def _make_due(exec_sql: Callable[..., Any], item_id: str) -> None:
    """把退避排程提前到已到期，便于连续触发重试。"""
    exec_sql(
        "UPDATE background_work_items SET next_attempt_at = now() - interval '1 second' "
        "WHERE id = %s",
        (item_id,),
    )


def _outcomes(exec_sql: Callable[..., Any], item_id: str) -> list[str]:
    rows = exec_sql(
        "SELECT outcome FROM work_attempts WHERE work_item_id = %s ORDER BY started_at, id",
        (item_id,),
    )
    return [r[0] for r in rows]


# ── 认领与租约（ADR-4） ──


async def test_claim_one_per_tenant_per_round(
    make_tenant, control, work_repo: WorkItemRepository
) -> None:
    """同一租户多条就绪 → 本轮至多 1 条；跨租户各自可认领。"""
    a = await make_tenant(prefix="c15a")
    b = await make_tenant(prefix="c15b")
    for i in range(3):
        await _new_work(control, a, idempotency_key=f"a-{i}")
    await _new_work(control, b, idempotency_key="b-0")

    claimed = await work_repo.claim_batch("w1")
    tenants = sorted(c["tenant_id"] for c in claimed)
    assert len(claimed) == 2
    assert tenants == sorted([a["tenant_id"], b["tenant_id"]])


async def test_claim_skips_tenant_with_active_lease(
    make_tenant, control, work_repo: WorkItemRepository
) -> None:
    """条件①：该租户已有有效租约在途 → 本轮不认领它的任何工作项（ADR-4 反例回归）。"""
    tenant = await make_tenant(prefix="c15c")
    for i in range(3):
        await _new_work(control, tenant, idempotency_key=f"c-{i}")

    first = await work_repo.claim_batch("w1")
    assert len(first) == 1
    # 若只做「轮内去重」而无「在途互斥」，这里会取走该租户的下一条 queued。
    assert await work_repo.claim_batch("w2") == []


async def test_claim_does_not_touch_attempt_count(
    make_tenant, control, work_repo: WorkItemRepository
) -> None:
    """认领不修改 `attempt_count`（ADR-3 实现期细化：唯一递增点在 record_work_failed）。"""
    tenant = await make_tenant(prefix="c15d")
    await _new_work(control, tenant, idempotency_key="d-0")
    claimed = await work_repo.claim_batch("w1")
    assert claimed[0]["attempt_count"] == 0


async def test_heartbeat_and_lease_lost(
    make_tenant, control, work_repo: WorkItemRepository
) -> None:
    tenant = await make_tenant(prefix="c15e")
    item = await _new_work(control, tenant, idempotency_key="e-0")
    await work_repo.claim_batch("owner-1")

    assert await work_repo.heartbeat(tenant["tenant_id"], item["id"], "owner-1") is True
    assert await work_repo.heartbeat(tenant["tenant_id"], item["id"], "owner-2") is False


# ── 终态与同事务副作用（ADR-6） ──


async def test_succeeded_commits_side_effect_atomically(
    make_tenant, control, work_repo: WorkItemRepository, exec_sql
) -> None:
    tenant = await make_tenant(prefix="c15f")
    item = await _new_work(control, tenant, idempotency_key="f-0")
    await work_repo.claim_batch("w1")

    async def mutate(sess: AsyncSession) -> None:
        await _insert_side_effect(sess, tenant["tenant_id"])

    result = await work_repo.record_work_succeeded(
        tenant["tenant_id"], item["id"], "w1", mutate=mutate
    )
    assert result["status"] == "succeeded"
    assert result["finished_at"] is not None
    assert _side_effects(exec_sql) == 1
    assert _outcomes(exec_sql, item["id"]) == ["succeeded"]


async def test_mutate_failure_rolls_back_terminal_state(
    make_tenant, control, work_repo: WorkItemRepository, exec_sql
) -> None:
    """副作用抛错 → 终态与副作用都不落（同事务，ADR-6）。"""
    tenant = await make_tenant(prefix="c15g")
    item = await _new_work(control, tenant, idempotency_key="g-0")
    await work_repo.claim_batch("w1")

    async def bad_mutate(sess: AsyncSession) -> None:
        await _insert_side_effect(sess, tenant["tenant_id"])
        raise RuntimeError("handler 失败")

    with pytest.raises(RuntimeError):
        await work_repo.record_work_succeeded(
            tenant["tenant_id"], item["id"], "w1", mutate=bad_mutate
        )

    row = await work_repo.get_work_item(tenant["tenant_id"], item["id"])
    assert row is not None
    assert row["status"] == "in_progress" and row["lease_owner"] == "w1"
    assert _side_effects(exec_sql) == 0


# ── 失败、退避与死信（ADR-3 定案 (B)） ──


async def test_failures_increment_budget_and_dead_letter_at_max(
    make_tenant, control, work_repo: WorkItemRepository, exec_sql
) -> None:
    """恰好 max_attempts 次业务失败后进入 `failed` 死信终态，且不再被认领。"""
    tenant = await make_tenant(prefix="c15h")
    item = await _new_work(control, tenant, idempotency_key="h-0")

    for i in range(1, 5):
        claimed = await work_repo.claim_batch(f"w{i}")
        assert [c["id"] for c in claimed] == [item["id"]]
        res = await work_repo.record_work_failed(
            tenant["tenant_id"], item["id"], f"w{i}", f"err{i}", max_attempts=5
        )
        assert res["attempt_count"] == i
        assert res["status"] == "queued"
        await _make_due(exec_sql, item["id"])

    claimed = await work_repo.claim_batch("w5")
    assert [c["id"] for c in claimed] == [item["id"]]
    final = await work_repo.record_work_failed(
        tenant["tenant_id"], item["id"], "w5", "err5", max_attempts=5
    )
    assert final["status"] == "failed"
    assert final["finished_at"] is not None
    assert await work_repo.claim_batch("w6") == []
    assert _outcomes(exec_sql, item["id"]) == ["failed"] * 5


async def test_redrive_resets_and_preserves_history(
    make_tenant, control, work_repo: WorkItemRepository, exec_sql
) -> None:
    """仅死信可 redrive；复位尝试计数、保留全部历史、重新可认领。"""
    tenant = await make_tenant(prefix="c15i")
    item = await _new_work(control, tenant, idempotency_key="i-0")
    await work_repo.claim_batch("w1")
    await work_repo.record_work_failed(
        tenant["tenant_id"], item["id"], "w1", "boom", max_attempts=1
    )
    assert (await work_repo.get_work_item(tenant["tenant_id"], item["id"]))["status"] == "failed"

    redriven = await work_repo.redrive_work_item(
        tenant["tenant_id"], item["id"], "已修好", operator="admin"
    )
    assert redriven["status"] == "queued"
    assert redriven["attempt_count"] == 0
    assert _outcomes(exec_sql, item["id"]) == ["failed", "redrive"]
    assert len(await work_repo.claim_batch("w2")) == 1


async def test_redrive_rejected_for_non_dead_letter(
    make_tenant, control, work_repo: WorkItemRepository
) -> None:
    tenant = await make_tenant(prefix="c15j")
    item = await _new_work(control, tenant, idempotency_key="j-0")
    with pytest.raises(RedriveNotAllowedError):
        await work_repo.redrive_work_item(tenant["tenant_id"], item["id"], "不该允许")


async def test_release_for_retry_does_not_consume_budget(
    make_tenant, control, work_repo: WorkItemRepository, exec_sql
) -> None:
    """维护类延后不计失败、不碰尝试计数，并留下 `released` 审计行（ADR-5）。"""
    tenant = await make_tenant(prefix="c15k")
    item = await _new_work(control, tenant, idempotency_key="k-0")
    await work_repo.claim_batch("w1")

    released = await work_repo.release_for_retry(
        tenant["tenant_id"], item["id"], "w1", delay_seconds=30, note="interactive 忙"
    )
    assert released["status"] == "queued"
    assert released["attempt_count"] == 0
    assert _outcomes(exec_sql, item["id"]) == ["released"]


# ── 崩溃恢复（ADR-3） ──


async def test_sweep_resets_stale_without_consuming_budget(
    make_tenant, control, work_repo: WorkItemRepository, exec_sql
) -> None:
    tenant = await make_tenant(prefix="c15l")
    item = await _new_work(control, tenant, idempotency_key="l-0")
    await work_repo.claim_batch("dead")
    await _expire_lease(exec_sql, item["id"])

    swept = await work_repo.sweep_stale_leases()
    assert [s["id"] for s in swept] == [item["id"]]
    assert swept[0]["prev_lease_owner"] == "dead"

    row = await work_repo.get_work_item(tenant["tenant_id"], item["id"])
    assert row is not None
    assert row["status"] == "queued"
    assert row["attempt_count"] == 0
    assert row["lease_owner"] is None
    assert _outcomes(exec_sql, item["id"]) == ["recovered"]
    assert len(await work_repo.claim_batch("alive")) == 1

    # 租约未过期不受影响
    assert await work_repo.sweep_stale_leases() == []


async def test_repeated_crashes_never_dead_letter(
    make_tenant, control, work_repo: WorkItemRepository, exec_sql
) -> None:
    """反复崩溃不消耗预算、不判死、始终可恢复（(B) 定案的核心不变量）。"""
    tenant = await make_tenant(prefix="c15m")
    item = await _new_work(control, tenant, idempotency_key="m-0")

    for i in range(7):
        assert len(await work_repo.claim_batch(f"w{i}")) == 1
        await _expire_lease(exec_sql, item["id"])
        assert len(await work_repo.sweep_stale_leases()) == 1

    row = await work_repo.get_work_item(tenant["tenant_id"], item["id"])
    assert row is not None
    assert row["status"] == "queued"
    assert row["attempt_count"] == 0
    assert len(await work_repo.claim_batch("final")) == 1


# ── 失租禁写 / 租户隔离 / 审计流 ──


async def test_lease_lost_cannot_write_state(
    make_tenant, control, work_repo: WorkItemRepository
) -> None:
    tenant = await make_tenant(prefix="c15n")
    item = await _new_work(control, tenant, idempotency_key="n-0")
    await work_repo.claim_batch("owner-a")

    with pytest.raises(LeaseLostError):
        await work_repo.record_work_failed(tenant["tenant_id"], item["id"], "owner-b", "x")
    with pytest.raises(LeaseLostError):
        await work_repo.record_work_succeeded(tenant["tenant_id"], item["id"], "owner-b")
    with pytest.raises(LeaseLostError):
        await work_repo.release_for_retry(tenant["tenant_id"], item["id"], "owner-b")


async def test_tenant_isolation(
    make_tenant, control, work_repo: WorkItemRepository
) -> None:
    a = await make_tenant(prefix="c15o")
    b = await make_tenant(prefix="c15p")
    item = await _new_work(control, a, idempotency_key="o-0")
    await work_repo.claim_batch("w1")

    assert await work_repo.get_work_item(b["tenant_id"], item["id"]) is None
    assert await work_repo.get_work_item(a["tenant_id"], item["id"]) is not None
    assert await work_repo.list_work_attempts(b["tenant_id"], item["id"]) == []
    with pytest.raises(WorkItemNotFoundError):
        await work_repo.record_work_succeeded(b["tenant_id"], item["id"], "w1")


async def test_audit_stream_is_append_only_in_order(
    make_tenant, control, work_repo: WorkItemRepository, exec_sql
) -> None:
    """`released → failed` 顺序稳定；审计流只追加、不被改写（ADR-2）。"""
    tenant = await make_tenant(prefix="c15q")
    item = await _new_work(control, tenant, idempotency_key="q-0")
    await work_repo.claim_batch("w1")
    await work_repo.release_for_retry(tenant["tenant_id"], item["id"], "w1", delay_seconds=1)
    await _make_due(exec_sql, item["id"])
    await work_repo.claim_batch("w2")
    await work_repo.record_work_failed(
        tenant["tenant_id"], item["id"], "w2", "boom", max_attempts=5
    )
    assert _outcomes(exec_sql, item["id"]) == ["released", "failed"]

    listed = await work_repo.list_work_attempts(tenant["tenant_id"], item["id"])
    assert [r["outcome"] for r in listed] == ["released", "failed"]
    assert listed[1]["error"] == "boom"


# ── 词汇校验（spec「工作类型词汇与分派」） ──


async def test_work_kind_vocabulary_rejects_flow_value(
    make_tenant, control: TurnControlRepository
) -> None:
    """`consolidation` 一类 flow 值不得当作 work_kind（纠正 C2 时期的错误词汇）。"""
    tenant = await make_tenant(prefix="c15r")
    with pytest.raises(ValueError):
        await control.create_work_item(
            tenant["tenant_id"], "consolidation", conversation_id=tenant["conversation_id"]
        )
    with pytest.raises(ValueError):
        await control.create_work_item(
            tenant["tenant_id"],
            "maintenance",
            flow="not-a-flow",
            conversation_id=tenant["conversation_id"],
        )
