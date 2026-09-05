"""C3 overload 测试矩阵：global interactive 128、per-tenant 16、BoundedAdmissionQueue 语义。"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from agent.admission.queues import (
    AdmissionLimits,
    AdmissionOverloadError,
    BoundedAdmissionQueue,
)
from bootstrap.passive_worker import PassiveMessageWorker
from bus.events import InboundMessage, OutboundMessage
from bus.queue import MessageBus


def _msg(chat_id: str = "c1", tenant_id: str = "t1") -> InboundMessage:
    return InboundMessage(
        channel="chat",
        sender="u",
        chat_id=chat_id,
        content="hi",
        tenant_id=tenant_id,
    )


def test_admission_limits_frozen_defaults() -> None:
    limits = AdmissionLimits()
    assert limits.global_interactive_queue == 128
    assert limits.per_tenant_pending_interactive == 16
    assert limits.global_maintenance_queue == 64
    with pytest.raises(ValueError):
        AdmissionLimits(global_interactive_queue=0)


def test_bounded_queue_overload_semantics() -> None:
    queue = BoundedAdmissionQueue(2, limit_kind="global_interactive", retry_after=3.0)
    queue.try_put_nowait("a")
    queue.try_put_nowait("b")
    with pytest.raises(AdmissionOverloadError) as exc_info:
        queue.try_put_nowait("c")
    err = exc_info.value
    assert err.limit_kind == "global_interactive"
    assert err.retry_after == 3.0
    # 已接受项不受影响
    assert queue.get_nowait() == "a"
    with pytest.raises(ValueError):
        BoundedAdmissionQueue(0, limit_kind="global_interactive")


async def test_message_bus_global_interactive_overload() -> None:
    bus = MessageBus(inbound_limit=2)
    await bus.publish_inbound(_msg("c1"))
    await bus.publish_inbound(_msg("c2"))
    # 第 3 条：durable acceptance 前 overload，明确拒绝
    with pytest.raises(AdmissionOverloadError) as exc_info:
        await bus.publish_inbound(_msg("c3"))
    assert exc_info.value.limit_kind == "global_interactive"
    # 已接受项不丢失
    assert bus.inbound_size == 2
    first = await bus.consume_inbound()
    assert first.chat_id == "c1"


class _FakeRuntime:
    def wait_thread_available(self, thread_id: str) -> Any:
        return _noop()


class _FakeLoop:
    async def _run_inbound_turn(self, item: Any) -> None:  # pragma: no cover
        return None


async def _noop() -> None:
    return None


def _make_worker(inbound_limit: int, per_tenant: int) -> PassiveMessageWorker:
    bus = MessageBus(inbound_limit=inbound_limit)
    return PassiveMessageWorker(
        cast(Any, bus),
        cast(Any, _FakeRuntime()),
        cast(Any, _FakeLoop()),
        per_tenant_pending=per_tenant,
    )


async def test_passive_worker_per_tenant_pending_rejects_17th(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _make_worker(inbound_limit=64, per_tenant=2)
    published: list[OutboundMessage] = []

    async def fake_publish_outbound(msg: OutboundMessage) -> None:
        published.append(msg)

    monkeypatch.setattr(
        cast(Any, worker._bus), "publish_outbound", fake_publish_outbound
    )
    blocked = asyncio.Event()

    async def wait_blocked(thread_id: str) -> None:
        await blocked.wait()

    monkeypatch.setattr(
        cast(Any, worker._runtime), "wait_thread_available", wait_blocked
    )

    # tenant t1：1 条进 lane 被消费（阻塞在 wait_thread_available），2 条 pending
    for i in range(3):
        worker._enqueue(_msg(f"c{i}", tenant_id="t1"))
        await asyncio.sleep(0)
    assert worker._lane_queues["t1"].qsize() == 2

    # 第 4 条（pending 上限 2 已满）：明确拒绝，不进入 lane
    worker._enqueue(_msg("c3", tenant_id="t1"))
    assert worker._lane_queues["t1"].qsize() == 2
    assert worker.rejected_inbound == 1
    # 其他 tenant 不受影响
    worker._enqueue(_msg("d0", tenant_id="t2"))
    assert worker._lane_queues["t2"].qsize() == 1
    assert worker.rejected_inbound == 1

    blocked.set()
    worker.stop()
    await asyncio.sleep(0.05)
    # 拒绝回执进入 outbound（明确拒绝反馈，非静默丢弃）
    assert any("排队已满" in msg.content for msg in published)
