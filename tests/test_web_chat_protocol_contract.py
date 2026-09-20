"""WebChat 协议契约测试（后端侧，与前端脚本共用同一 fixture）。

同一份 ``tests/fixtures/chat_protocol_frames.json`` 由本文件与
``frontend/chat/scripts/protocol-contract.test.mjs`` 双向消费：帧形状、常量、
错误用例任一侧漂移都会失败（roadmap 5.9.4 / task-04 验收第 3 条）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from bus.event_bus import EventBus
from bus.queue import MessageBus
from infra.channels.contract import ChannelContext
from infra.channels.web_chat_channel import (
    WebChatChannel,
    WebChatIdentity,
    _Connection,
)
from infra.channels.web_chat_protocol import (
    CLOSE_DEV_ONLY,
    CLOSE_IDLE_TIMEOUT,
    CLOSE_OVERLOAD,
    DEV_ACCOUNT_ID,
    DEV_SESSION_KEY,
    DEV_TENANT_ID,
    ERROR_CODES,
    SOFT_LIMIT,
    error as error_frame,
    hello,
    is_droppable,
    is_replayable,
    is_seq_stamped,
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

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "chat_protocol_frames.json").read_text(
        encoding="utf-8"
    )
)
SERVER_FRAMES: dict[str, dict[str, Any]] = FIXTURE["server_to_client"]
SEMANTICS: dict[str, Any] = FIXTURE["replay_semantics"]


class _SessionManagerStub:
    workspace: str | None = None


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed_with: tuple[int, str] | None = None

    async def accept(self) -> None:
        return None

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


def _make_channel() -> WebChatChannel:
    channel = WebChatChannel()
    channel._bind(
        ChannelContext(
            bus=MessageBus(),
            session_manager=_SessionManagerStub(),  # type: ignore[arg-type]
            event_bus=EventBus(),
            push_tool=None,  # type: ignore[arg-type]
            attachment_store=None,  # type: ignore[arg-type]
            http_resources=None,  # type: ignore[arg-type]
            interrupt_controller=None,
            bot_commands=[],
            log=logging.getLogger("test"),
        )
    )
    return channel


def _queued(conn: _Connection) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while not conn.outbound.empty():
        entry = conn.outbound.get_nowait()
        if entry is not None:
            frame, _size = entry
            items.append(frame)
    return items


# ── 常量与身份 ────────────────────────────────────────────────────


def test_fixture_identity_and_constants_match_protocol():
    identity = FIXTURE["identity"]
    assert DEV_ACCOUNT_ID == identity["dev_account_id"]
    assert DEV_SESSION_KEY == identity["session_key"] == identity["dev_conversation_id"]
    # dev tenant 与显式单用户默认租户必须同值（两处漂移会被这条断言拦住）。
    assert DEV_TENANT_ID == identity["dev_tenant_id"] == DEFAULT_TENANT
    assert FIXTURE["protocol_version"] == SERVER_FRAMES["hello"]["protocol_version"]
    assert CLOSE_OVERLOAD == SEMANTICS["overload_close_code"]
    assert CLOSE_IDLE_TIMEOUT == SEMANTICS["idle_close_code"]
    assert CLOSE_DEV_ONLY == SEMANTICS["dev_only_close_code"]
    assert SOFT_LIMIT == SEMANTICS["soft_limit"]
    assert set(ERROR_CODES) == set(FIXTURE["error_codes"])


def test_fixture_replay_semantics_match_helpers():
    replayable = SEMANTICS["replayable_types"]
    droppable = SEMANTICS["droppable_types"]
    never = SEMANTICS["never_replayed_types"]

    assert [t for t in SERVER_FRAMES if is_replayable(t)] == replayable
    assert [t for t in SERVER_FRAMES if is_droppable(t)] == droppable
    assert [t for t in SERVER_FRAMES if is_seq_stamped(t)] == replayable
    for frame_type in never:
        assert not is_replayable(frame_type)


def test_channel_defaults_use_frozen_ws_limits():
    """慢消费者默认档位：soft 192 / hard 256 / 1 MiB（§5.9.5 + C3 冻结值）。"""
    from agent.admission.queues import (
        WS_OUTBOUND_HARD_LIMIT,
        WS_OUTBOUND_MAX_PAYLOAD_BYTES,
        WS_OUTBOUND_SOFT_LIMIT,
    )

    channel = WebChatChannel()
    assert WS_OUTBOUND_SOFT_LIMIT == SOFT_LIMIT == SEMANTICS["soft_limit"]
    assert channel._ws_soft_limit == WS_OUTBOUND_SOFT_LIMIT
    assert channel._ws_hard_limit == WS_OUTBOUND_HARD_LIMIT == SEMANTICS["hard_limit"]
    assert channel._ws_max_payload == WS_OUTBOUND_MAX_PAYLOAD_BYTES
    assert channel._ws_max_payload == SEMANTICS["max_payload_bytes"]


def test_default_idle_timeout_leaves_room_for_keepalive():
    """前端 keepalive 25s，服务端默认空闲超时 90s（3.6 倍余量）。"""
    channel = WebChatChannel()
    assert channel._ws_idle_timeout == 90.0


# ── 帧形状：协议工厂 == fixture 向量 ──────────────────────────────


def test_fixture_frame_shapes_match_factories():
    cases = SERVER_FRAMES
    assert (
        hello(
            connection_id="0f8b7a1c2d3e4f5a6b7c8d9e0f1a2b3c",
            latest_seq=42,
        )
        == cases["hello"]
    )
    assert (
        message_accepted(
            client_message_id="3f2a9c1e-8b4d-4c3a-9e2f-1a2b3c4d5e6f",
            session_key="chat:local",
        )
        == cases["message.accepted"]
    )
    assert (
        message_delta(turn_id="turn-0f8b", content_delta="你好") == cases["message.delta"]
    )
    assert (
        tool_started(turn_id="turn-0f8b", call_id="call_01", tool_name="search_messages")
        == cases["tool.started"]
    )
    assert (
        tool_completed(
            turn_id="turn-0f8b",
            call_id="call_01",
            tool_name="search_messages",
            status="done",
            result_preview="共 3 条结果",
        )
        == cases["tool.completed"]
    )
    assert (
        turn_completed(turn_id="turn-0f8b", content="完整回复正文") == cases["turn.completed"]
    )
    assert (
        turn_failed(turn_id="turn-0f8b", error="处理消息时出错，请稍后再试。")
        == cases["turn.failed"]
    )
    assert replay_required(after_seq=12) == cases["replay_required"]
    assert error_frame(code="bad_frame", message="无法解析的帧") == cases["error"]
    assert pong() == cases["pong"]


# ── 错误用例矩阵：fixture 驱动的预期错误码 ────────────────────────


@pytest.mark.parametrize("case_name", sorted(FIXTURE["error_cases"]))
@pytest.mark.asyncio
async def test_error_case_matrix(case_name: str):
    case = FIXTURE["error_cases"][case_name]
    channel = _make_channel()
    websocket = _FakeWebSocket()
    conn = _Connection(websocket, uuid4().hex)  # type: ignore[arg-type]

    await channel._handle_client_frame(conn, case["frame"])

    frames = _queued(conn)
    errors = [f for f in frames if f["type"] == "error"]
    assert len(errors) == 1, f"{case_name}: expected one error frame, got {frames}"
    assert errors[0]["code"] == case["expect"]["code"]
    assert errors[0]["code"] in ERROR_CODES


@pytest.mark.asyncio
async def test_hello_carries_server_derived_identity():
    channel = _make_channel()
    websocket = _FakeWebSocket()
    conn = _Connection(websocket, uuid4().hex)  # type: ignore[arg-type]
    identity = WebChatIdentity()

    frame = hello(
        connection_id=conn.connection_id,
        account_id=identity.account_id,
        tenant_id=identity.tenant_id,
        conversation_id=identity.conversation_id,
        session_key=identity.session_key,
        latest_seq=0,
    )

    assert frame["account_id"] == DEV_ACCOUNT_ID
    assert frame["tenant_id"] == DEV_TENANT_ID
    assert frame["conversation_id"] == DEV_SESSION_KEY
    assert frame["connection_id"] == conn.connection_id
    assert frame["latest_seq"] == 0
