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
    # C9 onboarding 门禁：本套件只测附件语义，先把该账号 tenant 的 persona
    # 一次性设置完成（否则 uploads/media 一律 403 persona_onboarding_required）。
    async def _onboard() -> None:
        from sqlalchemy import text as _text

        from bootstrap.db.repository.persona_repo import PersonaRepository
        from infra.storage.partitioning import partition_name_for_tenant

        canonical = getattr(runtime, "canonical_repo", None)
        convs = await canonical.list_conversations_by_account(
            "00000000-0000-0000-0000-000000000001"
        )
        tenant_id = str(convs[0]["tenant_id"]) if convs else "dev"
        # memory_items 为租户 LIST 分区表：persona 种子写入前先保证分区存在
        # （生产时序由 provisioning 就绪服务完成）。
        pname = partition_name_for_tenant(tenant_id)
        async with runtime.session_factory() as sess, sess.begin():
            await sess.execute(
                _text(
                    f"CREATE TABLE IF NOT EXISTS {pname} PARTITION OF memory_items "
                    f"FOR VALUES IN ('{tenant_id}')"
                )
            )
        repo = PersonaRepository(runtime.session_factory)
        if not await repo.has_profile(tenant_id):
            await repo.submit_onboarding(
                tenant_id=tenant_id, source="custom",
                identity="attachment-test persona", personality_rules="rules",
                self_model="self",
            )

    asyncio.run(_onboard())
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


def _attachment_row(att_pg_url: str, attachment_id: str) -> dict[str, object]:
    """直读 attachments 行（核验 metadata 与磁盘字节是否一致）。"""
    import psycopg

    conn = psycopg.connect(att_pg_url, autocommit=True)
    row = conn.execute(
        "SELECT tenant_id, storage_key, checksum_sha256, size_bytes, status "
        "FROM attachments WHERE id = %s",
        (attachment_id,),
    ).fetchone()
    conn.close()
    assert row is not None, attachment_id
    keys = ("tenant_id", "storage_key", "checksum", "size_bytes", "status")
    return dict(zip(keys, row))


def test_media_bytes_match_recorded_checksum(
    client: TestClient, authed_cookies: dict[str, str], att_pg_url: str
) -> None:
    """spec「metadata 与 blob 一一对应可核验」：读出字节的 sha256 等于记录值。"""
    import hashlib

    png = _png_bytes()
    resp = client.post("/api/chat/uploads?filename=photo.png", content=png)
    assert resp.status_code == 200
    att_id = resp.json()["attachment_id"]
    media = client.get("/api/chat/media", params={"attachment_id": att_id})
    assert media.status_code == 200

    row = _attachment_row(att_pg_url, att_id)
    assert row["checksum"] == hashlib.sha256(png).hexdigest()
    assert row["size_bytes"] == len(png)
    # ADR-1：未被消息引用的上传停在 staged（§5.9.15 未引用 24h），不是 committed
    assert row["status"] == "staged"


def test_media_size_mismatch_is_404(
    client: TestClient,
    authed_cookies: dict[str, str],
    att_pg_url: str,
    tmp_path: Path,
) -> None:
    """blob 被截断（size 与 metadata 不符）→ 不服务可疑字节，读取 404。

    staged 行不进入 missing：规格里 missing 定义为「metadata 已提交但 blob 缺失」，
    未引用的 staged 由 24h 清理收敛。
    """
    from agent.tools.path_resolver import tenant_dirname

    png = _png_bytes()
    resp = client.post("/api/chat/uploads?filename=photo.png", content=png)
    att_id = resp.json()["attachment_id"]
    row = _attachment_row(att_pg_url, att_id)
    blob = (
        tmp_path
        / "tenants"
        / tenant_dirname(str(row["tenant_id"]))
        / "attachments"
        / str(row["storage_key"])
    )
    blob.write_bytes(png[: len(png) // 2])

    media = client.get("/api/chat/media", params={"attachment_id": att_id})
    assert media.status_code == 404
    after = _attachment_row(att_pg_url, att_id)
    assert after["status"] == "staged"


def test_media_size_mismatch_on_committed_marks_missing(
    client: TestClient,
    authed_cookies: dict[str, str],
    att_pg_url: str,
    tmp_path: Path,
) -> None:
    """已提交（被引用）的附件字节损坏 → 404 + missing 标记，供 reconciliation 告警。"""
    import asyncio

    from agent.tools.path_resolver import tenant_dirname
    from bootstrap.db.repository.attachment_repo import AttachmentRepository
    import uuid

    png = _png_bytes()
    resp = client.post("/api/chat/uploads?filename=photo.png", content=png)
    att_id = resp.json()["attachment_id"]
    row = _attachment_row(att_pg_url, att_id)

    repo = AttachmentRepository(_runtime_of(client).session_factory)
    asyncio.run(
        repo.commit_attachment(
            account_id="00000000-0000-0000-0000-000000000001",
            tenant_id=str(row["tenant_id"]),
            attachment_id=uuid.UUID(att_id),
            referenced_ttl_days=30,
        )
    )

    blob = (
        tmp_path
        / "tenants"
        / tenant_dirname(str(row["tenant_id"]))
        / "attachments"
        / str(row["storage_key"])
    )
    blob.write_bytes(png[: len(png) // 2])

    media = client.get("/api/chat/media", params={"attachment_id": att_id})
    assert media.status_code == 404
    assert _attachment_row(att_pg_url, att_id)["status"] == "missing"


def test_media_rejects_legacy_path_parameter(    client: TestClient, authed_cookies: dict[str, str]
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


def test_upload_truncated_image_rejected_415(
    client: TestClient, authed_cookies: dict[str, str]
) -> None:
    """畸形/截断图片：Pillow 的 OSError 面收口成 415，绝不冒 500。"""
    resp = client.post(
        "/api/chat/uploads?filename=broken.png",
        content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
    )
    assert resp.status_code == 415
    assert resp.json()["detail"] == "upload_type_denied"


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
    # 未产生任何本地附件文件（旧单用户 uploads/ 与租户命名空间 tenants/ 都不该出现）
    assert not (tmp_path / "uploads").exists()
    assert not (tmp_path / "attachments").exists()
    assert not (tmp_path / "tenants").exists()