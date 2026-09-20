"""WebChat dev 闭环端到端测试（真实入口 + 真实 uvicorn + 真实 WebSocket）。

用真实 ``create_chat_app``（Gateway 路由）与真实 ``uvicorn`` 服务，客户端用真实
``websockets`` 连接，覆盖 task-04 验收第 1/2/7/8 条：

1. dev 模式收发消息并收到 AgentLoop 回复 + 流式更新（LLM 用 stub worker 代替，
   协议链路本身是真实的）；
2. 重连补拉无重复、顺序稳定；
3. 断线只影响显示，不取消服务端 turn/tool；
4. 异常连接（静默）被心跳超时回收。

PG 与真实 LLM 不在本测试范围（dev 路径不依赖它们）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
import uvicorn
import websockets

from bootstrap.chat_api import create_chat_app
from bus.event_bus import EventBus
from bus.events import InboundMessage, OutboundMessage
from bus.events_lifecycle import StreamDeltaReady
from bus.queue import MessageBus
from infra.channels.contract import ChannelContext
from infra.channels.web_chat_channel import WebChatChannel
from infra.channels.web_chat_protocol import (
    CLOSE_IDLE_TIMEOUT,
    DEV_ACCOUNT_ID,
    DEV_SESSION_KEY,
    DEV_TENANT_ID,
)
from session.manager import SessionManager

RECV_TIMEOUT_S = 5.0


class _SessionManagerStub:
    """占位：本测试用真实 SessionManager，保留给类型参考。"""

    workspace: str | None = None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _runtime(
    tmp_path: Path,
    *,
    idle_timeout_s: float = 90.0,
    replay_buffer_size: int | None = None,
) -> tuple[Any, MessageBus, EventBus, WebChatChannel]:
    bus = MessageBus()
    event_bus = EventBus()
    channel = WebChatChannel(
        ws_idle_timeout_s=idle_timeout_s, replay_buffer_size=replay_buffer_size
    )
    channel._bind(
        ChannelContext(
            bus=bus,
            session_manager=SessionManager(tmp_path),
            event_bus=event_bus,
            push_tool=None,  # type: ignore[arg-type]
            attachment_store=None,  # type: ignore[arg-type]
            http_resources=None,  # type: ignore[arg-type]
            interrupt_controller=None,
            bot_commands=[],
            log=logging.getLogger("test"),
        )
    )
    app = create_chat_app(workspace=tmp_path, channel=channel)
    return app, bus, event_bus, channel


@contextlib.asynccontextmanager
async def _serve(app: Any, bus: MessageBus) -> AsyncIterator[int]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
    )
    server_task = asyncio.create_task(server.serve())
    dispatch_task = asyncio.create_task(bus.dispatch_outbound())
    try:
        for _ in range(300):
            if server.started:
                break
            await asyncio.sleep(0.01)
        if not server.started:
            raise RuntimeError("uvicorn 未在预期时间内启动")
        yield port
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)
        _ = dispatch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dispatch_task


async def _agent_stub(
    bus: MessageBus,
    event_bus: EventBus,
    recorded: list[InboundMessage],
    *,
    turn_id: str = "turn-e2e",
    delay: float = 0.0,
    chunks: tuple[str, ...] = ("你", "好"),
    done: asyncio.Event | None = None,
    max_turns: int | None = None,
) -> None:
    """最小 AgentLoop 替身：入站 → 若干 delta → 终态 outbound。"""
    turns = 0
    while max_turns is None or turns < max_turns:
        msg = cast(InboundMessage, await bus.consume_inbound())
        turns += 1
        recorded.append(msg)
        for chunk in chunks:
            if delay:
                await asyncio.sleep(delay)
            _ = await event_bus.emit(
                StreamDeltaReady(
                    session_key=msg.session_key,
                    channel="chat",
                    chat_id="local",
                    turn_id=turn_id,
                    content_delta=chunk,
                )
            )
        await bus.publish_outbound(
            OutboundMessage(
                channel="chat",
                chat_id="local",
                content="".join(chunks),
                media=[],
                metadata={},
                control_turn_id=turn_id,
            )
        )
        if done is not None:
            done.set()


async def _stop(task: asyncio.Task[None]) -> None:
    if not task.done():
        _ = task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _send(ws: Any, content: str, **extra: Any) -> str:
    cmid = str(uuid4())
    frame: dict[str, Any] = {
        "type": "send",
        "client_message_id": cmid,
        "content": content,
        "media": [],
    }
    frame.update(extra)
    await ws.send(json.dumps(frame))
    return cmid


async def _recv(ws: Any) -> dict[str, Any]:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S))


# ── 1. 端到端收发 + 流式 + 终态 ──────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_dev_send_receives_stream_and_completion(tmp_path: Path):
    app, bus, event_bus, _channel = _runtime(tmp_path)
    recorded: list[InboundMessage] = []
    async with _serve(app, bus) as port:
        stub = asyncio.create_task(_agent_stub(bus, event_bus, recorded))
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
                hello = await _recv(ws)
                assert hello["type"] == "hello"
                assert hello["protocol_version"] == 0
                assert hello["account_id"] == DEV_ACCOUNT_ID
                assert hello["tenant_id"] == DEV_TENANT_ID
                assert hello["conversation_id"] == DEV_SESSION_KEY
                assert hello["latest_seq"] == 0

                cmid = await _send(
                    ws, "你好", tenant_id="attacker-tenant", session_key="telegram:9"
                )

                frames: list[dict[str, Any]] = []
                while True:
                    frame = await _recv(ws)
                    frames.append(frame)
                    if frame["type"] in {"turn.completed", "turn.failed"}:
                        break

            accepted = [f for f in frames if f["type"] == "message.accepted"]
            deltas = [f for f in frames if f["type"] == "message.delta"]
            completed = [f for f in frames if f["type"] == "turn.completed"]

            assert accepted and accepted[0]["client_message_id"] == cmid
            assert accepted[0]["seq"] == 1
            assert "".join(d["content_delta"] for d in deltas) == "你好"
            assert all(d["seq"] is None for d in deltas)
            assert completed and completed[0]["content"] == "你好"
            assert completed[0]["seq"] == 2

            # 客户端声明的 tenant/session 不参与授权。
            assert recorded[0].tenant_id == DEV_TENANT_ID
            assert recorded[0].session_key == DEV_SESSION_KEY
        finally:
            await _stop(stub)


# ── 2. 重连补拉：无重复、顺序稳定 ────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_reconnect_replay_no_duplicates(tmp_path: Path):
    app, bus, event_bus, _channel = _runtime(tmp_path)
    recorded: list[InboundMessage] = []
    async with _serve(app, bus) as port:
        stub = asyncio.create_task(_agent_stub(bus, event_bus, recorded))
        try:
            uri = f"ws://127.0.0.1:{port}/ws"
            async with websockets.connect(uri) as ws:
                _ = await _recv(ws)
                _ = await _send(ws, "你好")
                while (await _recv(ws))["type"] != "turn.completed":
                    pass

            async with websockets.connect(uri) as ws:
                hello = await _recv(ws)
                assert hello["latest_seq"] == 2

                await ws.send(json.dumps({"type": "replay", "after_seq": 1}))
                replayed = [await _recv(ws)]
                assert [f["type"] for f in replayed] == ["turn.completed"]
                assert replayed[0]["seq"] == 2

                # 已到最新游标：不应再补发任何帧（无重复）。
                await ws.send(json.dumps({"type": "replay", "after_seq": 2}))
                with pytest.raises(asyncio.TimeoutError):
                    _ = await asyncio.wait_for(ws.recv(), timeout=0.5)
        finally:
            await _stop(stub)


@pytest.mark.asyncio
async def test_e2e_replay_cursor_beyond_buffer_requires_rest_rebuild(tmp_path: Path):
    # buffer=1：跑完一轮后最旧 seq 已挤出（oldest_seq=2），after_seq=0 无法覆盖。
    app, bus, event_bus, _channel = _runtime(tmp_path, replay_buffer_size=1)
    recorded: list[InboundMessage] = []
    async with _serve(app, bus) as port:
        stub = asyncio.create_task(_agent_stub(bus, event_bus, recorded))
        try:
            uri = f"ws://127.0.0.1:{port}/ws"
            async with websockets.connect(uri) as ws:
                _ = await _recv(ws)
                _ = await _send(ws, "你好")
                while (await _recv(ws))["type"] != "turn.completed":
                    pass

            async with websockets.connect(uri) as ws:
                _ = await _recv(ws)
                await ws.send(json.dumps({"type": "replay", "after_seq": 0}))
                frame = await _recv(ws)
                assert frame["type"] == "replay_required"
                assert frame["after_seq"] == 0
        finally:
            await _stop(stub)


@pytest.mark.asyncio
async def test_e2e_rest_history_rebuild_endpoint_reachable(tmp_path: Path):
    """replay_required 后的 REST 重建通路可达（canonical message 由 agent loop 写入）。"""
    app, bus, _event_bus, _channel = _runtime(tmp_path)
    async with _serve(app, bus) as port:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/api/chat/sessions/chat%3Alocal/messages",
                params={"sort_order": "asc", "page_size": 200},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body and "total" in body


# ── 3. 断线不取消服务端 turn ─────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_disconnect_does_not_cancel_turn(tmp_path: Path):
    app, bus, event_bus, channel = _runtime(tmp_path)
    recorded: list[InboundMessage] = []
    done = asyncio.Event()
    async with _serve(app, bus) as port:
        stub = asyncio.create_task(
            _agent_stub(
                bus, event_bus, recorded, delay=0.2, done=done, max_turns=1
            )
        )
        try:
            uri = f"ws://127.0.0.1:{port}/ws"
            async with websockets.connect(uri) as ws:
                _ = await _recv(ws)
                _ = await _send(ws, "你好")
                accepted = await _recv(ws)
                assert accepted["type"] == "message.accepted"
            # 此刻连接已断开：turn 仍必须继续跑完（不被取消）。
            await asyncio.wait_for(done.wait(), timeout=RECV_TIMEOUT_S)
            assert stub.done() and not stub.cancelled()

            # 断线只影响显示：终态帧仍在重放 buffer，可由新连接补拉。
            async with websockets.connect(uri) as ws2:
                hello = await _recv(ws2)
                assert hello["latest_seq"] == 2
                await ws2.send(json.dumps({"type": "replay", "after_seq": 1}))
                replayed = await _recv(ws2)
                assert replayed["type"] == "turn.completed"
                assert replayed["content"] == "你好"

            # 断线连接被清理，连接表不残留。
            for _ in range(100):
                if not channel._connections:
                    break
                await asyncio.sleep(0.01)
            assert channel._connections == {}
        finally:
            await _stop(stub)


# ── 4. 静默连接回收 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_idle_connection_is_reaped(tmp_path: Path):
    app, bus, _event_bus, channel = _runtime(tmp_path, idle_timeout_s=0.3)
    async with _serve(app, bus) as port:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            _ = await _recv(ws)
            with pytest.raises(websockets.exceptions.ConnectionClosed) as excinfo:
                _ = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
            assert excinfo.value.rcvd is not None
            assert excinfo.value.rcvd.code == CLOSE_IDLE_TIMEOUT

        for _ in range(100):
            if not channel._connections:
                break
            await asyncio.sleep(0.01)
        assert channel._connections == {}
