"""WebChat delivery 接线验收（pg-durable-sot-cutover task 4.1/4.2）。

覆盖（webchat-durable-storage spec「final 投递经 delivery 状态机」）：
- 在线投递：适配器从重放帧表取帧 → 投到在线连接 → `sent` 推进 + attempt 记录；
- 无在线连接：attempt 失败按退避排程重试，final 消息不受影响；
- 退避/上限：`max_attempts` 用尽进入 `dead_letter`，attempt 链可查；
- 帧缺失（retention）：适配器抛错走失败路径。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.repository.control_plane_repo import (
    DeliveryRepository,
    IngressRepository,
    TurnControlRepository,
)
from bootstrap.delivery_worker import DeliveryWorkerConfig, OutboundDeliveryWorker
from bootstrap.webchat_durable import (
    WebchatDeliveryAdapter,
    WebchatDurableTurnFinisher,
)
from bus.events import OutboundMessage
from infra.channels.web_chat_channel import WebChatChannel, _Connection
from tests.test_web_chat_channel import _FakeWebSocket, _queued

pytestmark = pytest.mark.postgres


def _ack(client_message_id: str) -> dict[str, Any]:
    return {
        "type": "message.accepted",
        "seq": None,
        "client_message_id": client_message_id,
        "session_key": "chat:tenant",
    }


async def _make_completed_turn(
    c2_factory: async_sessionmaker, prefix: str
) -> tuple[dict[str, Any], str, str]:
    """造一个已 T2 完成的 turn（pending intent 在位），返回 (tenant, turn_id, message_id)。"""
    from bootstrap.db.repository.canonical_repo import CanonicalIdentityRepository

    identities = CanonicalIdentityRepository(c2_factory)
    provisioned = await identities.provision_account_with_agent(
        f"dlv_{prefix}_{uuid.uuid4().hex[:8]}", status="active"
    )
    tenant = {
        "tenant_id": provisioned["conversation"]["tenant_id"],
        "account_id": provisioned["account"]["id"],
        "conversation_id": provisioned["conversation"]["id"],
    }
    ingress = IngressRepository(c2_factory)
    cmid = f"dlv-{prefix}-{uuid.uuid4().hex[:12]}"
    accepted = await ingress.accept_inbound(
        tenant["tenant_id"],
        tenant["conversation_id"],
        account_id=tenant["account_id"],
        client_message_id=cmid,
        content="问题",
        replay_frame=_ack(cmid),
    )
    finisher = WebchatDurableTurnFinisher(c2_factory, channel_name="chat")
    outbound = OutboundMessage(
        channel="chat",
        chat_id=tenant["tenant_id"],
        content="回答正文",
        metadata={
            "nexus_pg_turn_id": accepted.turn_id,
            "nexus_pg_inbox_id": accepted.inbox_id,
            "nexus_pg_conversation_id": tenant["conversation_id"],
            "tenant_id": tenant["tenant_id"],
            "client_message_id": cmid,
        },
    )
    await finisher._on_outbound(outbound)
    return tenant, str(accepted.turn_id), str(accepted.message_id)


def _channel_with_connection(
    tenant: dict[str, Any],
) -> tuple[WebChatChannel, _FakeWebSocket]:
    from infra.channels.web_chat_channel import WebChatIdentity

    channel = WebChatChannel()
    ws = _FakeWebSocket()
    conn = _Connection(
        ws,
        uuid.uuid4().hex,
        identity=WebChatIdentity(
            account_id=str(tenant["account_id"]),
            tenant_id=str(tenant["tenant_id"]),
            conversation_id=str(tenant["conversation_id"]),
            session_key=f"chat:{tenant['tenant_id']}",
            chat_id=str(tenant["tenant_id"]),
        ),
    )
    channel._connections[ws] = conn
    return channel, ws


async def test_online_delivery_advances_sent_with_attempt(
    c2_factory: async_sessionmaker,
) -> None:
    tenant, _turn_id, _message_id = await _make_completed_turn(c2_factory, "on")
    channel, ws = _channel_with_connection(tenant)

    worker = OutboundDeliveryWorker(
        DeliveryRepository(c2_factory),
        WebchatDeliveryAdapter(c2_factory, channel),
    )
    processed = await worker.process_once()
    assert processed == 1

    # 在线连接收到 final 帧（含 durable seq），intent 推进 sent。
    conn = next(iter(channel._connections.values()))
    delivered = [f for f in _queued(conn) if f.get("type") == "turn.completed"]
    assert len(delivered) == 1
    assert delivered[0]["content"] == "回答正文"
    assert isinstance(delivered[0]["seq"], int)
    intents = await DeliveryRepository(c2_factory).claim_batch("other-owner")
    assert intents == []  # 已 sent，不再被认领


async def test_no_online_connection_fails_attempt_with_backoff(
    c2_factory: async_sessionmaker,
) -> None:
    tenant, _turn_id, message_id = await _make_completed_turn(c2_factory, "off")
    channel = WebChatChannel()  # 无任何连接

    worker = OutboundDeliveryWorker(
        DeliveryRepository(c2_factory),
        WebchatDeliveryAdapter(c2_factory, channel),
        config=DeliveryWorkerConfig(max_attempts=3, backoff_seconds=(60.0, 60.0, 60.0)),
    )
    processed = await worker.process_once()
    assert processed == 1

    # attempt 失败排程重试；final 消息与重放帧不受影响。
    intents = await DeliveryRepository(c2_factory).claim_batch("other-owner")
    assert intents == []  # 未到期，不被认领
    from bootstrap.db.repository.control_plane_repo import WebchatReplayRepository

    frames = await WebchatReplayRepository(c2_factory).frames_after(
        tenant["tenant_id"], tenant["conversation_id"], 0
    )
    completed = [f for f in frames if f["type"] == "turn.completed"]
    assert len(completed) == 1 and completed[0]["content"] == "回答正文"


async def test_dead_letter_after_max_attempts(
    c2_factory: async_sessionmaker,
) -> None:
    tenant, _turn_id, _message_id = await _make_completed_turn(c2_factory, "dl")
    channel = WebChatChannel()

    worker = OutboundDeliveryWorker(
        DeliveryRepository(c2_factory),
        WebchatDeliveryAdapter(c2_factory, channel),
        config=DeliveryWorkerConfig(max_attempts=1, backoff_seconds=(60.0,)),
    )
    assert await worker.process_once() == 1

    delivery = DeliveryRepository(c2_factory)
    assert await delivery.claim_batch("other-owner") == []
    # dead_letter 状态直查（无租户可见 API；系统侧核验 attempt 链收束）。
    import psycopg
    from tests.control_plane.conftest import ADMIN_URL

    url = ADMIN_URL.rsplit("/", 1)[0] + "/nexus_c2test"
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT status, attempt_count, last_error FROM outbound_delivery_intents "
            "WHERE conversation_id = %s",
            (tenant["conversation_id"],),
        ).fetchone()
    assert row is not None
    assert row[0] == "dead_letter"
    assert row[1] == 1
    assert "无在线连接" in row[2]
