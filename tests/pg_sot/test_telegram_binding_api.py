"""C10 HTTP 端点验收（task 3.1/3.2）。

用户面绑定码签发/状态 + 管理面预绑定/列表/解绑：认证门禁、在途上限 429、
唯一冲突 409、绑定服务未装配时 404（不泄露能力存在性）、非 admin 401。
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bootstrap.auth.api import build_admin_api
from bootstrap.chat_api import create_chat_app
from bootstrap.telegram_binding import TelegramBindingService
from tests.auth_provisioning.test_http_contract import (
    DEV_ORIGIN,
    admin_authed,
    jar,
    make_runtime,
    parse_cookies,
    seed_user_credentials,
    user_exchange,
)

pytestmark = pytest.mark.postgres


def _build_app(runtime: Any, binding: Any, tmp_path: Any) -> TestClient:
    """chat_api 全量 app（用户路由）+ admin router（管理路由）。"""
    runtime.telegram_binding = binding
    app = create_chat_app(
        workspace=tmp_path,
        channel=SimpleNamespace(name="chat", _require_ctx=lambda: None),
        auth_runtime=runtime,
        static_root=tmp_path / "chat",
        telegram_binding=binding,
    )
    app.include_router(build_admin_api(runtime))
    return TestClient(app, client=("127.0.0.1", 54321))


async def _body(sot_factory: Any, sot_pg_url: str, tmp_path: Any) -> None:
    runtime = make_runtime(sot_pg_url, tmp_path / "tg-api")
    try:
        account, raw = await seed_user_credentials(runtime)
        binding = TelegramBindingService(sot_factory)
        client = _build_app(runtime, binding, tmp_path)

        # ── 用户面 ──
        assert (
            client.post(
                "/api/telegram/binding-codes",
                json={},
                headers={"origin": DEV_ORIGIN},
            ).status_code
            == 401
        )
        _status, _body_json, cookie = user_exchange(client, raw)
        issued = client.post(
            "/api/telegram/binding-codes",
            json={"note": "n"},
            headers={"cookie": cookie, "origin": DEV_ORIGIN},
        )
        if issued.status_code != 201:
            print("ISSUE RESP:", issued.status_code, issued.text[:300])
        assert issued.status_code == 201
        code = issued.json()["code"]
        assert code and code.count("-") == 3
        assert "digest" not in issued.json()

        status = client.get("/api/telegram/binding", headers={"cookie": cookie})
        assert status.status_code == 200
        assert status.json() == {
            "bound": False,
            "telegram_user_id": None,
            "bound_via": None,
            "bound_at": None,
            "open_codes": 1,
        }

        # 在途码上限 5：第 6 张 → 429。
        for _ in range(4):
            assert client.post(
                "/api/telegram/binding-codes",
                json={},
                headers={"cookie": cookie, "origin": DEV_ORIGIN},
            ).status_code == 201
        over = client.post(
            "/api/telegram/binding-codes",
            json={},
            headers={"cookie": cookie, "origin": DEV_ORIGIN},
        )
        assert over.status_code == 429

        # 已绑定账号再签发 → 409（先经 admin 预绑定本账号制造冲突态）。
        admin_session, admin_cookies = await admin_authed(client, runtime)
        admin_cookie = jar(nexus_admin=admin_cookies["nexus_admin"])
        csrf = runtime.auth.csrf_token(admin_session["session_id"])
        prebind = client.post(
            "/api/admin/telegram-bindings",
            json={
                "account_id": str(account["id"]),
                "telegram_user_id": "10001",
                "telegram_chat_id": "10001",
                "note": "预绑定",
            },
            headers={
                "cookie": admin_cookie,
                "origin": DEV_ORIGIN,
                "x-csrf-token": csrf,
            },
        )
        assert prebind.status_code == 201
        assert prebind.json()["binding"]["bound_via"] == "admin"
        again = client.post(
            "/api/telegram/binding-codes",
            json={},
            headers={"cookie": cookie, "origin": DEV_ORIGIN},
        )
        assert again.status_code == 409

        # ── 管理面 ──
        # 无 CSRF 的 mutation → 403；非 admin cookie → 401。
        no_csrf = client.post(
            "/api/admin/telegram-bindings",
            json={"account_id": str(account["id"]), "telegram_user_id": "2", "telegram_chat_id": "2"},
            headers={"cookie": admin_cookie, "origin": DEV_ORIGIN},
        )
        assert no_csrf.status_code == 403
        user_admin = client.get("/api/admin/telegram-bindings", headers={"cookie": cookie})
        assert user_admin.status_code == 401

        # 唯一冲突：同账号再绑另一身份 → 409。
        conflict = client.post(
            "/api/admin/telegram-bindings",
            json={
                "account_id": str(account["id"]),
                "telegram_user_id": "10002",
                "telegram_chat_id": "10002",
            },
            headers={
                "cookie": admin_cookie,
                "origin": DEV_ORIGIN,
                "x-csrf-token": csrf,
            },
        )
        assert conflict.status_code == 409

        listed = client.get(
            "/api/admin/telegram-bindings?active_only=true",
            headers={"cookie": admin_cookie},
        )
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 1 and items[0]["telegram_user_id"] == "10001"
        binding_id = items[0]["id"]

        # 解绑（幂等）→ 404 语义。
        unbind = client.post(
            f"/api/admin/telegram-bindings/{binding_id}/unbind",
            headers={"cookie": admin_cookie, "origin": DEV_ORIGIN, "x-csrf-token": csrf},
        )
        assert unbind.status_code == 200 and unbind.json()["binding"]["status"] == "unbound"
        unbind_again = client.post(
            f"/api/admin/telegram-bindings/{binding_id}/unbind",
            headers={"cookie": admin_cookie, "origin": DEV_ORIGIN, "x-csrf-token": csrf},
        )
        assert unbind_again.status_code == 200
        missing = client.post(
            f"/api/admin/telegram-bindings/{uuid.uuid4()}/unbind",
            headers={"cookie": admin_cookie, "origin": DEV_ORIGIN, "x-csrf-token": csrf},
        )
        assert missing.status_code == 404

        # 绑定服务未装配（开关关闭）→ 404，不泄露能力存在性。
        runtime.telegram_binding = None
        bare = _build_app(runtime, None, tmp_path)
        assert bare.get("/api/admin/telegram-bindings", headers={"cookie": admin_cookie}).status_code == 404
    finally:
        try:
            await runtime.aclose()
        except RuntimeError:
            pass


def test_binding_http_endpoints(
    sot_factory: Any, sot_pg_url: str, sot_reset: Any, tmp_path: Any
) -> None:
    sot_reset()
    asyncio.run(_body(sot_factory, sot_pg_url, tmp_path))
