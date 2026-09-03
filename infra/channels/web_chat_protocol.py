"""WebChat dev v0 WebSocket 帧协议契约。

本模块是 P0.5 dev-mode WebChat 的帧类型与构造工厂的 single source of truth。
规范示例帧见 ``tests/fixtures/chat_protocol_frames.json``（roadmap 5.9.4 的
shared contract fixture）；后端测试直接参数化该文件，前端 protocol.ts 与其
保持一致。

约定：
- 帧是 JSON 文本，信封 ``{"type": str, "seq": int | None, ...}``；
- ``seq`` 由服务端按 channel 单调分配（dev 模式单 canonical session，一个
  计数器即可）；``seq=None`` 表示不可重放的在线帧（``pong``）；
- 完整历史引导一律走 REST ``GET /api/chat/sessions/{key}/messages``；
- delta / tool 帧为在线优化，不承诺重放；``turn.completed`` / ``turn.failed``
  与 ``message.accepted`` 属于可重放边界（dev v0 仅限进程内 ring buffer，
  超出 buffer 用 ``replay_required`` 指引客户端走 REST 重建，与 roadmap
  5.9.4 “delta 不跨重启重放、终态从持久层补拉”的冻结语义对齐）。
"""

from __future__ import annotations

from typing import Any

PROTOCOL_VERSION = 0

# Server → client
HELLO = "hello"
MESSAGE_ACCEPTED = "message.accepted"
MESSAGE_DELTA = "message.delta"
TOOL_STARTED = "tool.started"
TOOL_COMPLETED = "tool.completed"
TURN_COMPLETED = "turn.completed"
TURN_FAILED = "turn.failed"
REPLAY_REQUIRED = "replay_required"
ERROR = "error"
PONG = "pong"

# Client → server
SEND = "send"
REPLAY = "replay"
PING = "ping"

# WebSocket close code：outbound 队列持续过载时服务端主动断开。
CLOSE_OVERLOAD = 1013

# 慢消费者降级阈值：outbound 队列达到该深度时开始丢弃可丢帧。
SOFT_LIMIT = 192

# 发送时由服务端盖 seq 戳的帧；其中终端帧/ack 帧同时进入重放 buffer。
_SEQ_STAMPED = frozenset(
    {MESSAGE_ACCEPTED, MESSAGE_DELTA, TOOL_STARTED, TOOL_COMPLETED, TURN_COMPLETED, TURN_FAILED}
)
_REPLAYABLE = frozenset({MESSAGE_ACCEPTED, TURN_COMPLETED, TURN_FAILED})
_DROPPABLE = frozenset({MESSAGE_DELTA, TOOL_STARTED, TOOL_COMPLETED})

DEV_SESSION_KEY = "chat:local"


def is_seq_stamped(frame_type: str) -> bool:
    """该帧类型发送时分配 seq。"""
    return frame_type in _SEQ_STAMPED


def is_replayable(frame_type: str) -> bool:
    """该帧类型进入重放 buffer（delta/tool 帧不参与重放）。"""
    return frame_type in _REPLAYABLE


def is_droppable(frame_type: str) -> bool:
    """慢消费者降级时可以丢弃的帧类型。"""
    return frame_type in _DROPPABLE


def hello(*, connection_id: str, session_key: str, latest_seq: int) -> dict[str, Any]:
    return {
        "type": HELLO,
        "seq": None,
        "connection_id": connection_id,
        "protocol_version": PROTOCOL_VERSION,
        "session_key": session_key or DEV_SESSION_KEY,
        "latest_seq": latest_seq,
    }


def message_accepted(*, client_message_id: str, session_key: str) -> dict[str, Any]:
    return {
        "type": MESSAGE_ACCEPTED,
        "seq": None,
        "client_message_id": client_message_id,
        "session_key": session_key or DEV_SESSION_KEY,
    }


def message_delta(
    *,
    turn_id: str,
    content_delta: str = "",
    thinking_delta: str = "",
) -> dict[str, Any]:
    return {
        "type": MESSAGE_DELTA,
        "seq": None,
        "turn_id": turn_id,
        "content_delta": content_delta,
        "thinking_delta": thinking_delta,
    }


def tool_started(*, turn_id: str, call_id: str, tool_name: str) -> dict[str, Any]:
    return {
        "type": TOOL_STARTED,
        "seq": None,
        "turn_id": turn_id,
        "call_id": call_id,
        "tool_name": tool_name,
    }


def tool_completed(
    *,
    turn_id: str,
    call_id: str,
    tool_name: str,
    status: str,
    result_preview: str = "",
) -> dict[str, Any]:
    return {
        "type": TOOL_COMPLETED,
        "seq": None,
        "turn_id": turn_id,
        "call_id": call_id,
        "tool_name": tool_name,
        "status": status,
        "result_preview": result_preview,
    }


def turn_completed(
    *,
    turn_id: str,
    content: str,
    thinking: str | None = None,
    media: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": TURN_COMPLETED,
        "seq": None,
        "turn_id": turn_id,
        "content": content,
        "thinking": thinking,
        "media": media or [],
    }


def turn_failed(*, turn_id: str, error: str) -> dict[str, Any]:
    return {
        "type": TURN_FAILED,
        "seq": None,
        "turn_id": turn_id,
        "error": error,
    }


def replay_required(*, after_seq: int) -> dict[str, Any]:
    """客户端 buffer 不可覆盖时，指引其走 REST 重建。"""
    return {
        "type": REPLAY_REQUIRED,
        "seq": None,
        "after_seq": after_seq,
    }


def error(*, code: str, message: str) -> dict[str, Any]:
    return {"type": ERROR, "seq": None, "code": code, "message": message}


def pong() -> dict[str, Any]:
    return {"type": PONG, "seq": None}
