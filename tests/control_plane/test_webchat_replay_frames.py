"""durable 重放帧与 WebChat durable 接受网关验收（pg-durable-sot-cutover task 2.1/2.2）。

覆盖（webchat-durable-storage spec）：
- T1 与 accepted 重放帧同事务（replay_seq 从 1 起单调、帧 JSON 逐字含 seq、
  message_id/turn_id 落列）；
- 重复注入逐字重放**原帧**（同 wire seq，重启存续语义）；
- T2 / 失败终态收束的 turn.completed / turn.failed 帧同事务落表；
- 补拉读取面（frames_after 升序、租户隔离、current_seq 水位、retention 删除）；
- gateway：overload 预检不消耗幂等键、accepted 附 durable 元数据、duplicate 重放。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.repository.control_plane_repo import (
    DeliveryRepository,
    IngressRepository,
    TurnControlRepository,
    WebchatReplayRepository,
)
from bootstrap.webchat_durable import WebchatDurableGateway
from bus.queue import MessageBus
from tests.control_plane.conftest import DEV_ACCOUNT_ID, DEV_TENANT_ID

pytestmark = pytest.mark.postgres

DEV_CONVERSATION_ID = "00000000-0000-0000-0000-000000000002"


@pytest.fixture
def ingress(c2_factory: async_sessionmaker) -> IngressRepository:
    return IngressRepository(c2_factory)


@pytest.fixture
def turns(c2_factory: async_sessionmaker) -> TurnControlRepository:
    return TurnControlRepository(c2_factory)


@pytest.fixture
def replay(c2_factory: async_sessionmaker) -> WebchatReplayRepository:
    return WebchatReplayRepository(c2_factory)


def _ack(client_message_id: str) -> dict[str, Any]:
    return {
        "type": "message.accepted",
        "seq": None,
        "client_message_id": client_message_id,
        "session_key": f"chat:{DEV_TENANT_ID}",
    }


async def test_accept_writes_replay_frame_in_same_transaction(
    make_tenant, ingress: IngressRepository, replay: WebchatReplayRepository
) -> None:
    tenant = await make_tenant()
    cmid = "c1f0a2b3-0000-4000-8000-0000000000a1"

    result = await ingress.accept_inbound(
        tenant["tenant_id"],
        tenant["conversation_id"],
        account_id=tenant["account_id"],
        client_message_id=cmid,
        content="你好",
        replay_frame=_ack(cmid),
    )

    assert result.duplicate is False
    assert result.replay_seq == 1  # 计数器从 1 起（与 legacy 进程内 buffer 对齐）
    assert result.replay_frame is not None
    assert result.replay_frame["seq"] == 1
    assert result.replay_frame["client_message_id"] == cmid
    # 帧行落列：message_id/turn_id 可回查（重复路径依据）。
    rows = await replay.frames_after(
        tenant["tenant_id"], tenant["conversation_id"], 0
    )
    assert len(rows) == 1
    assert rows[0]["seq"] == 1
    assert rows[0]["type"] == "message.accepted"
    assert await replay.current_seq(tenant["tenant_id"], tenant["conversation_id"]) == 1


async def test_duplicate_returns_original_frame_with_same_seq(
    make_tenant, ingress: IngressRepository
) -> None:
    tenant = await make_tenant()
    cmid = "c1f0a2b3-0000-4000-8000-0000000000a2"
    first = await ingress.accept_inbound(
        tenant["tenant_id"],
        tenant["conversation_id"],
        account_id=tenant["account_id"],
        client_message_id=cmid,
        content="第一条",
        replay_frame=_ack(cmid),
    )
    # 模拟「重放窗口内的后续帧」：同会话另一条消息先拿走 seq=2。
    await ingress.accept_inbound(
        tenant["tenant_id"],
        tenant["conversation_id"],
        account_id=tenant["account_id"],
        client_message_id="c1f0a2b3-0000-4000-8000-0000000000a3",
        content="第二条",
        replay_frame=_ack("c1f0a2b3-0000-4000-8000-0000000000a3"),
    )

    dup = await ingress.accept_inbound(
        tenant["tenant_id"],
        tenant["conversation_id"],
        account_id=tenant["account_id"],
        client_message_id=cmid,
        content="重发（内容可不同）",
    )

    assert dup.duplicate is True
    assert dup.replay_seq == first.replay_seq == 1  # 原 seq，不随新帧漂移
    assert dup.replay_frame == first.replay_frame  # 逐字一致
    assert dup.turn_id == first.turn_id


async def test_completion_and_failure_frames_in_same_transaction(
    make_tenant, ingress: IngressRepository, turns: TurnControlRepository,
    replay: WebchatReplayRepository, c2_factory: async_sessionmaker,
) -> None:
    tenant = await make_tenant()
    cmid = "c1f0a2b3-0000-4000-8000-0000000000b1"
    accepted = await ingress.accept_inbound(
        tenant["tenant_id"],
        tenant["conversation_id"],
        account_id=tenant["account_id"],
        client_message_id=cmid,
        content="问题",
        replay_frame=_ack(cmid),
    )
    assert accepted.turn_id is not None
    _ = await turns.transition_turn(
        tenant["tenant_id"], accepted.turn_id,
        expected_status="queued", new_status="in_progress",
    )

    completed = await turns.complete_turn_with_delivery(
        tenant["tenant_id"],
        tenant["conversation_id"],
        accepted.turn_id,
        expected_status="in_progress",
        response_content="回答",
        delivery_channel="chat",
        delivery_target=tenant["tenant_id"],
        replay_frame={
            "type": "turn.completed", "seq": None,
            "turn_id": "wire-turn", "content": "回答", "thinking": None, "media": [],
        },
    )

    assert completed.replay_seq == 2  # accepted=1 之后单调
    assert completed.replay_frame is not None
    assert completed.replay_frame["type"] == "turn.completed"
    frames = await replay.frames_after(tenant["tenant_id"], tenant["conversation_id"], 0)
    assert [f["type"] for f in frames] == ["message.accepted", "turn.completed"]

    # 失败终态：另一条消息入队后直接收束 failed，turn.failed 帧同事务。
    accepted2 = await ingress.accept_inbound(
        tenant["tenant_id"],
        tenant["conversation_id"],
        account_id=tenant["account_id"],
        client_message_id="c1f0a2b3-0000-4000-8000-0000000000b2",
        content="会失败的",
        replay_frame=_ack("c1f0a2b3-0000-4000-8000-0000000000b2"),
    )
    failed = await turns.transition_turn(
        tenant["tenant_id"],
        accepted2.turn_id,
        expected_status="queued",
        new_status="failed",
        error={"code": "boom"},
        replay_frame={"type": "turn.failed", "seq": None, "turn_id": "wire-turn-2", "error": "boom"},
    )
    assert failed["replay_seq"] == 4  # seq 按事务提交顺序分配：accepted1=1, completed=2, accepted2=3
    frames = await replay.frames_after(tenant["tenant_id"], tenant["conversation_id"], 0)
    assert [f["type"] for f in frames] == [
        "message.accepted", "turn.completed", "message.accepted", "turn.failed",
    ]
    _ = DeliveryRepository, c2_factory


async def test_replay_frames_tenant_isolation_and_retention(
    make_tenant, ingress: IngressRepository, replay: WebchatReplayRepository
) -> None:
    tenant_a = await make_tenant(prefix="sota")
    tenant_b = await make_tenant(prefix="sotb")
    for tenant, cmid_suffix in ((tenant_a, "c1"), (tenant_b, "c2")):
        await ingress.accept_inbound(
            tenant["tenant_id"],
            tenant["conversation_id"],
            account_id=tenant["account_id"],
            client_message_id=f"c1f0a2b3-0000-4000-8000-00000000{cmid_suffix}",
            content="hi",
            replay_frame=_ack(
                f"c1f0a2b3-0000-4000-8000-00000000{cmid_suffix}"
            ),
        )

    # 跨 tenant 补拉为空（租户隔离，语义同 canonical fetch_messages）。
    assert await replay.frames_after(tenant_a["tenant_id"], tenant_b["conversation_id"], 0) == []
    # 未知会话 current_seq=0（不泄露存在性）。
    assert await replay.current_seq(tenant_a["tenant_id"], "00000000-0000-4000-8000-999999999999") == 0

    # retention：删 seq<2 不动计数器（水位只增不减）。
    removed = await replay.delete_frames_before(
        tenant_a["tenant_id"], tenant_a["conversation_id"], 2
    )
    assert removed == 1
    assert await replay.frames_after(tenant_a["tenant_id"], tenant_a["conversation_id"], 0) == []
    assert await replay.current_seq(tenant_a["tenant_id"], tenant_a["conversation_id"]) == 1
    assert await replay.frames_after(tenant_a["tenant_id"], tenant_a["conversation_id"], 1) == []


class _Identity:
    """gateway 用最小身份（C1 seed 的 dev 三元组）。"""

    account_id = DEV_ACCOUNT_ID
    tenant_id = DEV_TENANT_ID
    conversation_id = DEV_CONVERSATION_ID
    session_key = f"chat:{DEV_TENANT_ID}"
    chat_id = DEV_TENANT_ID


async def test_gateway_accept_duplicate_and_overload_precheck(
    c2_factory: async_sessionmaker,
) -> None:
    bus = MessageBus(inbound_limit=1)
    gateway = WebchatDurableGateway(c2_factory, bus, channel_name="chat")
    identity = _Identity()
    cmid = "c1f0a2b3-0000-4000-8000-0000000000d1"

    # 队列满 → overload 预检：不发生 T1（重发同 id 仍走全新接受）。
    full = MessageBus(inbound_limit=0)
    full_gateway = WebchatDurableGateway(c2_factory, full, channel_name="chat")
    outcome = await full_gateway.accept_send(
        identity=identity, client_message_id=cmid, content="hi", media=[], sender="webchat"
    )
    assert outcome.kind == "overload"

    outcome = await gateway.accept_send(
        identity=identity, client_message_id=cmid, content="你好", media=[], sender="webchat"
    )
    assert outcome.kind == "accepted"
    assert outcome.frame is not None and outcome.frame["seq"] == 1
    # inbound 携带 pg durable 元数据（task 3.x 完成事务贯穿键）。
    inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)
    assert inbound.metadata["nexus_pg_turn_id"]  # queued turn 已在 T1 落行
    assert inbound.metadata["nexus_pg_conversation_id"] == DEV_CONVERSATION_ID
    assert inbound.tenant_id == DEV_TENANT_ID

    # duplicate：重放原帧（同 seq），不产生第二条 inbound。
    outcome = await gateway.accept_send(
        identity=identity, client_message_id=cmid, content="重发", media=[], sender="webchat"
    )
    assert outcome.kind == "duplicate"
    assert outcome.frame is not None and outcome.frame["seq"] == 1
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.consume_inbound(), timeout=0.05)


async def test_finisher_completes_turn_with_delivery_and_frames(
    c2_factory: async_sessionmaker, make_tenant, ingress: IngressRepository
) -> None:
    """正常回复 → T2（final+completed+intent 同事务）+ inbox 收束 + seq 盖回。"""
    from bootstrap.webchat_durable import WebchatDurableTurnFinisher
    from bus.events import OutboundMessage

    tenant = await make_tenant()
    ingress_repo = IngressRepository(c2_factory)
    finisher = WebchatDurableTurnFinisher(c2_factory, channel_name="chat")
    cmid = "c1f0a2b3-0000-4000-8000-0000000000e1"
    accepted = await ingress_repo.accept_inbound(
        tenant["tenant_id"],
        tenant["conversation_id"],
        account_id=tenant["account_id"],
        client_message_id=cmid,
        content="问题",
        replay_frame=_ack(cmid),
    )
    assert accepted.turn_id is not None

    outbound = OutboundMessage(
        channel="chat",
        chat_id=tenant["tenant_id"],
        content="回答正文",
        thinking="想了一下",
        metadata={
            "nexus_pg_turn_id": accepted.turn_id,
            "nexus_pg_inbox_id": accepted.inbox_id,
            "nexus_pg_conversation_id": tenant["conversation_id"],
            "tenant_id": tenant["tenant_id"],
            "client_message_id": cmid,
        },
    )
    await finisher._on_outbound(outbound)

    assert outbound.metadata["nexus_replay_seq"] == 2  # accepted=1, completed=2
    turns_repo = TurnControlRepository(c2_factory)
    turn = await turns_repo.get_turn(tenant["tenant_id"], accepted.turn_id)
    assert turn is not None and turn["status"] == "completed"
    inbox = await ingress_repo.get_inbox(tenant["tenant_id"], accepted.inbox_id)
    assert inbox is not None and inbox["status"] == "processed"
    rows = await WebchatReplayRepository(c2_factory).frames_after(
        tenant["tenant_id"], tenant["conversation_id"], 0
    )
    assert [f["type"] for f in rows] == ["message.accepted", "turn.completed"]
    assert rows[1]["content"] == "回答正文"
    # T2 产出的 pending intent 可被 delivery worker 认领（4.x 前置）。
    delivery = DeliveryRepository(c2_factory)
    claimed = await delivery.claim_batch("test-owner", batch_size=5)
    assert len(claimed) == 1
    assert claimed[0]["status"] == "attempting"


async def test_finisher_marks_failed_without_intent_on_error_outbound(
    c2_factory: async_sessionmaker, make_tenant, ingress: IngressRepository
) -> None:
    """nexus_error 出站 → 失败终态 + turn.failed 帧，不产投递意图。"""
    from bootstrap.webchat_durable import WebchatDurableTurnFinisher
    from bus.events import OutboundMessage

    tenant = await make_tenant()
    ingress_repo = IngressRepository(c2_factory)
    finisher = WebchatDurableTurnFinisher(c2_factory, channel_name="chat")
    cmid = "c1f0a2b3-0000-4000-8000-0000000000f1"
    accepted = await ingress_repo.accept_inbound(
        tenant["tenant_id"],
        tenant["conversation_id"],
        account_id=tenant["account_id"],
        client_message_id=cmid,
        content="会失败的问题",
        replay_frame=_ack(cmid),
    )

    outbound = OutboundMessage(
        channel="chat",
        chat_id=tenant["tenant_id"],
        content="处理消息时出错，请稍后再试。",
        metadata={
            "nexus_pg_turn_id": accepted.turn_id,
            "nexus_pg_inbox_id": accepted.inbox_id,
            "nexus_pg_conversation_id": tenant["conversation_id"],
            "tenant_id": tenant["tenant_id"],
            "nexus_error": True,
            "nexus_fail_reason": "provider_error",
        },
    )
    await finisher._on_outbound(outbound)

    assert outbound.metadata["nexus_replay_seq"] == 2
    turns_repo = TurnControlRepository(c2_factory)
    turn = await turns_repo.get_turn(tenant["tenant_id"], accepted.turn_id)
    assert turn is not None and turn["status"] == "failed"
    inbox = await ingress_repo.get_inbox(tenant["tenant_id"], accepted.inbox_id)
    assert inbox is not None and inbox["status"] == "processed"
    delivery = DeliveryRepository(c2_factory)
    claimed = await delivery.claim_batch("test-owner", batch_size=5)
    assert claimed == []  # 失败终态不创建投递意图
    frames = await WebchatReplayRepository(c2_factory).frames_after(
        tenant["tenant_id"], tenant["conversation_id"], 0
    )
    assert [f["type"] for f in frames] == ["message.accepted", "turn.failed"]
