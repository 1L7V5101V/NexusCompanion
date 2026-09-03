"""bootstrap/chat_api.py 的 FastAPI 路由与 WS 握手测试。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from bootstrap.chat_api import create_chat_app
from bus.queue import MessageBus
from infra.channels.contract import ChannelContext
from infra.channels.web_chat_channel import WebChatChannel
from session.manager import SessionManager


class _Ctx:
    bus: MessageBus
    session_manager: SessionManager
    event_bus: Any
    push_tool: Any
    attachment_store: Any
    http_resources: Any
    interrupt_controller: Any
    bot_commands: list
    log: Any


def _build(tmp_path: Path) -> tuple[TestClient, WebChatChannel, MessageBus]:
    bus = MessageBus()
    from bus.event_bus import EventBus

    event_bus = EventBus()
    channel = WebChatChannel()
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
    client = TestClient(app)
    return client, channel, bus


def test_index_returns_status_json_without_bundle(tmp_path: Path) -> None:
    client, _, _ = _build(tmp_path)
    resp = client.get("/")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["channel"] == "chat"


def test_sessions_and_messages_routes(tmp_path: Path) -> None:
    client, _, _ = _build(tmp_path)
    resp = client.get("/api/chat/sessions")
    assert resp.status_code == 200
    assert "items" in resp.json() and "total" in resp.json()

    resp = client.get("/api/chat/sessions/chat%3Alocal/messages")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and "total" in body


@pytest.mark.asyncio
async def test_ws_hello_and_send_roundtrip(tmp_path: Path) -> None:
    client, channel, bus = _build(tmp_path)
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["protocol_version"] == 0
        assert hello["session_key"] == "chat:local"

        cmid = str(uuid4())
        ws.send_json({"type": "send", "client_message_id": cmid, "content": "你好"})
        accepted = ws.receive_json()
        assert accepted["type"] == "message.accepted"
        assert accepted["client_message_id"] == cmid

    inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)
    assert inbound.session_key == "chat:local"
    _ = channel


def test_upload_endpoint_writes_file(tmp_path: Path) -> None:
    client, channel, _ = _build(tmp_path)
    resp = client.post("/api/chat/uploads?filename=note.txt", content=b"hello")
    assert resp.status_code == 200
    body = resp.json()
    assert Path(body["path"]).read_bytes() == b"hello"
    assert body["url"].startswith("/api/chat/media?path=")

    media = client.get("/api/chat/media", params={"path": body["path"]})
    assert media.status_code == 200
    assert media.content == b"hello"
    _ = channel


def test_media_rejects_path_outside_upload_roots(tmp_path: Path) -> None:
    client, _, _ = _build(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    resp = client.get("/api/chat/media", params={"path": str(outside)})
    assert resp.status_code == 404
