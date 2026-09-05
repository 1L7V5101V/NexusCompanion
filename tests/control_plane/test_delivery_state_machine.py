"""delivery 状态机验收：claim/lease/heartbeat、sent 仅由 ack 推进、退避、dead_letter、redrive。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.repository.control_plane_repo import (
    DeliveryIntentNotFoundError,
    DeliveryRepository,
    IngressRepository,
    LeaseLostError,
    RedriveNotAllowedError,
    TurnControlRepository,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def repos(c2_factory: async_sessionmaker) -> dict[str, Any]:
    return {
        "ingress": IngressRepository(c2_factory),
        "control": TurnControlRepository(c2_factory),
        "delivery": DeliveryRepository(c2_factory),
    }


async def _make_pending_intent(
    repos: dict[str, Any],
    tenant: dict[str, Any],
    *,
    tag: str,
    channel: str = "telegram",
) -> dict[str, Any]:
    """走生产路径（T1 + T2）创建一条 pending 投递意图。"""
    accepted = await repos["ingress"].accept_inbound(
        tenant["tenant_id"],
        tenant["conversation_id"],
        account_id=tenant["account_id"],
        client_message_id=f"cm-{tag}",
        content="hi",
    )
    assert accepted.turn_id is not None
    result = await repos["control"].complete_turn_with_delivery(
        tenant["tenant_id"],
        tenant["conversation_id"],
        accepted.turn_id,
        expected_status="queued",
        response_content=f"reply-{tag}",
        delivery_channel=channel,
        delivery_target=f"chat-{tag}",
    )
    return result.intent


def _force_due(exec_sql: Callable[..., Any], intent_id: str) -> None:
    exec_sql(
        "UPDATE outbound_delivery_intents SET next_attempt_at = now() - interval '1 second'"
        " WHERE id = %s",
        (intent_id,),
    )


def _expire_lease(exec_sql: Callable[..., Any], intent_id: str) -> None:
    exec_sql(
        "UPDATE outbound_delivery_intents SET lease_expires_at = now() - interval '1 second'"
        " WHERE id = %s",
        (intent_id,),
    )


async def test_claim_pending_to_attempting(
    make_tenant, repos: dict[str, Any]
) -> None:
    """认领：pending → attempting，租约属主/到期时间写入，attempt_count +1。"""
    tenant = await make_tenant()
    intent = await _make_pending_intent(repos, tenant, tag="claim")
    delivery: DeliveryRepository = repos["delivery"]
    claimed = await delivery.claim_batch("owner-a")
    assert len(claimed) == 1
    row = claimed[0]
    assert row["id"] == intent["id"]
    assert row["status"] == "attempting"
    assert row["lease_owner"] == "owner-a"
    assert row["attempt_count"] == 1
    assert row["lease_expires_at"]


async def test_sent_only_via_ack_full_path(
    make_tenant, repos: dict[str, Any]
) -> None:
    """全路径：认领 → 发送成功（ack）→ sent + attempt(receipt)。"""
    tenant = await make_tenant()
    intent = await _make_pending_intent(repos, tenant, tag="sent")
    delivery: DeliveryRepository = repos["delivery"]
    claimed = (await delivery.claim_batch("owner-a"))[0]
    recorded = await delivery.record_attempt_sent(
        tenant["tenant_id"],
        uuid.UUID(claimed["id"]),
        "owner-a",
        provider_receipt="tg-ack-123",
    )
    assert recorded["status"] == "sent"
    assert recorded["sent_at"]
    attempts = await delivery.list_delivery_attempts(tenant["tenant_id"], uuid.UUID(intent["id"]))
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "sent"
    assert attempts[0]["provider_receipt"] == "tg-ack-123"
    assert attempts[0]["finished_at"]


async def test_no_ack_never_sent(
    make_tenant, repos: dict[str, Any], exec_sql: Callable[..., Any]
) -> None:
    """负向：发送无 ack → 永不 sent；failed + 退避排程 + attempt 记录错误。"""
    tenant = await make_tenant()
    intent = await _make_pending_intent(repos, tenant, tag="noack")
    delivery: DeliveryRepository = repos["delivery"]
    claimed = (await delivery.claim_batch("owner-a"))[0]
    recorded = await delivery.record_attempt_failed(
        tenant["tenant_id"],
        uuid.UUID(claimed["id"]),
        "owner-a",
        "provider timeout",
        max_attempts=5,
        backoff_seconds=(60.0, 300.0, 1800.0, 7200.0, 21600.0),
    )
    assert recorded["status"] == "failed"
    assert recorded["sent_at"] is None
    assert recorded["last_error"] == "provider timeout"
    assert recorded["lease_owner"] is None
    delay = (
        datetime.fromisoformat(recorded["next_attempt_at"]) - datetime.now(UTC)
    ).total_seconds()
    assert 50 <= delay <= 70  # 首次退避 ≈ 1m（§5.9.11）
    attempts = await delivery.list_delivery_attempts(tenant["tenant_id"], uuid.UUID(intent["id"]))
    assert [a["outcome"] for a in attempts] == ["failed"]
    # 未到期不被认领；模拟时间流逝后才可重试
    assert await delivery.claim_batch("owner-a") == []
    _force_due(exec_sql, intent["id"])
    reclaimed = await delivery.claim_batch("owner-a")
    assert [r["id"] for r in reclaimed] == [intent["id"]]
    assert reclaimed[0]["attempt_count"] == 2


async def test_backoff_schedule_progression(
    make_tenant, repos: dict[str, Any], exec_sql: Callable[..., Any]
) -> None:
    """退避序列 1m/5m/30m/2h 逐次推进，第 5 次失败进入 dead_letter。"""
    tenant = await make_tenant()
    intent = await _make_pending_intent(repos, tenant, tag="backoff")
    delivery: DeliveryRepository = repos["delivery"]
    expected_backoff = (60.0, 300.0, 1800.0, 7200.0)
    for cycle in range(4):
        claimed = (await delivery.claim_batch("owner-a"))[0]
        assert claimed["attempt_count"] == cycle + 1
        recorded = await delivery.record_attempt_failed(
            tenant["tenant_id"],
            uuid.UUID(claimed["id"]),
            "owner-a",
            f"boom-{cycle}",
            max_attempts=5,
            backoff_seconds=(60.0, 300.0, 1800.0, 7200.0, 21600.0),
        )
        assert recorded["status"] == "failed"
        delay = (
            datetime.fromisoformat(recorded["next_attempt_at"]) - datetime.now(UTC)
        ).total_seconds()
        assert expected_backoff[cycle] - 10 <= delay <= expected_backoff[cycle] + 10
        _force_due(exec_sql, intent["id"])
    # 第 5 次尝试失败 → dead_letter
    claimed = (await delivery.claim_batch("owner-a"))[0]
    assert claimed["attempt_count"] == 5
    recorded = await delivery.record_attempt_failed(
        tenant["tenant_id"],
        uuid.UUID(claimed["id"]),
        "owner-a",
        "boom-final",
        max_attempts=5,
        backoff_seconds=(60.0, 300.0, 1800.0, 7200.0, 21600.0),
    )
    assert recorded["status"] == "dead_letter"
    assert await delivery.claim_batch("owner-a") == []


async def test_stale_lease_takeover(
    make_tenant, repos: dict[str, Any], exec_sql: Callable[..., Any]
) -> None:
    """stale lease：attempting 租约过期后可被其他扫描器接管（attempt_count 继续 +1）。"""
    tenant = await make_tenant()
    intent = await _make_pending_intent(repos, tenant, tag="stale")
    delivery: DeliveryRepository = repos["delivery"]
    first = (await delivery.claim_batch("owner-a"))[0]
    assert first["lease_owner"] == "owner-a"
    assert await delivery.claim_batch("owner-b") == []  # 租约未过期，他人不可认领
    _expire_lease(exec_sql, intent["id"])
    takeover = await delivery.claim_batch("owner-b")
    assert [r["id"] for r in takeover] == [intent["id"]]
    assert takeover[0]["lease_owner"] == "owner-b"
    assert takeover[0]["attempt_count"] == 2


async def test_heartbeat_renews_and_detects_loss(
    make_tenant, repos: dict[str, Any]
) -> None:
    """heartbeat：属主续租成功；非属主/非 attempting → False（租约已失信号）。"""
    tenant = await make_tenant()
    await _make_pending_intent(repos, tenant, tag="hb")
    delivery: DeliveryRepository = repos["delivery"]
    claimed = (await delivery.claim_batch("owner-a"))[0]
    assert await delivery.heartbeat(
        tenant["tenant_id"], uuid.UUID(claimed["id"]), "owner-a", lease_ttl_seconds=60.0
    )
    assert not await delivery.heartbeat(
        tenant["tenant_id"], uuid.UUID(claimed["id"]), "owner-b", lease_ttl_seconds=60.0
    )
    # 推进终态后 heartbeat 失效
    await delivery.record_attempt_sent(tenant["tenant_id"], uuid.UUID(claimed["id"]), "owner-a")
    assert not await delivery.heartbeat(
        tenant["tenant_id"], uuid.UUID(claimed["id"]), "owner-a", lease_ttl_seconds=60.0
    )


async def test_lease_lost_cannot_record_sent(
    make_tenant, repos: dict[str, Any], exec_sql: Callable[..., Any]
) -> None:
    """失租不得写 sent：原属主的成功结果被拒绝，接管者收束（ADR-5）。"""
    tenant = await make_tenant()
    intent = await _make_pending_intent(repos, tenant, tag="lost")
    delivery: DeliveryRepository = repos["delivery"]
    await delivery.claim_batch("owner-a")
    _expire_lease(exec_sql, intent["id"])
    await delivery.claim_batch("owner-b")  # 接管
    with pytest.raises(LeaseLostError):
        await delivery.record_attempt_sent(
            tenant["tenant_id"], uuid.UUID(intent["id"]), "owner-a", provider_receipt="late-ack"
        )
    row = await delivery.get_intent(tenant["tenant_id"], uuid.UUID(intent["id"]))
    assert row is not None and row["status"] == "attempting"
    assert row["lease_owner"] == "owner-b"
    await delivery.record_attempt_sent(tenant["tenant_id"], uuid.UUID(intent["id"]), "owner-b")
    row = await delivery.get_intent(tenant["tenant_id"], uuid.UUID(intent["id"]))
    assert row is not None and row["status"] == "sent"


async def test_redrive_dead_letter_keeps_history(
    make_tenant, repos: dict[str, Any], exec_sql: Callable[..., Any]
) -> None:
    """redrive：追加处置记录保留全部历史；复位 pending 重投；原 attempt 不删不改（ADR-6）。"""
    tenant = await make_tenant()
    intent = await _make_pending_intent(repos, tenant, tag="redrive")
    delivery: DeliveryRepository = repos["delivery"]
    # 两次失败（每次强制到期重试）
    for cycle in range(2):
        claimed = (await delivery.claim_batch("owner-a"))[0]
        await delivery.record_attempt_failed(
            tenant["tenant_id"],
            uuid.UUID(claimed["id"]),
            "owner-a",
            f"boom-{cycle}",
            max_attempts=2,
            backoff_seconds=(60.0, 300.0, 1800.0, 7200.0, 21600.0),
        )
        if cycle == 0:
            _force_due(exec_sql, intent["id"])
    row = await delivery.get_intent(tenant["tenant_id"], uuid.UUID(intent["id"]))
    assert row is not None and row["status"] == "dead_letter"
    attempts_before = await delivery.list_delivery_attempts(
        tenant["tenant_id"], uuid.UUID(intent["id"])
    )
    assert [a["outcome"] for a in attempts_before] == ["failed", "failed"]

    redriven = await delivery.redrive_dead_letter(
        tenant["tenant_id"], uuid.UUID(intent["id"]), "管理员核实后重投", operator="admin-1"
    )
    assert redriven["status"] == "pending"
    assert redriven["attempt_count"] == 0
    attempts_after = await delivery.list_delivery_attempts(
        tenant["tenant_id"], uuid.UUID(intent["id"])
    )
    assert [a["outcome"] for a in attempts_after] == ["failed", "failed", "redrive"]
    assert attempts_after[2]["error"] == "[admin-1] 管理员核实后重投"
    assert attempts_after[0] == attempts_before[0]  # 原 attempt 行未被改写
    # 复位后可再次认领（本周期计数从 0 重新开始）
    claimed = (await delivery.claim_batch("owner-a"))[0]
    assert claimed["attempt_count"] == 1


async def test_redrive_rejected_for_non_dead_letter(
    make_tenant, repos: dict[str, Any]
) -> None:
    """非 dead_letter 状态的 redrive 请求被拒绝且零副作用。"""
    tenant = await make_tenant()
    intent = await _make_pending_intent(repos, tenant, tag="no-redrive")
    delivery: DeliveryRepository = repos["delivery"]
    with pytest.raises(RedriveNotAllowedError):
        await delivery.redrive_dead_letter(
            tenant["tenant_id"], uuid.UUID(intent["id"]), "误操作"
        )
    claimed = (await delivery.claim_batch("owner-a"))[0]
    await delivery.record_attempt_sent(tenant["tenant_id"], uuid.UUID(claimed["id"]), "owner-a")
    with pytest.raises(RedriveNotAllowedError):
        await delivery.redrive_dead_letter(
            tenant["tenant_id"], uuid.UUID(intent["id"]), "误操作"
        )
    attempts = await delivery.list_delivery_attempts(
        tenant["tenant_id"], uuid.UUID(intent["id"])
    )
    assert [a["outcome"] for a in attempts] == ["sent"]


async def test_tenant_isolation_on_delivery_queries(
    make_tenant, repos: dict[str, Any]
) -> None:
    """跨租户查询投递意图/attempt → 不可见；未知意图 fail-closed。"""
    tenant_a = await make_tenant(prefix="c2da")
    tenant_b = await make_tenant(prefix="c2db")
    intent = await _make_pending_intent(repos, tenant_a, tag="iso")
    delivery: DeliveryRepository = repos["delivery"]
    assert await delivery.get_intent(tenant_b["tenant_id"], uuid.UUID(intent["id"])) is None
    assert await delivery.list_intents_by_status(tenant_b["tenant_id"], "pending") == []
    with pytest.raises(DeliveryIntentNotFoundError):
        await delivery.record_attempt_failed(
            tenant_b["tenant_id"],
            uuid.UUID(intent["id"]),
            "owner-x",
            "boom",
            max_attempts=5,
            backoff_seconds=(60.0,),
        )
    with pytest.raises(DeliveryIntentNotFoundError):
        await delivery.redrive_dead_letter(
            tenant_b["tenant_id"], uuid.UUID(intent["id"]), "跨租户"
        )


async def test_claim_batch_skips_when_over_max_attempts(
    make_tenant, repos: dict[str, Any]
) -> None:
    """pending/failed 分支受本周期 attempt_count < max_attempts 把门（ADR-5）。"""
    tenant = await make_tenant()
    intent = await _make_pending_intent(repos, tenant, tag="cap")
    delivery: DeliveryRepository = repos["delivery"]
    claimed = (await delivery.claim_batch("owner-a", max_attempts=1))[0]
    await delivery.record_attempt_failed(
        tenant["tenant_id"],
        uuid.UUID(claimed["id"]),
        "owner-a",
        "boom",
        max_attempts=1,
        backoff_seconds=(60.0, 300.0, 1800.0, 7200.0, 21600.0),
    )
    row = await delivery.get_intent(tenant["tenant_id"], uuid.UUID(intent["id"]))
    assert row is not None and row["status"] == "dead_letter"
    assert await delivery.claim_batch("owner-a", max_attempts=1) == []
