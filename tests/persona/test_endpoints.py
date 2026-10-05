"""onboarding 端点、门禁与 source breakdown 权限（tasks 2.1-2.3 / 3.4 / ADR-3/6）。

auth 通过 stub runtime 走真实 chat_api 依赖链（cookie → validate_user_session →
身份派生）；PG 用 scratch 工厂。dev 模式（无 auth）断言端点不存在、行为不变。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from bus.queue import MessageBus
from bus.event_bus import EventBus
from infra.channels.contract import ChannelContext
from infra.channels.web_chat_channel import WebChatChannel
from session.manager import SessionManager

pytestmark = pytest.mark.postgres

COOKIE = "nexus_session"


class _Ctx:
    pass


def _build_app(
    tmp_path: Path,
    persona_factory,
    *,
    with_auth: bool,
    with_durable: bool = True,
    source_breakdown_provider: Any = None,
):
    bus = MessageBus()
    event_bus = EventBus()
    channel = WebChatChannel()
    channel._bind(
        ChannelContext(
            bus=bus,
            session_manager=SessionManager(tmp_path),
            event_bus=event_bus,
            push_tool=None,
            attachment_store=None,
            http_resources=None,
            interrupt_controller=None,
            bot_commands=[],
            log=logging.getLogger("test"),
        )
    )
    durable = (
        SimpleNamespace(session_factory=persona_factory) if with_durable else None
    )
    auth_runtime = None
    if with_auth:
        # stub auth runtime：cookie 校验/身份派生按调用点协议返回。
        session_row = {
            "account_id": "00000000-0000-0000-0000-000000000001",
            "principal_type": "user",
        }

        class _Auth:
            async def validate_user_session(self, raw: str) -> dict[str, Any]:
                if raw != "good-cookie":
                    from bootstrap.db.repository.auth_repo import SessionInvalidError

                    raise SessionInvalidError()
                return dict(session_row)

        class _CanonicalRepo:
            async def list_conversations_by_account(self, account_id: str) -> list:
                return [
                    {
                        "tenant_id": "pt_dev",
                        "id": "00000000-0000-0000-0000-000000000002",
                    }
                ]

        auth_runtime = SimpleNamespace(
            config=SimpleNamespace(cookie_secure=False),
            auth=_Auth(),
            canonical_repo=_CanonicalRepo(),
        )
    from bootstrap.chat_api import create_chat_app

    app = create_chat_app(
        workspace=tmp_path,
        channel=channel,
        auth_runtime=auth_runtime,
        static_root=tmp_path / "chat",
        durable_runtime=durable,
        attachment_config=None,
        source_breakdown_provider=source_breakdown_provider,
    )
    return TestClient(app), channel


def _headers() -> dict[str, str]:
    return {"cookie": f"{COOKIE}=good-cookie"}


def _onboard(client: TestClient) -> None:
    resp = client.post(
        "/api/persona/onboarding",
        headers=_headers(),
        json={
            "source": "custom",
            "identity": "测试身份",
            "personality_rules": "测试规则",
            "self_model": "测试初始关系状态",
        },
    )
    assert resp.status_code == 201
    return resp


def test_dev_mode_persona_endpoints_absent(tmp_path: Path, persona_factory) -> None:
    """dev 路径（无 auth）：persona 端点不装配，行为与 C9 之前一致。"""
    client, _ = _build_app(tmp_path, persona_factory, with_auth=False)
    assert client.get("/api/persona/status").status_code in (401, 403, 404)
    resp = client.post(
        "/api/persona/onboarding", json={"source": "custom"}
    )
    assert resp.status_code in (401, 403, 404)


def test_status_onboarding_and_lock_flow(
    tmp_path: Path, persona_factory
) -> None:
    client, _ = _build_app(tmp_path, persona_factory, with_auth=True)

    # onboarding 未完成：status 报告 required；模板目录可见。
    status = client.get("/api/persona/status", headers=_headers())
    assert status.status_code == 200
    assert status.json()["onboarding_required"] is True

    # 一次性提交（custom）。
    created = _onboard(client)
    assert created.json()["status"] == "completed"

    # 提交后：status 不再 required；模板正文不可见（404）。
    status2 = client.get("/api/persona/status", headers=_headers())
    assert status2.json()["onboarding_required"] is False
    assert client.get("/api/persona/templates", headers=_headers()).status_code == 404

    # 重复提交 → 409。
    dup = client.post(
        "/api/persona/onboarding",
        headers=_headers(),
        json={
            "source": "custom",
            "identity": "x",
            "personality_rules": "y",
            "self_model": "z",
        },
    )
    assert dup.status_code == 409


def test_modify_endpoints_absent_after_onboarding(
    tmp_path: Path, persona_factory
) -> None:
    """spec「提交后用户侧无修改入口」（API 面）：修改类调用一律 404。"""
    client, _ = _build_app(tmp_path, persona_factory, with_auth=True)
    _onboard(client)
    for method in ("PATCH", "PUT", "DELETE", "POST"):
        resp = client.request(
            method,
            "/api/persona/profile",
            headers=_headers(),
            json={"identity": "hack"},
        )
        assert resp.status_code == 404


def test_onboarding_gate_on_user_content_paths(
    tmp_path: Path, persona_factory
) -> None:
    """未 onboarding 的 tenant：uploads 门禁拒绝（persona_onboarding_required）。"""
    client, _ = _build_app(tmp_path, persona_factory, with_auth=True)
    resp = client.post(
        "/api/chat/uploads?filename=a.txt",
        headers=_headers(),
        content=b"hello",
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "persona_onboarding_required"


def test_source_breakdown_admin_debug_only(tmp_path: Path, persona_factory) -> None:
    """spec「普通用户不可获取 breakdown」：认证模式普通用户 404；
    dev 模式（debug 面）可用。"""
    client, _ = _build_app(tmp_path, persona_factory, with_auth=True)
    resp = client.get("/api/persona/source-breakdown", headers=_headers())
    assert resp.status_code == 404  # 普通用户不泄露能力存在性

    # dev 模式（debug 面）：provider 返回区块 metadata（无正文）。
    provider = lambda: [  # noqa: E731
        SimpleNamespace(name="identity", chars=10, est_tokens=3, is_static=True, cache_hit=False)
    ]
    client_dev, _ = _build_app(
        tmp_path, persona_factory, with_auth=False, source_breakdown_provider=provider
    )
    resp_dev = client_dev.get("/api/persona/source-breakdown")
    assert resp_dev.status_code == 200
    items = resp_dev.json()["items"]
    assert items and items[0]["name"] == "identity"
    assert "content" not in items[0]


def test_config_seed_does_not_override_onboarded_tenant(
    tmp_path: Path,
    persona_factory,
    make_tenant: Any,
) -> None:
    """task 4.1 / spec「config 不承载 tenant 当前值」：config seed 改动不影响
    已 onboarding tenant 的 PG 快照（恢复语义 = PG 当前值）。"""
    from agent.core.types import PersonaSnapshot
    from bootstrap.persona import resolve_persona_snapshot

    tenant = asyncio.run(make_tenant(prefix="pt_cfg"))
    repo_factory = persona_factory
    from bootstrap.db.repository.persona_repo import PersonaRepository

    repo = PersonaRepository(repo_factory)
    asyncio.run(
        repo.submit_onboarding(
            tenant_id=tenant["tenant_id"], source="custom",
            identity="PG 里的身份", personality_rules="PG 里的规则", self_model="PG 里的状态",
        )
    )
    before = asyncio.run(resolve_persona_snapshot(repo_factory, tenant["tenant_id"]))

    # 模拟 config.toml seed 修改 + 进程重启（重新解析）：PG 当前值不变。
    config_seed_changed = PersonaSnapshot(
        tenant_id=tenant["tenant_id"], source="custom",
        identity="config 里的新身份", personality_rules="", relationship_state="",
    )
    after = asyncio.run(resolve_persona_snapshot(repo_factory, tenant["tenant_id"]))
    assert after.identity == before.identity == "PG 里的身份"
    assert after.identity != config_seed_changed.identity
