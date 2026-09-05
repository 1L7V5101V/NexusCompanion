"""重启重放验收：只补投未确认 intent、不重新生成 final；worker 集成（含接管/退避路径）。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.delivery_worker import DeliveryWorkerConfig, OutboundDeliveryWorker
from bootstrap.db.repository.control_plane_repo import (
    DeliveryRepository,
    IngressRepository,
    TurnControlRepository,
)

pytestmark = pytest.mark.postgres


class _FakeChannel:
    """可编程发送回调：成功返回 receipt，可切换为失败/抛错。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    async def __call__(self, envelope: Any) -> str | None:
        self.calls.append(envelope.intent_id)
        if self.fail:
            raise RuntimeError("channel down")
        return f"receipt-{envelope.intent_id[:8]}"


@pytest.fixture
def repos(c2_factory: async_sessionmaker) -> dict[str, Any]:
    return {
        "ingress": IngressRepository(c2_factory),
        "control": TurnControlRepository(c2_factory),
        "delivery": DeliveryRepository(c2_factory),
    }


async def _make_pending_intent(
    repos: dict[str, Any], tenant: dict[str, Any], *, tag: str
) -> dict[str, Any]:
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
        delivery_channel="telegram",
        delivery_target=f"chat-{tag}",
    )
    return result.intent


async def _assistant_message_count(repos: dict[str, Any], tenant: dict[str, Any]) -> int:
    from bootstrap.db.repository.canonical_repo import CanonicalMessageRepository

    messages = CanonicalMessageRepository(repos["control"]._sf)  # type: ignore[arg-type]
    stream = await messages.fetch_messages(tenant["tenant_id"], tenant["conversation_id"])
    return sum(1 for m in stream if m["role"] == "assistant")


def _expire_lease(exec_sql: Callable[..., Any], intent_id: str) -> None:
    exec_sql(
        "UPDATE outbound_delivery_intents SET lease_expires_at = now() - interval '1 second'"
        " WHERE id = %s",
        (intent_id,),
    )


async def test_restart_replay_no_duplicate_final(
    make_tenant, repos: dict[str, Any], exec_sql: Callable[..., Any]
) -> None:
    """T2 已提交（final + pending intent）→ 模拟重启（新 worker 实例）→ 只补投、final 不变。"""
    tenant = await make_tenant()
    intent = await _make_pending_intent(repos, tenant, tag="replay")
    channel = _FakeChannel()

    # 「重启前」进程：已 claim 但未投递即崩溃（无任何状态写入）
    crashed = OutboundDeliveryWorker(repos["delivery"], channel, worker_id="worker-crashed")
    claimed = await repos["delivery"].claim_batch(crashed.owner)
    assert [r["id"] for r in claimed] == [intent["id"]]
    del crashed  # 模拟进程退出：租约残留在 DB

    # 「重启后」新进程：新 worker 实例接管并完成投递
    channel2 = _FakeChannel()
    restarted = OutboundDeliveryWorker(repos["delivery"], channel2, worker_id="worker-new")
    assert await restarted.process_once() == 0  # 租约未过期，不可立即接管
    delivery: DeliveryRepository = repos["delivery"]

    _expire_lease(exec_sql, intent["id"])
    assert await restarted.process_once() == 1
    assert channel2.calls == [intent["id"]]

    row = await delivery.get_intent(tenant["tenant_id"], uuid.UUID(intent["id"]))
    assert row is not None and row["status"] == "sent"
    # 不重新生成 final：assistant final 恰一条
    assert await _assistant_message_count(repos, tenant) == 1
    # 原 worker 未发送过：只有接管者的一次投递调用
    assert channel.calls == []


async def test_sent_intent_not_redispatched_after_restart(
    make_tenant, repos: dict[str, Any]
) -> None:
    """已 sent 的意图重启后不再投递（无新 attempt、无新调用）。"""
    tenant = await make_tenant()
    intent = await _make_pending_intent(repos, tenant, tag="sent-once")
    channel = _FakeChannel()
    worker = OutboundDeliveryWorker(repos["delivery"], channel, worker_id="worker-a")
    assert await worker.process_once() == 1
    delivery: DeliveryRepository = repos["delivery"]
    attempts_before = await delivery.list_delivery_attempts(
        tenant["tenant_id"], uuid.UUID(intent["id"])
    )
    assert await worker.process_once() == 0
    attempts_after = await delivery.list_delivery_attempts(
        tenant["tenant_id"], uuid.UUID(intent["id"])
    )
    assert attempts_before == attempts_after
    assert channel.calls == [intent["id"]]


async def test_worker_retries_failed_delivery_without_new_final(
    make_tenant, repos: dict[str, Any], exec_sql: Callable[..., Any]
) -> None:
    """通道失败：worker 记 failed + 退避；重试成功；全程 final 消息只此一份。"""
    tenant = await make_tenant()
    intent = await _make_pending_intent(repos, tenant, tag="retry")
    channel = _FakeChannel()
    channel.fail = True
    worker = OutboundDeliveryWorker(
        repos["delivery"],
        channel,
        worker_id="worker-a",
        config=DeliveryWorkerConfig(
            poll_interval_seconds=0.01,
            max_attempts=3,
            backoff_seconds=(60.0, 300.0, 1800.0, 7200.0, 21600.0),
        ),
    )
    assert await worker.process_once() == 1
    delivery: DeliveryRepository = repos["delivery"]
    row = await delivery.get_intent(tenant["tenant_id"], uuid.UUID(intent["id"]))
    assert row is not None and row["status"] == "failed"
    assert await _assistant_message_count(repos, tenant) == 1

    # 时间流逝后重试成功
    channel.fail = False
    exec_sql(
        "UPDATE outbound_delivery_intents SET next_attempt_at = now() - interval '1 second'"
        " WHERE id = %s",
        (intent["id"],),
    )
    assert await worker.process_once() == 1
    row = await delivery.get_intent(tenant["tenant_id"], uuid.UUID(intent["id"]))
    assert row is not None and row["status"] == "sent"
    assert await _assistant_message_count(repos, tenant) == 1
    attempts = await delivery.list_delivery_attempts(
        tenant["tenant_id"], uuid.UUID(intent["id"])
    )
    assert [a["outcome"] for a in attempts] == ["failed", "sent"]


async def test_worker_dead_letter_then_admin_redrive(
    make_tenant, repos: dict[str, Any], exec_sql: Callable[..., Any]
) -> None:
    """连续失败达上限 → dead_letter；管理员 redrive 后 worker 可重投成功。"""
    tenant = await make_tenant()
    intent = await _make_pending_intent(repos, tenant, tag="dl")
    channel = _FakeChannel()
    channel.fail = True
    worker = OutboundDeliveryWorker(
        repos["delivery"],
        channel,
        worker_id="worker-a",
        config=DeliveryWorkerConfig(
            poll_interval_seconds=0.01,
            max_attempts=2,
            backoff_seconds=(60.0, 300.0, 1800.0, 7200.0, 21600.0),
        ),
    )
    delivery: DeliveryRepository = repos["delivery"]
    assert await worker.process_once() == 1
    exec_sql(
        "UPDATE outbound_delivery_intents SET next_attempt_at = now() - interval '1 second'"
        " WHERE id = %s",
        (intent["id"],),
    )
    assert await worker.process_once() == 1
    row = await delivery.get_intent(tenant["tenant_id"], uuid.UUID(intent["id"]))
    assert row is not None and row["status"] == "dead_letter"

    channel.fail = False
    await delivery.redrive_dead_letter(
        tenant["tenant_id"], uuid.UUID(intent["id"]), "运维确认通道恢复"
    )
    assert await worker.process_once() == 1
    row = await delivery.get_intent(tenant["tenant_id"], uuid.UUID(intent["id"]))
    assert row is not None and row["status"] == "sent"
    assert await _assistant_message_count(repos, tenant) == 1
    attempts = await delivery.list_delivery_attempts(
        tenant["tenant_id"], uuid.UUID(intent["id"])
    )
    assert [a["outcome"] for a in attempts] == ["failed", "failed", "redrive", "sent"]


async def test_run_loop_delivers_until_stop(
    make_tenant, repos: dict[str, Any], c2_pg_url
) -> None:
    """run() 循环：后台投递直到 stop；轮询间隔用最小配置。"""
    tenant = await make_tenant()
    intent = await _make_pending_intent(repos, tenant, tag="loop")
    channel = _FakeChannel()
    worker = OutboundDeliveryWorker(
        repos["delivery"],
        channel,
        worker_id="worker-loop",
        config=DeliveryWorkerConfig(poll_interval_seconds=0.01),
    )
    task = asyncio.create_task(worker.run())
    delivery: DeliveryRepository = repos["delivery"]
    for _ in range(200):
        await asyncio.sleep(0.01)
        row = await delivery.get_intent(tenant["tenant_id"], uuid.UUID(intent["id"]))
        if row is not None and row["status"] == "sent":
            break
    worker.stop()
    await asyncio.wait_for(task, timeout=5)
    row = await delivery.get_intent(tenant["tenant_id"], uuid.UUID(intent["id"]))
    assert row is not None and row["status"] == "sent"
    assert channel.calls == [intent["id"]]
