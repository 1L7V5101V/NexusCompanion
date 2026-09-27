"""webchat-auth-wiring 任务 5.2：真实 PG 上的 exchange → handshake → hello e2e。

真实 uvicorn + 真实 WebSocket 客户端 + 真实 Cookie + 真实 PostgreSQL（复用
``nexus_c5test`` scratch 库，alembic 全链迁移）：邀请 token → ``POST
/api/auth/exchange`` 拿真实 Set-Cookie → WS 携带 Cookie + Origin 握手 → hello
返回 PG 派生三元组 → 收发一轮（流式 delta + 终态帧）。负向：无 Cookie 与
Origin 不在 allowlist 均在握手期拒绝，不产生 hello、不入队。

与 ``tests/test_web_chat_e2e_dev.py``（dev 回退身份、不触 PG）及本目录其余
TestClient 级测试的区别：本文件覆盖「真实 HTTP 网络往返 + 真实 PG session
校验 + C1 身份派生」的整条链，作为部署前验收证据。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets
import websockets.exceptions

from agent.config_models import AuthConfig, Config
from bootstrap.auth import create_auth_runtime
from bootstrap.chat_api import create_chat_app
from bus.event_bus import EventBus
from bus.events import InboundMessage
from bus.queue import MessageBus
from infra.channels.contract import ChannelContext
from infra.channels.web_chat_channel import WebChatChannel
from session.manager import SessionManager
from tests.test_web_chat_e2e_dev import (
    _agent_stub,
    _free_port,
    _recv,
    _send,
    _serve,
)

pytestmark = pytest.mark.postgres

ORIGIN = "http://localhost:5173"


async def _make_runtime(c5_pg_url: str, workspace: Path) -> Any:
    full = Config(provider="", model="", api_key="", system_prompt="")
    full.storage.postgres_url = c5_pg_url.replace(
        "postgresql://", "postgresql+asyncpg://"
    )
    full.auth = AuthConfig(
        cookie_secure=False,
        origin_allowlist=[ORIGIN],
        admin_allow_ips=["127.0.0.1", "::1"],
    )
    return create_auth_runtime(config=full, workspace=workspace)


def _auth_app(
    tmp_path: Path, runtime: Any, bus: MessageBus, event_bus: EventBus
) -> Any:
    channel = WebChatChannel()
    channel._bind(
        ChannelContext(
            bus=bus,
            session_manager=SessionManager(tmp_path / "sessions"),
            event_bus=event_bus,
            push_tool=None,  # type: ignore[arg-type]
            attachment_store=None,  # type: ignore[arg-type]
            http_resources=None,  # type: ignore[arg-type]
            interrupt_controller=None,
            bot_commands=[],
            log=logging.getLogger("e2e-auth-pg"),
        )
    )
    return create_chat_app(
        workspace=tmp_path, channel=channel, auth_runtime=runtime
    )


async def _exchange_cookie(port: int, token: str) -> str:
    """真实 HTTP 兑换：邀请 token → Set-Cookie 的 session 值。"""
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        resp = await client.post(
            "/api/auth/exchange",
            json={"token": token},
            headers={"Origin": ORIGIN},
        )
    assert resp.status_code == 200, resp.text
    jar = SimpleCookie(resp.headers["set-cookie"])
    return jar["nexus_session"].value


async def _assert_handshake_rejected(port: int, **kwargs: Any) -> None:
    """握手期被拒：无升级成功、无任何帧。"""
    with contextlib.suppress(
        websockets.exceptions.InvalidStatus, websockets.exceptions.ConnectionClosed
    ):
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/ws",
            open_timeout=5,
            **kwargs,
        ) as ws:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=2)


async def test_e2e_exchange_handshake_hello_send_round(
    c5_pg_url: str, tmp_path: Path
) -> None:
    runtime = await _make_runtime(c5_pg_url, tmp_path / "secrets")
    try:
        created = await runtime.provisioning.create_account(display_name="E2E")
        await runtime.provisioning.run_pending(max_jobs=4)
        account = await runtime.provisioning.get_account(created["account"]["id"])
        assert account["status"] == "active"
        _, raw_token = await runtime.auth.issue_invitation(
            account["id"], issued_by="e2e"
        )
        convs = await runtime.canonical_repo.list_conversations_by_account(
            account["id"]
        )
        assert convs, "provisioning 完成后必须有 canonical conversation"
        conv = convs[0]

        bus = MessageBus()
        event_bus = EventBus()
        app = _auth_app(tmp_path, runtime, bus, event_bus)
        recorded: list[InboundMessage] = []
        async with _serve(app, bus) as port:
            stub = asyncio.create_task(_agent_stub(bus, event_bus, recorded))
            try:
                session_value = await _exchange_cookie(port, raw_token)

                # 负向：无 Cookie / Origin 不在 allowlist → 握手拒绝
                await _assert_handshake_rejected(port, origin=ORIGIN)
                await _assert_handshake_rejected(
                    port,
                    origin="https://evil.example.com",
                    additional_headers={"Cookie": f"nexus_session={session_value}"},
                )

                # 正向：Cookie + Origin → hello 返回 PG 派生三元组
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/ws",
                    origin=ORIGIN,
                    additional_headers={
                        "Cookie": f"nexus_session={session_value}"
                    },
                ) as ws:
                    hello = await _recv(ws)
                    assert hello["type"] == "hello"
                    assert hello["account_id"] == str(account["id"])
                    assert hello["tenant_id"] == str(conv["tenant_id"])
                    assert hello["conversation_id"] == str(conv["id"])
                    assert hello["latest_seq"] == 0

                    cmid = await _send(
                        ws,
                        "你好",
                        tenant_id="attacker-tenant",
                        session_key="telegram:9",
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
                assert "".join(d["content_delta"] for d in deltas) == "你好"
                assert completed and completed[0]["content"] == "你好"

                # 客户端声明的归属字段不参与授权：入站归属来自 PG 派生身份。
                assert recorded[0].tenant_id == str(conv["tenant_id"])
                assert recorded[0].session_key == f"chat:{conv['tenant_id']}"
            finally:
                if not stub.done():
                    _ = stub.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stub
    finally:
        await runtime.aclose()
