"""dev-only 暴露门禁与身份边界测试（task-04 验收第 5/6 条）。

覆盖三层门禁（配置/绑定/运行期）与「客户端不得声明可信 tenant」的负向语义。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from bootstrap.chat_api import (
    _DevOnlyGuardMiddleware,
    build_chat_server,
    create_chat_app,
    is_loopback_host,
)
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
    DEV_ACCOUNT_ID,
    DEV_SESSION_KEY,
    DEV_TENANT_ID,
)
from infra.storage.tenancy import DEFAULT_TENANT


class _SessionManagerStub:
    workspace: str | None = None


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


def _make_channel(identity: WebChatIdentity | None = None) -> WebChatChannel:
    channel = WebChatChannel(identity=identity)
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


class _RecordingApp:
    def __init__(self) -> None:
        self.called: list[dict[str, Any]] = []

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        self.called.append(scope)


async def _invoke_guard(
    middleware: _DevOnlyGuardMiddleware, scope: dict[str, Any]
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(scope, _receive, _send)
    return sent


# ── 第 1 层：回环判定 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.5.5.5", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("testclient", True),
        ("0.0.0.0", False),
        ("10.0.0.9", False),
        ("8.8.8.8", False),
        ("", False),
    ],
)
def test_is_loopback_host(host: str, expected: bool):
    assert is_loopback_host(host) is expected


# ── 第 2 层：绑定门禁 ─────────────────────────────────────────────


def test_build_chat_server_requires_dev_mode(tmp_path: Path):
    channel = _make_channel()
    with pytest.raises(RuntimeError, match="dev_mode"):
        _ = build_chat_server(workspace=tmp_path, channel=channel, dev_mode=False)


def test_build_chat_server_rejects_non_loopback_bind_without_opt_in(tmp_path: Path):
    channel = _make_channel()
    with pytest.raises(RuntimeError, match="非回环地址"):
        _ = build_chat_server(
            workspace=tmp_path, channel=channel, dev_mode=True, host="0.0.0.0"
        )
    # 显式 opt-in 才允许（P1 前禁止公网，这里覆盖的是「显式才放行」语义）。
    server = build_chat_server(
        workspace=tmp_path,
        channel=channel,
        dev_mode=True,
        host="0.0.0.0",
        allow_public_bind=True,
    )
    assert server is not None


def test_build_chat_server_allows_loopback(tmp_path: Path):
    channel = _make_channel()
    server = build_chat_server(
        workspace=tmp_path, channel=channel, dev_mode=True, host="127.0.0.1"
    )
    assert server is not None


# ── 第 3 层：运行期回环中间件 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_runtime_guard_rejects_non_loopback_http():
    app = _RecordingApp()
    guard = _DevOnlyGuardMiddleware(app, allow_public_bind=False)
    scope = {"type": "http", "client": ("10.0.0.9", 1234), "headers": []}

    sent = await _invoke_guard(guard, scope)

    assert app.called == []
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 403
    assert sent[1]["type"] == "http.response.body"


@pytest.mark.asyncio
async def test_runtime_guard_rejects_non_loopback_websocket():
    app = _RecordingApp()
    guard = _DevOnlyGuardMiddleware(app, allow_public_bind=False)
    scope = {"type": "websocket", "client": ("10.0.0.9", 1234), "headers": []}

    sent = await _invoke_guard(guard, scope)

    assert app.called == []
    assert sent == [
        {"type": "websocket.close", "code": CLOSE_DEV_ONLY, "reason": "dev-only"}
    ]


@pytest.mark.asyncio
async def test_runtime_guard_allows_loopback():
    app = _RecordingApp()
    guard = _DevOnlyGuardMiddleware(app, allow_public_bind=False)
    scope = {"type": "http", "client": ("127.0.0.1", 1234), "headers": []}

    _ = await _invoke_guard(guard, scope)

    assert len(app.called) == 1


@pytest.mark.asyncio
async def test_runtime_guard_opt_in_disables_layer():
    app = _RecordingApp()
    guard = _DevOnlyGuardMiddleware(app, allow_public_bind=True)
    scope = {"type": "http", "client": ("10.0.0.9", 1234), "headers": []}

    _ = await _invoke_guard(guard, scope)

    assert len(app.called) == 1


# ── 身份边界：客户端字段不参与授权 ───────────────────────────────


def test_identity_defaults_match_protocol_constants():
    identity = WebChatIdentity()
    assert identity.account_id == DEV_ACCOUNT_ID
    assert identity.tenant_id == DEV_TENANT_ID == DEFAULT_TENANT
    assert identity.conversation_id == DEV_SESSION_KEY
    assert identity.session_key == DEV_SESSION_KEY


@pytest.mark.asyncio
async def test_client_declared_identity_is_ignored():
    channel = _make_channel()
    bus = channel._require_ctx().bus
    websocket = _FakeWebSocket()
    conn = _Connection(websocket, uuid4().hex)  # type: ignore[arg-type]
    cmid = str(uuid4())

    # 客户端刻意声明另一套 tenant/account/session/channel。
    await channel._handle_send(
        conn,
        {
            "type": "send",
            "client_message_id": cmid,
            "content": "hi",
            "tenant_id": "attacker-tenant",
            "account_id": "attacker-account",
            "session_key": "telegram:999",
            "channel": "telegram",
        },
    )

    inbound = await bus.consume_inbound()
    assert inbound.tenant_id == DEV_TENANT_ID
    assert inbound.channel == "chat"
    assert inbound.session_key == DEV_SESSION_KEY
    assert inbound.metadata["client_message_id"] == cmid


@pytest.mark.asyncio
async def test_injected_identity_is_the_only_authority():
    identity = WebChatIdentity(
        account_id="acct:x",
        tenant_id="tenant:x",
        conversation_id="conv:x",
        session_key="chat:x",
    )
    channel = _make_channel(identity)
    bus = channel._require_ctx().bus
    websocket = _FakeWebSocket()
    conn = _Connection(websocket, uuid4().hex)  # type: ignore[arg-type]

    await channel._handle_send(
        conn,
        {
            "type": "send",
            "client_message_id": str(uuid4()),
            "content": "hi",
            "tenant_id": "attacker-tenant",
        },
    )

    inbound = await bus.consume_inbound()
    assert inbound.tenant_id == "tenant:x"


def test_create_chat_app_applies_runtime_guard(tmp_path: Path):
    channel = _make_channel()
    app = create_chat_app(workspace=tmp_path, channel=channel)
    # 中间件栈里必须存在 dev-only 运行期门禁。
    assert any(
        middleware.cls is _DevOnlyGuardMiddleware for middleware in app.user_middleware
    )
