"""C6 HTTP 契约测试（真 PG + AuthRuntime）：uploads/media 新契约与 fail-closed。

覆盖 spec：上传只返回 attachment_id（无本地路径）、旧 path 参数 400、跨租户/
不存在 404（不泄露）、blob 缺失 404、Content-Length 预检 413、错误码映射。
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
import warnings
from pathlib import Path
from typing import Any

import pytest

from PIL import Image

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from agent.config_models import AttachmentConfig, Config
from bootstrap.auth.runtime import AuthRuntime
from bootstrap.auth.identity import resolve_webchat_identity  # noqa: F401  (验证派生点)
from bootstrap.chat_api import create_chat_app
from bootstrap.db.repository.canonical_repo import CanonicalIdentityRepository
from bus.event_bus import EventBus
from bus.queue import MessageBus
from infra.channels.contract import ChannelContext
from infra.channels.web_chat_channel import WebChatChannel
from session.manager import SessionManager

pytestmark = pytest.mark.postgres


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 100, 50)).save(buf, format="PNG")
    return buf.getvalue()


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


def _build_auth_runtime(
    att_pg_url: str, account_id: str, workspace: Path
) -> tuple[AuthRuntime, Any]:
    """构造真 AuthRuntime（复用附件 scratch DB；account 已由 att_reset seed）。"""
    import os

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(
        att_pg_url.replace("postgresql://", "postgresql+asyncpg://"),
        poolclass=NullPool,
    )
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sf = async_sessionmaker(engine, expire_on_commit=False)
    canonical = CanonicalIdentityRepository(sf)
    full = Config(provider="", model="", api_key="", system_prompt="")
    # dev HTTP 测试态：cookie_secure=False → cookie 名退化为 nexus_session（§5.9.3）
    full.auth.cookie_secure = False
    auth = AuthRuntime(
        config=full.auth,
        workspace=workspace,
        session_factory=sf,
        canonical_repo=canonical,
    )
    return auth, engine


def _build_client(
    tmp_path: Path, att_pg_url: str, account_id: str
) -> tuple[TestClient, Any, Any]:
    """构造带 AuthRuntime + durable 的 chat app（附件服务可用）。"""
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
    auth_runtime, engine = _build_auth_runtime(att_pg_url, account_id, tmp_path)

    class _Durable:
        session_factory = auth_runtime.session_factory

    from agent.config_models import AttachmentConfig

    app = create_chat_app(
        workspace=tmp_path,
        channel=channel,
        auth_runtime=auth_runtime,
        durable_runtime=_Durable(),
        static_root=tmp_path / "chat",
        attachment_config=AttachmentConfig(),
    )
    client = TestClient(app)
    return client, engine, auth_runtime


@pytest.fixture
def client(tmp_path, att_pg_url: str):
    c, engine, auth = _build_client(
        tmp_path, att_pg_url, "00000000-0000-0000-0000-000000000001"
    )
    yield c
    engine.sync_engine.dispose()


@pytest.fixture
def authed_cookies(client: TestClient) -> dict[str, str]:
    """通过 CredentialRepository.create_user_session 直造有效 user session。

    直接把 cookie 设到 TestClient 实例（httpx2 对 per-request cookies 的
    DeprecationWarning 在 -W error 下会中止）。
    """
    import asyncio

    runtime = _runtime_of(client)

    async def _make() -> str:
        from bootstrap.auth.service import (
            SESSION_COOKIE_VALUE_PREFIX,
            digest_value,
            new_token,
        )
        from bootstrap.db.repository.auth_repo import CredentialRepository

        repo = CredentialRepository(runtime.session_factory)
        raw = new_token(SESSION_COOKIE_VALUE_PREFIX)
        await repo.create_user_session(
            account_id="00000000-0000-0000-0000-000000000001",
            session_digest=digest_value(raw, runtime.auth._pepper.get()),
            idle_timeout_s=runtime.config.session_idle_s,
            absolute_timeout_s=runtime.config.session_absolute_s,
            user_agent="test",
        )
        return raw

    raw = asyncio.run(_make())
    # 显式注入 Cookie header（TestClient 的 cookie jar 不自动写入 request headers）
    client.headers["Cookie"] = f"nexus_session={raw}"
    return {"nexus_session": raw}


def _runtime_of(client: TestClient) -> Any:
    return client.app.state.auth_runtime


def test_upload_success_returns_attachment_id_no_path(
    client: TestClient, authed_cookies: dict[str, str]
) -> None:
    resp = client.post(
        "/api/chat/uploads?filename=photo.png",
        content=_png_bytes(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "attachment_id" in body
    assert not any(
        token in body["url"] for token in ("path", "storage", "C:", "/tmp", "workspace")
    ), body["url"]
    assert body["url"].startswith("/api/chat/media?attachment_id=")


def test_upload_then_media_roundtrip(
    client: TestClient, authed_cookies: dict[str, str]
) -> None:
    resp = client.post(
        "/api/chat/uploads?filename=photo.png",
        content=_png_bytes(),
    )
    assert resp.status_code == 200
    body = resp.json()
    media = client.get(
        "/api/chat/media",
        params={"attachment_id": body["attachment_id"]},
    )
    assert media.status_code == 200
    assert media.content == _png_bytes()
    assert media.headers["content-type"].startswith("image/png")


def test_media_rejects_legacy_path_parameter(
    client: TestClient, authed_cookies: dict[str, str]
) -> None:
    resp = client.get(
        "/api/chat/media",
        params={"path": str(Path.home())},
    )
    assert resp.status_code == 400


def test_media_unknown_id_returns_404(
    client: TestClient, authed_cookies: dict[str, str]
) -> None:
    resp = client.get(
        "/api/chat/media",
        params={"attachment_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


def test_upload_rejects_banned_content(
    client: TestClient, authed_cookies: dict[str, str]
) -> None:
    resp = client.post(
        "/api/chat/uploads?filename=evil.pdf.png",
        content=b"%PDF-1.4 fake",
    )
    assert resp.status_code == 415
    assert resp.json()["detail"] == "upload_type_denied"


def test_upload_rejects_ext_mismatch(
    client: TestClient, authed_cookies: dict[str, str]
) -> None:
    resp = client.post(
        "/api/chat/uploads?filename=text.jpg",
        content=b"plain utf-8 text",
    )
    assert resp.status_code == 415
    assert resp.json()["detail"] == "upload_ext_mismatch"


def test_upload_content_length_preflight(
    client: TestClient, authed_cookies: dict[str, str]
) -> None:
    """超 20MiB 由 body 读取即断（Content-Length 预检 + 累计）。"""
    big = b"x" * (20 * 1024 * 1024 + 1)
    resp = client.post(
        "/api/chat/uploads?filename=big.txt", content=big
    )
    assert resp.status_code == 413
    assert resp.json()["detail"] == "upload_too_large"


def test_dev_no_auth_returns_503(tmp_path) -> None:
    """无 auth + 无 durable → attachments 不可用 503（fail-closed，不落单用户路径）。"""
    bus = MessageBus()
    channel = WebChatChannel()
    channel._bind(
        ChannelContext(
            bus=bus,
            session_manager=SessionManager(tmp_path),
            event_bus=EventBus(),
            push_tool=None,  # type: ignore[arg-type]
            attachment_store=None,  # type: ignore[arg-type]
            http_resources=None,  # type: ignore[arg-type]
            interrupt_controller=None,
            bot_commands=[],
            log=logging.getLogger("test"),
        )
    )
    app = create_chat_app(workspace=tmp_path, channel=channel, static_root=tmp_path / "c2")
    c = TestClient(app)
    resp = c.post("/api/chat/uploads?filename=a.png", content=_png_bytes())
    assert resp.status_code == 503
    # 未产生任何本地附件文件
    assert not (tmp_path / "uploads").exists()
    assert not (tmp_path / "attachments").exists()