"""C3 WS outbound 分级降级：soft 丢 delta+replay_required、hard 256/1MiB overload close。

直接驱动 ``WebChatChannel._broadcast`` 与真实 ``_Connection``（stub WebSocket），
不复制实现逻辑。
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from agent.admission.queues import (
    WS_OUTBOUND_HARD_LIMIT,
    WS_OUTBOUND_MAX_PAYLOAD_BYTES,
    WS_OUTBOUND_SOFT_LIMIT,
)
from infra.channels.web_chat_channel import WebChatChannel, _Connection, _frame_size
from infra.channels.web_chat_protocol import CLOSE_OVERLOAD, is_droppable


class _StubWebSocket:
    def __init__(self) -> None:
        self.closed_with: tuple[int, str] | None = None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


def _make_channel(**kwargs: int) -> WebChatChannel:
    return WebChatChannel(channel_name="chat", **kwargs)


def _make_conn(websocket: _StubWebSocket, **kwargs: int) -> _Connection:
    return _Connection(cast(Any, websocket), "c1", **kwargs)


def _bind_conn(channel: WebChatChannel, websocket: _StubWebSocket, conn: _Connection) -> None:
    cast(Any, channel)._connections[websocket] = conn


def _frame(kind: str, size_hint: int = 0) -> dict[str, Any]:
    frame: dict[str, Any] = {"type": kind}
    if size_hint:
        frame["payload"] = "x" * size_hint
    return frame


def _fill(conn: _Connection, count: int, kind: str = "turn_completed") -> None:
    for _ in range(count):
        assert conn.try_enqueue(_frame(kind))


def _queued_types(conn: _Connection) -> list[str]:
    return [str(entry[0].get("type")) for entry in list(conn.outbound._queue)]  # noqa: SLF001


def test_frozen_ws_limits() -> None:
    assert WS_OUTBOUND_SOFT_LIMIT == 192
    assert WS_OUTBOUND_HARD_LIMIT == 256
    assert WS_OUTBOUND_MAX_PAYLOAD_BYTES == 1024 * 1024
    assert is_droppable("message.delta")


def test_frame_size_measurable() -> None:
    assert _frame_size({"type": "ping"}) > 0
    assert _frame_size(_frame("message.delta", size_hint=100)) > 100


async def test_soft_limit_drops_droppable_and_requests_replay() -> None:
    channel = _make_channel(ws_outbound_soft_limit=2, ws_outbound_hard_limit=8)
    websocket = _StubWebSocket()
    conn = _make_conn(websocket, soft_limit=2, hard_limit=8)
    _bind_conn(channel, websocket, conn)
    _fill(conn, 2)

    # 达 soft：可丢帧（delta）不入队，发 replay_required 要求按 last_sequence 补拉
    channel._broadcast(_frame("message.delta"))
    assert conn.outbound.qsize() == 3
    assert _queued_types(conn)[-1] == "replay_required"
    # 重复丢弃不重复发 notice
    channel._broadcast(_frame("message.delta"))
    assert conn.outbound.qsize() == 3
    # 非 durable 终态帧在 soft 区间不被丢弃
    channel._broadcast(_frame("turn_completed"))
    assert conn.outbound.qsize() == 4
    assert not conn.closed


async def test_soft_limit_below_threshold_enqueues_normally() -> None:
    channel = _make_channel(ws_outbound_soft_limit=4, ws_outbound_hard_limit=8)
    websocket = _StubWebSocket()
    conn = _make_conn(websocket, soft_limit=4, hard_limit=8)
    _bind_conn(channel, websocket, conn)
    channel._broadcast(_frame("message.delta"))
    assert _queued_types(conn) == ["message.delta"]


async def test_hard_limit_closes_with_overload_code() -> None:
    channel = _make_channel(ws_outbound_soft_limit=1, ws_outbound_hard_limit=2)
    websocket = _StubWebSocket()
    conn = _make_conn(websocket, soft_limit=1, hard_limit=2)
    _bind_conn(channel, websocket, conn)
    _fill(conn, 2)
    # hard：即使终态帧也触发 close（不静默丢帧；终态保留在重放 buffer）
    channel._broadcast(_frame("turn_completed"))
    assert conn.closed
    await asyncio.sleep(0)
    assert websocket.closed_with is not None
    assert websocket.closed_with[0] == CLOSE_OVERLOAD
    assert "overload" in websocket.closed_with[1]


async def test_hard_payload_overload_closes() -> None:
    channel = _make_channel(
        ws_outbound_soft_limit=100,
        ws_outbound_hard_limit=100,
        ws_outbound_max_payload_bytes=150,
    )
    websocket = _StubWebSocket()
    conn = _make_conn(websocket, soft_limit=100, hard_limit=100, max_payload_bytes=150)
    _bind_conn(channel, websocket, conn)
    assert conn.try_enqueue(_frame("turn_completed", size_hint=150))
    assert conn.payload_over_limit()
    channel._broadcast(_frame("turn_completed", size_hint=100))
    assert conn.closed
    await asyncio.sleep(0)
    assert websocket.closed_with is not None
    assert websocket.closed_with[0] == CLOSE_OVERLOAD


async def test_payload_accounting_decrements_on_send() -> None:
    conn = _make_conn(_StubWebSocket())
    assert conn.try_enqueue(_frame("turn_completed", size_hint=50))
    assert conn.bytes_enqueued > 0
    entry = conn.outbound.get_nowait()
    conn.bytes_enqueued = max(0, conn.bytes_enqueued - entry[1])
    assert conn.bytes_enqueued == 0
