"""WebChatChannel durable 接受分支测试（pg-durable-sot-cutover task 2.1/2.2）。

用假 gateway 锁定通道侧行为：durable seq 不被进程内 buffer 重盖、duplicate
逐字重放、overload/error 不缓存幂等（可原样重发）、dev 回退身份走 legacy。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

import pytest

from bus.event_bus import EventBus
from bus.events import InboundMessage
from bus.queue import MessageBus
from infra.channels.contract import ChannelContext
from infra.channels.web_chat_channel import (
    DurableSendOutcome,
    WebChatChannel,
    _Connection,
)
from tests.test_web_chat_channel import (
    _FakeWebSocket,
    _queued,
    _register,
    _start_channel,
)


def _register_authed(channel: WebChatChannel, ws: _FakeWebSocket) -> _Connection:
    conn = _Connection(ws, uuid4().hex, identity=_authed_identity())
    channel._connections[ws] = conn
    return conn


class _FakeGateway:
    """脚本化 gateway：按预设脚本逐次返回 outcome，记录调用。"""

    def __init__(self, outcomes: list[DurableSendOutcome]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def eligible(self, identity: Any) -> bool:
        return bool(getattr(identity, "account_id", "").count("-") == 4)

    async def accept_send(
        self,
        *,
        identity: Any,
        client_message_id: str,
        content: str,
        media: list[str],
        sender: str,
    ) -> DurableSendOutcome:
        self.calls.append(
            {
                "identity": identity,
                "client_message_id": client_message_id,
                "content": content,
                "media": media,
                "sender": sender,
            }
        )
        return self.outcomes.pop(0)


_AUTHED_IDENTITY_CALL = object()


def _make_channel(outcomes: list[DurableSendOutcome]) -> tuple[WebChatChannel, _FakeGateway]:
    gateway = _FakeGateway(outcomes)
    channel = WebChatChannel(durable_gateway=gateway)  # type: ignore[arg-type]
    return channel, gateway


def _authed_identity() -> Any:
    from infra.channels.web_chat_channel import WebChatIdentity

    return WebChatIdentity(
        account_id="00000000-0000-0000-0000-000000000001",
        tenant_id="dev",
        conversation_id="00000000-0000-0000-0000-000000000002",
        session_key="chat:dev",
        chat_id="dev",
    )


def _accepted_frame(seq: int, client_message_id: str) -> dict[str, Any]:
    return {
        "type": "message.accepted",
        "seq": seq,
        "client_message_id": client_message_id,
        "session_key": "chat:dev",
    }


@pytest.mark.asyncio
async def test_durable_accept_acks_with_gateway_seq_and_publishes_via_gateway():
    """accepted：以 gateway 给的 durable seq ack（进程内 buffer 不再盖 seq）。"""
    cmid = str(uuid4())
    channel, gateway = _make_channel(
        [DurableSendOutcome(kind="accepted", frame=_accepted_frame(7, cmid))]
    )
    bus, _ = _start_channel(channel)
    ws = _FakeWebSocket()
    conn = _register_authed(channel, ws)

    await channel._handle_send(
        conn, {"type": "send", "client_message_id": cmid, "content": "你好"}
    )

    assert len(gateway.calls) == 1
    assert gateway.calls[0]["content"] == "你好"
    # legacy 路径的 publish_inbound 不发生（入队是 gateway 的职责）。
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.consume_inbound(), timeout=0.05)
    acked = [f for f in _queued(conn) if f["type"] == "message.accepted"]
    assert len(acked) == 1
    assert acked[0]["seq"] == 7  # durable seq 原样下发，未被 _replay 重盖


@pytest.mark.asyncio
async def test_duplicate_replays_exact_frame_and_cache_hit_skips_gateway():
    """duplicate：逐字重放原帧；同 id 第三次发送走进程内缓存，不再调 gateway。"""
    cmid = str(uuid4())
    original = _accepted_frame(3, cmid)
    channel, gateway = _make_channel(
        [
            DurableSendOutcome(kind="accepted", frame=dict(original)),
            DurableSendOutcome(kind="duplicate", frame=dict(original)),
        ]
    )
    _start_channel(channel)
    ws = _FakeWebSocket()
    conn = _register_authed(channel, ws)

    for _ in range(1):
        await channel._handle_send(
            conn, {"type": "send", "client_message_id": cmid, "content": "hi"}
        )
    # 清掉 L1 缓存模拟进程重启/缓存逐出：durable duplicate 由网关回查原帧。
    channel._accepted_frames.clear()
    await channel._handle_send(
        conn, {"type": "send", "client_message_id": cmid, "content": "hi"}
    )
    await channel._handle_send(
        conn, {"type": "send", "client_message_id": cmid, "content": "hi"}
    )

    assert len(gateway.calls) == 2  # 第三次命中缓存
    accepted = [f for f in _queued(conn) if f["type"] == "message.accepted"]
    assert len(accepted) == 3
    assert all(f["seq"] == 3 for f in accepted)  # 同 seq 重放（含重启存续语义）


@pytest.mark.asyncio
async def test_overload_and_error_do_not_consume_idempotency():
    """overload/error：结构化错误帧（复用 overload 码）、gateway 再次被调用。"""
    cmid = str(uuid4())
    channel, gateway = _make_channel(
        [
            DurableSendOutcome(kind="overload", error_message="服务繁忙。"),
            DurableSendOutcome(kind="error", error_message="服务暂时不可用。"),
            DurableSendOutcome(kind="accepted", frame=_accepted_frame(1, cmid)),
        ]
    )
    _start_channel(channel)
    ws = _FakeWebSocket()
    conn = _register_authed(channel, ws)

    for _ in range(2):
        await channel._handle_send(
            conn, {"type": "send", "client_message_id": cmid, "content": "hi"}
        )
    errors = [f for f in _queued(conn) if f["type"] == "error"]
    assert len(errors) == 2
    assert {f["code"] for f in errors} == {"overload"}

    # 第三次（accepted）：同 id 可原样重发并成功接受。
    await channel._handle_send(
        conn, {"type": "send", "client_message_id": cmid, "content": "hi"}
    )
    assert len(gateway.calls) == 3
    accepted = [f for f in _queued(conn) if f["type"] == "message.accepted"]
    assert [f["seq"] for f in accepted] == [1]


@pytest.mark.asyncio
async def test_ineligible_identity_falls_back_to_legacy_path():
    """dev 回退身份（非 UUID）：gateway.eligible=False → legacy 路径照旧。"""
    channel, gateway = _make_channel(
        [DurableSendOutcome(kind="accepted", frame=_accepted_frame(1, "x"))]
    )
    bus, _ = _start_channel(channel)
    ws = _FakeWebSocket()
    conn = _register(channel, ws)  # 默认 dev 身份：conn.identity=None 回退
    cmid = str(uuid4())

    await channel._handle_send(
        conn, {"type": "send", "client_message_id": cmid, "content": "你好"}
    )

    assert gateway.calls == []  # 未走 gateway
    inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)
    assert isinstance(inbound, InboundMessage)
    assert inbound.session_key == "chat:local"  # legacy dev 路由键不变


@pytest.mark.asyncio
async def test_no_gateway_keeps_legacy_behaviour():
    """未注入 gateway（PG 不可用/dev）：行为与 C4 完全一致。"""
    channel = WebChatChannel()
    bus, _ = _start_channel(channel)
    ws = _FakeWebSocket()
    conn = _register(channel, ws)
    cmid = str(uuid4())

    await channel._handle_send(
        conn, {"type": "send", "client_message_id": cmid, "content": "你好"}
    )

    inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)
    assert inbound.metadata["client_message_id"] == cmid
    accepted = [f for f in _queued(conn) if f["type"] == "message.accepted"]
    assert len(accepted) == 1  # legacy seq 由进程内 buffer 分配
