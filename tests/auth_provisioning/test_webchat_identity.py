"""webchat-auth-wiring：会话 → WebChat 身份派生（§5.9.1）与启用门禁（design ADR-3/ADR-6）。

覆盖：
- `resolve_webchat_identity` 从已认证 session 派生 account/tenant/conversation，
  且 fail-closed（无账号、无 canonical conversation 均拒绝，不回落 `DEFAULT_TENANT`）；
- 不同账号得到不同 tenant（越权负向）；
- 每条连接的身份是唯一授权来源：连接身份覆盖通道身份，客户端声明 tenant 被忽略；
- `build_chat_server` 三态门禁：auth 或 dev 之一成立才放行，两者皆无 fail-fast。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from bootstrap.auth.identity import (
    WebChatIdentityError,
    resolve_webchat_identity,
)
from bootstrap.chat_api import build_chat_server
from agent.config import load_config
from bus.event_bus import EventBus
from bus.queue import MessageBus
from infra.channels.contract import ChannelContext
from infra.channels.web_chat_channel import (
    WebChatChannel,
    WebChatIdentity,
    _Connection,
)
from infra.storage.tenancy import DEFAULT_TENANT


# ── stubs ────────────────────────────────────────────────────────


class _CanonicalRepoStub:
    """按 account_id 返回预置 canonical 会话（确定性，不触 PG）。"""

    def __init__(self, by_account: dict[str, list[dict[str, Any]]]) -> None:
        self._by_account = by_account

    async def list_conversations_by_account(self, account_id: Any) -> list[dict[str, Any]]:
        return list(self._by_account.get(str(account_id), []))


class _AuthRuntimeStub:
    def __init__(self, by_account: dict[str, list[dict[str, Any]]]) -> None:
        self.canonical_repo = _CanonicalRepoStub(by_account)


class _SessionManagerStub:
    workspace: str | None = None


class _FakeWebSocket:
    async def accept(self) -> None:
        return None

    async def send_json(self, data: Any) -> None:
        return None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


def _conv(tenant_id: str, conv_id: str, account_id: str) -> dict[str, Any]:
    return {
        "id": conv_id,
        "tenant_id": tenant_id,
        "account_id": account_id,
        "status": "active",
    }


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


# ── 任务 2.1：身份派生自 session ──────────────────────────────────


@pytest.mark.asyncio
async def test_resolves_identity_from_authenticated_session() -> None:
    runtime = _AuthRuntimeStub({"acct-a": [_conv("tenant:acct-a", "conv-a", "acct-a")]})

    identity = await resolve_webchat_identity(runtime, {"account_id": "acct-a"})  # type: ignore[arg-type]

    assert identity.account_id == "acct-a"
    assert identity.tenant_id == "tenant:acct-a"
    assert identity.conversation_id == "conv-a"
    # session 路由键按 tenant 区分，避免不同登录者共用 chat:local
    assert identity.chat_id == "tenant:acct-a"
    assert identity.session_key == "chat:tenant:acct-a"
    # 不再回落到 dev 默认租户
    assert identity.tenant_id != DEFAULT_TENANT


@pytest.mark.asyncio
async def test_distinct_accounts_get_distinct_tenants() -> None:
    runtime = _AuthRuntimeStub(
        {
            "acct-a": [_conv("tenant:acct-a", "conv-a", "acct-a")],
            "acct-b": [_conv("tenant:acct-b", "conv-b", "acct-b")],
        }
    )

    a = await resolve_webchat_identity(runtime, {"account_id": "acct-a"})  # type: ignore[arg-type]
    b = await resolve_webchat_identity(runtime, {"account_id": "acct-b"})  # type: ignore[arg-type]

    assert a.tenant_id != b.tenant_id
    assert a.conversation_id != b.conversation_id
    assert {a.tenant_id, b.tenant_id} == {"tenant:acct-a", "tenant:acct-b"}


# ── 任务 2.4 / ADR-6：fail-closed，不回退 dev 身份 ────────────────


@pytest.mark.asyncio
async def test_rejects_session_without_account() -> None:
    runtime = _AuthRuntimeStub({})

    with pytest.raises(WebChatIdentityError):
        await resolve_webchat_identity(runtime, {"account_id": None})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_rejects_account_without_canonical_conversation() -> None:
    runtime = _AuthRuntimeStub({})

    with pytest.raises(WebChatIdentityError):
        await resolve_webchat_identity(runtime, {"account_id": "acct-unknown"})  # type: ignore[arg-type]


# ── 任务 2.2 / 2.3：连接身份是唯一授权来源 ────────────────────────


@pytest.mark.asyncio
async def test_connection_identity_overrides_channel_identity() -> None:
    channel = _make_channel(
        WebChatIdentity(tenant_id="tenant:channel", chat_id="channel", session_key="chat:channel")
    )
    bus = channel._require_ctx().bus
    conn = _Connection(  # type: ignore[arg-type]
        _FakeWebSocket(),
        uuid4().hex,
        identity=WebChatIdentity(
            account_id="acct-conn",
            tenant_id="tenant:conn",
            conversation_id="conv-conn",
            session_key="chat:tenant:conn",
            chat_id="tenant:conn",
        ),
    )

    await channel._handle_send(
        conn,
        {
            "type": "send",
            "client_message_id": str(uuid4()),
            "content": "hi",
            "tenant_id": "attacker-tenant",
            "account_id": "attacker-account",
            "session_key": "attacker-session",
        },
    )

    inbound = await bus.consume_inbound()
    # 连接身份胜出，且客户端声明的归属字段全部无效
    assert inbound.tenant_id == "tenant:conn"
    assert inbound.session_key == "chat:tenant:conn"
    assert inbound.metadata["client_message_id"]


@pytest.mark.asyncio
async def test_connection_without_identity_falls_back_to_channel_identity() -> None:
    """未传连接身份时回退通道身份（dev 回退语义，与改动前一致）。"""
    channel = _make_channel(
        WebChatIdentity(tenant_id="tenant:channel", chat_id="channel", session_key="chat:channel")
    )
    bus = channel._require_ctx().bus
    conn = _Connection(_FakeWebSocket(), uuid4().hex)  # type: ignore[arg-type]

    await channel._handle_send(
        conn,
        {"type": "send", "client_message_id": str(uuid4()), "content": "hi"},
    )

    inbound = await bus.consume_inbound()
    assert inbound.tenant_id == "tenant:channel"
    assert inbound.session_key == "chat:channel"


# ── 任务 3.1：三态启用门禁（design ADR-3） ────────────────────────


def test_gate_allows_dev_mode_without_auth() -> None:
    server = build_chat_server(
        workspace=Path("."),
        channel=_make_channel(),
        dev_mode=True,
    )
    assert server is not None


def test_gate_rejects_without_auth_and_without_dev() -> None:
    with pytest.raises(RuntimeError, match="dev_mode"):
        build_chat_server(
            workspace=Path("."),
            channel=_make_channel(),
            dev_mode=False,
        )


def test_gate_allows_auth_without_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """auth.enabled 时不再要求 dev_mode（本 change 的核心门禁变更）。"""
    from fastapi import APIRouter

    import bootstrap.auth as auth_pkg

    monkeypatch.setattr(auth_pkg, "build_auth_api", lambda runtime: APIRouter())

    server = build_chat_server(
        workspace=Path("."),
        channel=_make_channel(),
        dev_mode=False,
        auth_runtime=object(),  # type: ignore[arg-type]
    )
    assert server is not None


# ── 任务 3.2：非回环绑定的显式许可（C4 语义在 auth 模式下不变） ──────


def test_gate_rejects_non_loopback_host_without_allow_public_bind() -> None:
    with pytest.raises(RuntimeError, match="allow_public_bind"):
        build_chat_server(
            workspace=Path("."),
            channel=_make_channel(),
            dev_mode=True,
            host="0.0.0.0",
        )


def test_gate_allows_non_loopback_host_with_explicit_permission() -> None:
    """容器部署依赖此路径：容器内绑 0.0.0.0 + 显式允许，宿主机只发布到回环。"""
    server = build_chat_server(
        workspace=Path("."),
        channel=_make_channel(),
        dev_mode=True,
        host="0.0.0.0",
        allow_public_bind=True,
    )
    assert server is not None


def test_gate_auth_mode_also_requires_explicit_public_bind() -> None:
    """启用认证不豁免非回环绑定许可（认证 ≠ 可以随便绑）。"""
    with pytest.raises(RuntimeError, match="allow_public_bind"):
        build_chat_server(
            workspace=Path("."),
            channel=_make_channel(),
            dev_mode=False,
            host="0.0.0.0",
            auth_runtime=object(),  # type: ignore[arg-type]
        )


# ── 任务 3.4：认证模式不得以打开 dev_mode 为代价 ──────────────────


def test_auth_enabled_does_not_imply_dev_mode(tmp_path: Path) -> None:
    """只开 `[auth]`、不开 `agent.dev_mode` 的配置：dev_mode 必须保持 False。

    `bootstrap/providers.py` 用 `dev_mode` 决定是否把 LLM 全量 payload 落盘，
    因此用 `dev_mode` 当放行后门会汄漏式把请求/响应正文写进日志（design ADR-3）。
    """
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "\n".join(
            [
                'provider = "openai"',
                "",
                "[llm.main]",
                'model = "test-model"',
                'api_key = "test-key"',
                "",
                "[channels.chat]",
                "enabled = true",
                "",
                "[auth]",
                "enabled = true",
                "cookie_secure = true",
                'origin_allowlist = ["https://nexus.example.com"]',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(cfg_path)

    assert config.auth.enabled is True
    assert config.channels.chat.enabled is True
    assert config.dev_mode is False


def test_auth_defaults_require_explicit_deployment_config() -> None:
    """`AuthConfig` 默认：关闭 + 空 allowlist。

    空 allowlist 意味着登录（exchange）会被 Origin 校验全部拒绝，
    所以部署时必须显式填入实际来源（公网域名）。
    """
    from bootstrap.auth.service import AuthConfig as _AuthConfig

    default = _AuthConfig()
    assert default.enabled is False
    assert default.origin_allowlist == []
    assert default.cookie_secure is True
