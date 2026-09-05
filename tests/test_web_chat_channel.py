"""WebChatChannel 与 dev v0 帧协议测试（shared-contract fixture 参数化）。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from bus.event_bus import EventBus
from bus.events import InboundMessage
from bus.events_lifecycle import (
    StreamDeltaReady,
    ToolCallCompleted,
    ToolCallStarted,
)
from bus.queue import MessageBus
from infra.channels.contract import ChannelContext
from infra.channels.web_chat_channel import _Connection, _ReplayBuffer, WebChatChannel
from infra.channels.web_chat_protocol import (
    DEV_SESSION_KEY,
    hello,
    message_accepted,
    turn_completed,
    turn_failed,
)
from infra.storage.tenancy import DEFAULT_TENANT
from session.manager import SessionManager

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "chat_protocol_frames.json").read_text(
        encoding="utf-8"
    )
)


class _SessionManagerStub:
    workspace: str | None = None


def _start_channel(
    channel: WebChatChannel, session_manager: Any = None
) -> tuple[MessageBus, EventBus]:
    bus = MessageBus()
    event_bus = EventBus()
    channel._bind(
        ChannelContext(
            bus=bus,
            session_manager=session_manager or _SessionManagerStub(),
            event_bus=event_bus,
            push_tool=None,  # type: ignore[arg-type]
            attachment_store=None,  # type: ignore[arg-type]
            http_resources=None,  # type: ignore[arg-type]
            interrupt_controller=None,
            bot_commands=[],
            log=logging.getLogger("test"),
        )
    )
    return bus, event_bus


class _FakeWebSocket:
    """最小 WebSocket 假件：记录 send_json；receive 由测试控制。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.incoming: list[str] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        if not self.incoming:
            await asyncio.sleep(3600)
        return self.incoming.pop(0)

    def push_client_frame(self, frame: dict[str, Any]) -> None:
        self.incoming.append(json.dumps(frame))


class _DyingWebSocket(_FakeWebSocket):
    async def receive_text(self) -> str:
        raise RuntimeError("connection closed")


def _register(channel: WebChatChannel, ws: _FakeWebSocket) -> _Connection:
    conn = _Connection(ws, uuid4().hex)
    channel._connections[ws] = conn
    return conn


def _queued(conn: _Connection) -> list[dict[str, Any]]:
    """取出 outbound 队列中的帧（直连 handler 的测试没有 sender task）。"""
    items: list[dict[str, Any]] = []
    while not conn.outbound.empty():
        entry = conn.outbound.get_nowait()
        if entry is not None:
            # C3：队列存 (frame, payload_bytes) 便于 1 MiB payload 会计。
            frame, _size = entry
            items.append(frame)
    return items


# ── shared contract：协议工厂与 fixture 形状一致 ──────────────────


def test_factory_shapes_match_fixture():
    cases = FIXTURE["server_to_client"]
    assert hello(
        connection_id="0f8b7a1c2d3e4f5a6b7c8d9e0f1a2b3c",
        session_key="chat:local",
        latest_seq=42,
    ) == cases["hello"]
    assert message_accepted(
        client_message_id="3f2a9c1e-8b4d-4c3a-9e2f-1a2b3c4d5e6f",
        session_key="chat:local",
    ) == cases["message.accepted"]
    assert turn_completed(
        turn_id="turn-0f8b", content="完整回复正文", thinking=None, media=[]
    ) == cases["turn.completed"]
    assert turn_failed(
        turn_id="turn-0f8b", error="处理消息时出错，请稍后再试。"
    ) == cases["turn.failed"]


# ── replay buffer ────────────────────────────────────────────────


def test_replay_buffer_stamps_monotonic_seq():
    buffer = _ReplayBuffer(max_size=3)
    f1 = buffer.stamp(
        message_accepted(client_message_id="a", session_key=DEV_SESSION_KEY)
    )
    f2 = buffer.stamp(turn_completed(turn_id="t", content="hi"))
    f3 = buffer.stamp(turn_completed(turn_id="t", content="hi2"))
    f4 = buffer.stamp(turn_completed(turn_id="t", content="hi3"))

    assert [f["seq"] for f in (f1, f2, f3, f4)] == [1, 2, 3, 4]
    # max_size=3：seq=1 已被挤出，after_seq=0 无法覆盖。
    assert buffer.frames_after(0) is None
    assert [f["seq"] for f in buffer.frames_after(1)] == [2, 3, 4]
    assert buffer.frames_after(4) == []


def test_replay_buffer_skips_non_replayable():
    buffer = _ReplayBuffer()
    before = buffer.next_seq
    buffer.stamp({"type": "message.delta", "seq": None})
    assert buffer.next_seq == before
    assert buffer.frames_after(0) == []


def test_broadcast_stamps_once_across_connections():
    channel = WebChatChannel()
    ws1, ws2 = _FakeWebSocket(), _FakeWebSocket()
    conn1 = _register(channel, ws1)
    conn2 = _register(channel, ws2)

    channel._broadcast(turn_completed(turn_id="t", content="hi"))
    f1 = [f for f in _queued(conn1) if f["type"] == "turn.completed"]
    f2 = [f for f in _queued(conn2) if f["type"] == "turn.completed"]
    assert f1 and f2
    assert f1[0]["seq"] == f2[0]["seq"] == 1


# ── send / dedupe ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_publishes_inbound_and_acks():
    channel = WebChatChannel()
    bus, _ = _start_channel(channel)
    ws = _FakeWebSocket()
    conn = _register(channel, ws)
    cmid = str(uuid4())

    await channel._handle_send(
        conn,
        {"type": "send", "client_message_id": cmid, "content": "你好", "media": []},
    )

    inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)
    assert isinstance(inbound, InboundMessage)
    assert inbound.channel == "chat"
    assert inbound.session_key == "chat:local"
    assert inbound.tenant_id == DEFAULT_TENANT
    assert inbound.metadata["client_message_id"] == cmid
    acked = [f for f in _queued(conn) if f["type"] == "message.accepted"]
    assert len(acked) == 1
    assert acked[0]["client_message_id"] == cmid


@pytest.mark.asyncio
async def test_send_dedupes_client_message_id():
    channel = WebChatChannel()
    bus, _ = _start_channel(channel)
    ws = _FakeWebSocket()
    conn = _register(channel, ws)
    cmid = str(uuid4())

    await channel._handle_send(
        conn, {"type": "send", "client_message_id": cmid, "content": "你好"}
    )
    await channel._handle_send(
        conn, {"type": "send", "client_message_id": cmid, "content": "你好"}
    )

    first = await asyncio.wait_for(bus.consume_inbound(), timeout=1)
    assert first.metadata["client_message_id"] == cmid
    # 队列里没有第二条 inbound。
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.consume_inbound(), timeout=0.1)
    accepted = [f for f in _queued(conn) if f["type"] == "message.accepted"]
    assert len(accepted) == 2
    assert accepted[0]["seq"] == accepted[1]["seq"]


@pytest.mark.asyncio
async def test_send_rejects_bad_frames():
    channel = WebChatChannel()
    _start_channel(channel)
    ws = _FakeWebSocket()
    conn = _register(channel, ws)

    await channel._handle_send(conn, {"type": "send", "content": "x"})
    await channel._handle_send(
        conn, {"type": "send", "client_message_id": "not-a-uuid", "content": "x"}
    )
    await channel._handle_send(
        conn, {"type": "send", "client_message_id": str(uuid4()), "content": "  "}
    )

    assert [f["code"] for f in _queued(conn)] == [
        "bad_client_message_id",
        "bad_client_message_id",
        "bad_request",
    ]


# ── event 桥接与 channel 过滤 ────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_delta_only_for_own_channel():
    channel = WebChatChannel()
    _start_channel(channel)
    ws = _FakeWebSocket()
    conn = _register(channel, ws)

    await channel._on_stream_delta(
        StreamDeltaReady(
            session_key="chat:local", channel="telegram", chat_id="1", content_delta="x"
        )
    )
    assert _queued(conn) == []

    await channel._on_stream_delta(
        StreamDeltaReady(
            session_key="chat:local",
            channel="chat",
            chat_id="local",
            turn_id="t1",
            content_delta="你好",
        )
    )
    frames = _queued(conn)
    assert len(frames) == 1
    frame = frames[0]
    assert frame["type"] == "message.delta"
    assert frame["content_delta"] == "你好"
    assert frame["seq"] is None


@pytest.mark.asyncio
async def test_tool_frames_forwarded():
    channel = WebChatChannel()
    _start_channel(channel)
    ws = _FakeWebSocket()
    conn = _register(channel, ws)

    await channel._on_tool_started(
        ToolCallStarted(
            session_key="chat:local",
            channel="chat",
            chat_id="local",
            iteration=0,
            call_id="c1",
            tool_name="read_file",
            arguments={},
        )
    )
    await channel._on_tool_completed(
        ToolCallCompleted(
            session_key="chat:local",
            channel="chat",
            chat_id="local",
            iteration=0,
            call_id="c1",
            tool_name="read_file",
            arguments={},
            final_arguments={},
            status="done",
            result_preview="ok",
        )
    )

    assert [f["type"] for f in _queued(conn)] == ["tool.started", "tool.completed"]


# ── 连接生命周期 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_websocket_lifecycle_and_hello():
    channel = WebChatChannel()
    _start_channel(channel)

    class _BlockingWebSocket(_FakeWebSocket):
        started = asyncio.Event()

        async def receive_text(self) -> str:
            self.started.set()
            await asyncio.sleep(3600)

    ws = _BlockingWebSocket()
    task = asyncio.create_task(channel.handle_websocket(ws))
    try:
        await asyncio.wait_for(_BlockingWebSocket.started.wait(), timeout=2)
        await asyncio.sleep(0.05)
        assert ws.accepted
        assert ws.sent and ws.sent[0]["type"] == "hello"
        assert ws.sent[0]["session_key"] == DEV_SESSION_KEY
        assert ws.sent[0]["protocol_version"] == 0
    finally:
        task.cancel()
        with suppress_cancel():
            await task
    assert channel._connections == {}


def suppress_cancel():
    import contextlib

    return contextlib.suppress(asyncio.CancelledError)


@pytest.mark.asyncio
async def test_two_connections_share_replay_seq():
    channel = WebChatChannel()
    _start_channel(channel)
    ws1, ws2 = _FakeWebSocket(), _FakeWebSocket()
    conn1 = _register(channel, ws1)
    conn2 = _register(channel, ws2)

    channel._broadcast(turn_completed(turn_id="t", content="hi"))
    f1 = [f for f in _queued(conn1) if f["type"] == "turn.completed"]
    f2 = [f for f in _queued(conn2) if f["type"] == "turn.completed"]
    assert f1 and f2
    assert f1[0]["seq"] == f2[0]["seq"]


# ── save_upload ──────────────────────────────────────────────────


def test_save_upload_and_media_allowlist(tmp_path: Path):
    channel = WebChatChannel()
    # start() 用 session_manager.workspace 初始化 AttachmentStore。
    _start_channel(channel, SessionManager(tmp_path))

    result = channel.save_upload(b"data", "a.png")
    assert Path(result["path"]).read_bytes() == b"data"
    assert result["url"].startswith("/api/chat/media?path=")
    assert channel.has_media(Path(result["path"]))
    assert not channel.has_media(tmp_path / "outside.png")
