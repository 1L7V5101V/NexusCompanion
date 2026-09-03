"""dev 模式 WebChat channel adapter（P0.5）。

把 WebSocket 入站消息提交到 MessageBus，把 EventBus 生命周期事件与
outbound 回复转发给已连接的浏览器连接。帧协议契约见
``infra/channels/web_chat_protocol.py`` 与
``tests/fixtures/chat_protocol_frames.json``。

边界（对齐 PILOT_ROADMAP P0.5）：无认证、DEFAULT_TENANT、单 canonical
session ``chat:local``、仅本地 dev 使用；P1 才接入邀请 Token 认证与
tenant 派生。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import WebSocket

from bus.events import InboundMessage, OutboundMessage
from bus.events_lifecycle import (
    StreamDeltaReady,
    ToolCallCompleted,
    ToolCallStarted,
)
from infra.channels.base import AttachmentStore, MessageDeduper
from infra.channels.contract import ChannelContext
from infra.channels.web_chat_protocol import (
    DEV_SESSION_KEY,
    SOFT_LIMIT,
    error as error_frame,
    hello,
    is_droppable,
    is_replayable,
    message_accepted,
    message_delta,
    pong,
    replay_required,
    tool_completed,
    tool_started,
    turn_completed,
    turn_failed,
)
from infra.storage.tenancy import DEFAULT_TENANT

logger = logging.getLogger(__name__)

_REPLAY_BUFFER_SIZE = 500
_OUTBOUND_QUEUE_SIZE = 256
_DEDUPER_SIZE = 500
_SENDER_DRAIN_TIMEOUT_S = 2.0


class _Connection:
    """一条已 accept 的 WebSocket 连接及其有界 outbound 队列。"""

    def __init__(self, websocket: WebSocket, connection_id: str) -> None:
        self.websocket = websocket
        self.connection_id = connection_id
        self.outbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            _OUTBOUND_QUEUE_SIZE
        )
        self.sender_task: asyncio.Task[None] | None = None
        self.closed = False

    def try_enqueue(self, frame: dict[str, Any]) -> bool:
        try:
            self.outbound.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            return False


@dataclass
class _ReplayBuffer:
    """进程内有界重放 buffer（dev v0；P1 由 PG durable sequence 替换）。

    只缓存 terminal/ack 帧（``is_replayable``），在入 buffer 时统一分配
    单调 seq；delta/tool 帧不占用 seq，也不参与重放。
    """

    max_size: int = _REPLAY_BUFFER_SIZE
    oldest_seq: int = 1
    next_seq: int = 1
    frames: deque[tuple[int, dict[str, Any]]] = field(default_factory=deque)

    def stamp(self, frame: dict[str, Any]) -> dict[str, Any]:
        """为 terminal/ack 帧盖单调 seq 并入 buffer；其他帧原样返回。"""
        if not is_replayable(str(frame.get("type") or "")):
            return frame
        stamped = dict(frame)
        stamped["seq"] = self.next_seq
        self.frames.append((self.next_seq, stamped))
        self.next_seq += 1
        while len(self.frames) > self.max_size:
            _ = self.frames.popleft()
        self.oldest_seq = self.frames[0][0] if self.frames else self.next_seq
        return stamped

    def frames_after(self, after_seq: int) -> list[dict[str, Any]] | None:
        """返回需要补发的帧；after_seq 过旧（超出 buffer）返回 None。"""
        if after_seq >= self.next_seq - 1:
            return []
        if after_seq < self.oldest_seq - 1:
            return None
        return [frame for seq, frame in self.frames if seq > after_seq]


class WebChatChannel:
    """FastAPI WebSocket 宿主 + MessageBus/EventBus 桥接（dev 单用户）。"""

    def __init__(self, channel_name: str = "chat") -> None:
        self.name = channel_name
        self._ctx: ChannelContext | None = None
        self._connections: dict[WebSocket, _Connection] = {}
        self._replay = _ReplayBuffer()
        self._deduper = MessageDeduper(_DEDUPER_SIZE)
        self._accepted_frames: dict[str, dict[str, Any]] = {}
        self._attachments: AttachmentStore | None = None
        self._subscriptions: list[Any] = []

    # ── chat_api.py 期望的接口面 ────────────────────────────────

    def _require_ctx(self) -> ChannelContext:
        if self._ctx is None:
            raise RuntimeError("WebChatChannel 尚未启动")
        return self._ctx

    def upload_roots(self) -> list[Path]:
        if self._attachments is None:
            return []
        return [self._attachments.root]

    def has_media(self, path: Path) -> bool:
        if self._attachments is None:
            return False
        try:
            _ = path.resolve().relative_to(self._attachments.root.resolve())
            return True
        except ValueError:
            return False

    def save_upload(self, data: bytes, filename: str) -> dict[str, str]:
        attachments = self._attachments
        assert attachments is not None
        suffix = Path(filename).suffix or ".bin"
        path = attachments.write_bytes(data, prefix="chat_", suffix=suffix)
        return {
            "path": str(path),
            "url": f"/api/chat/media?path={quote(str(path))}",
        }

    # ── Channel 生命周期 ────────────────────────────────────────

    async def start(self, ctx: ChannelContext) -> None:
        self._bind(ctx)

    def _bind(self, ctx: ChannelContext) -> None:
        """同步装配（测试与 start 共用；当前无真正的异步初始化）。"""
        self._ctx = ctx
        ws = getattr(ctx.session_manager, "workspace", None)
        self._attachments = AttachmentStore(Path(ws) / "uploads" if ws else None)
        ctx.bus.subscribe_outbound(self.name, self._on_outbound)
        self._subscriptions = [
            ctx.event_bus.on(StreamDeltaReady, self._on_stream_delta),
            ctx.event_bus.on(ToolCallStarted, self._on_tool_started),
            ctx.event_bus.on(ToolCallCompleted, self._on_tool_completed),
        ]
        logger.info("WebChatChannel 已启动（channel=%s）", self.name)

    async def stop(self) -> None:
        for sub in self._subscriptions:
            sub.close()
        self._subscriptions.clear()
        for conn in list(self._connections.values()):
            conn.closed = True
            _ = conn.outbound.put_nowait(None)
        for conn in list(self._connections.values()):
            if conn.sender_task is not None:
                try:
                    await asyncio.wait_for(conn.sender_task, _SENDER_DRAIN_TIMEOUT_S)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    conn.sender_task.cancel()
        self._connections.clear()
        logger.info("WebChatChannel 已停止")

    # ── WebSocket 连接处理 ──────────────────────────────────────

    async def handle_websocket(self, websocket: WebSocket) -> None:
        await websocket.accept()
        connection_id = uuid4().hex
        conn = _Connection(websocket, connection_id)
        self._connections[websocket] = conn

        hello_frame = hello(
            connection_id=connection_id,
            session_key=DEV_SESSION_KEY,
            latest_seq=self._replay.next_seq - 1,
        )
        conn.sender_task = asyncio.create_task(self._sender_loop(conn))
        conn.try_enqueue(hello_frame)

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    frame = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    conn.try_enqueue(
                        error_frame(code="bad_frame", message="无法解析的帧")
                    )
                    continue
                await self._handle_client_frame(conn, frame)
        except Exception:
            # 客户端断开（WebSocketDisconnect 等）是正常退出路径；清理在 finally。
            pass
        finally:
            self._connections.pop(websocket, None)
            conn.closed = True
            _ = conn.outbound.put_nowait(None)
            if conn.sender_task is not None:
                try:
                    await asyncio.wait_for(conn.sender_task, _SENDER_DRAIN_TIMEOUT_S)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    conn.sender_task.cancel()

    async def _sender_loop(self, conn: _Connection) -> None:
        while True:
            frame = await conn.outbound.get()
            if frame is None or conn.closed:
                break
            try:
                await conn.websocket.send_json(frame)
            except Exception:
                break

    async def _handle_client_frame(self, conn: _Connection, frame: Any) -> None:
        if not isinstance(frame, dict):
            conn.try_enqueue(
                error_frame(code="bad_frame", message="帧必须是 JSON 对象")
            )
            return
        frame_type = str(frame.get("type") or "")
        if frame_type == "ping":
            conn.try_enqueue(pong())
            return
        if frame_type == "replay":
            after_seq = frame.get("after_seq")
            if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < 0:
                conn.try_enqueue(
                    error_frame(
                        code="bad_replay", message="after_seq 必须是非负整数"
                    )
                )
                return
            self._handle_replay(conn, after_seq)
            return
        if frame_type == "send":
            await self._handle_send(conn, frame)
            return
        conn.try_enqueue(
            error_frame(
                code="unknown_type",
                message=f"未知帧类型: {frame_type or '<empty>'}",
            )
        )

    def _handle_replay(self, conn: _Connection, after_seq: int) -> None:
        frames = self._replay.frames_after(after_seq)
        if frames is None:
            conn.try_enqueue(replay_required(after_seq=after_seq))
            return
        for frame in frames:
            _ = conn.try_enqueue(frame)

    async def _handle_send(self, conn: _Connection, frame: dict[str, Any]) -> None:
        ctx = self._require_ctx()
        client_message_id = str(frame.get("client_message_id") or "")
        content = str(frame.get("content") or "")
        media = frame.get("media")
        if not client_message_id:
            conn.try_enqueue(
                error_frame(
                    code="bad_client_message_id", message="client_message_id 必填"
                )
            )
            return
        try:
            _ = UUID(client_message_id)
        except ValueError:
            conn.try_enqueue(
                error_frame(
                    code="bad_client_message_id",
                    message="client_message_id 必须是 UUID",
                )
            )
            return
        if not content.strip() and not media:
            conn.try_enqueue(
                error_frame(code="bad_request", message="内容不能为空")
            )
            return

        # 幂等：重复 client_message_id 重放原 ack，不重复提交 inbound。
        cached = self._accepted_frames.get(client_message_id)
        if cached is not None:
            conn.try_enqueue(dict(cached))
            return

        inbound = InboundMessage(
            channel=self.name,
            sender="webchat",
            chat_id="local",
            content=content,
            media=[str(m) for m in media] if isinstance(media, list) else [],
            metadata={"client_message_id": client_message_id, "username": "webchat"},
            tenant_id=DEFAULT_TENANT,
        )
        await ctx.bus.publish_inbound(inbound)

        accepted = self._replay.stamp(
            message_accepted(
                client_message_id=client_message_id, session_key=DEV_SESSION_KEY
            )
        )
        self._accepted_frames[client_message_id] = dict(accepted)
        if len(self._accepted_frames) > _DEDUPER_SIZE:
            self._accepted_frames.pop(next(iter(self._accepted_frames)))
        conn.try_enqueue(accepted)

    # ── EventBus / outbound 桥接 ────────────────────────────────

    def _broadcast(self, frame: dict[str, Any]) -> None:
        """向所有连接广播一帧；慢消费者按协议语义降级。

        dev 模式只有一个 canonical session，无需按 session 过滤；所有事件
        handler 已按 channel 过滤。terminal/ack 帧先在 channel 级盖一次
        seq（保证多连接看到同一 seq 且 buffer 不重复入帧）。
        """
        frame = self._replay.stamp(frame)
        for conn in list(self._connections.values()):
            if conn.closed:
                continue
            if conn.outbound.qsize() >= _OUTBOUND_QUEUE_SIZE:
                continue
            if conn.outbound.qsize() >= SOFT_LIMIT and is_droppable(
                str(frame.get("type") or "")
            ):
                continue
            _ = conn.try_enqueue(frame)

    async def _on_stream_delta(self, event: StreamDeltaReady) -> None:
        if event.channel != self.name:
            return
        if not event.content_delta and not event.thinking_delta:
            return
        self._broadcast(
            message_delta(
                turn_id=event.turn_id,
                content_delta=event.content_delta,
                thinking_delta=event.thinking_delta,
            )
        )

    async def _on_tool_started(self, event: ToolCallStarted) -> None:
        if event.channel != self.name:
            return
        self._broadcast(
            tool_started(
                turn_id=event.turn_id, call_id=event.call_id, tool_name=event.tool_name
            )
        )

    async def _on_tool_completed(self, event: ToolCallCompleted) -> None:
        if event.channel != self.name:
            return
        self._broadcast(
            tool_completed(
                turn_id=event.turn_id,
                call_id=event.call_id,
                tool_name=event.tool_name,
                status=event.status,
                result_preview=event.result_preview,
            )
        )

    async def _on_outbound(self, msg: OutboundMessage) -> None:
        turn_id = str(msg.control_turn_id or "")
        if bool(msg.metadata.get("nexus_error")):
            frame = turn_failed(turn_id=turn_id, error=str(msg.content))
        else:
            frame = turn_completed(
                turn_id=turn_id,
                content=msg.content,
                thinking=msg.thinking,
                media=list(msg.media),
            )
        self._broadcast(frame)
