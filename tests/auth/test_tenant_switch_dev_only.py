"""c18 task 8.2：租户切换入口只在 dev 配置下注册（design ADR-6）。

判据是**注册与否**，不是行为：非 dev 配置下这条路径必须 404，因为生产开放它等于
允许会话改绑他人租户（`uq_canonical_conversations_tenant_id` 决定了换租户必须换会话）。
换会话这一步走的是既有 `/api/auth/exchange`，那条链路由既有 C5 用例覆盖，这里不重复。

用例不连数据库：切换入口只是「解析目标账号 → 校验状态 → 替它签发一次性邀请 Token」
的编排，桩件喂到路由注册与 admin 门禁那一层就够把 404/401 的差别钉住。
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bootstrap.auth.api import build_admin_api, resolve_dev_switch_target
from bootstrap.auth.runtime import AuthRuntime
from bootstrap.auth.service import cookie_name
from bootstrap.db.repository.auth_repo import SessionInvalidError

DEV_ORIGIN = "http://localhost:5173"

ACCOUNTS = [
    {"id": "a-1", "status": "active", "display_name": "admin1"},
    {"id": "a-2", "status": "active", "display_name": "admin2"},
]
TENANT_OF = {"t-1": "a-1", "t-2": "a-2"}


class _Provisioning:
    async def list_accounts(self) -> list[dict[str, Any]]:
        return [dict(acc) for acc in ACCOUNTS]

    async def get_account(self, account_id: Any) -> dict[str, Any] | None:
        return next((acc for acc in ACCOUNTS if acc["id"] == str(account_id)), None)


class _Canonical:
    async def get_conversation_by_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        account_id = TENANT_OF.get(tenant_id)
        if account_id is None:
            return None
        return {"account_id": account_id, "tenant_id": tenant_id}

    async def list_conversations_by_account(
        self, account_id: Any
    ) -> list[dict[str, Any]]:
        return [
            {"account_id": owner, "tenant_id": tenant_id}
            for tenant_id, owner in TENANT_OF.items()
            if owner == str(account_id)
        ]


class _Auth:
    def __init__(self) -> None:
        self.minted: list[tuple[str, str, str]] = []

    def csrf_token(self, session_id: Any) -> str:
        return "csrf-1"

    async def issue_invitation(
        self, account_id: Any, *, display_note: str = "", issued_by: str = "admin"
    ) -> tuple[dict[str, str], str]:
        self.minted.append((str(account_id), display_note, issued_by))
        return {"id": "tok-1"}, "nxs_invitation_plain_once"


class _Admin:
    async def validate_admin_session(self, raw: str) -> dict[str, str]:
        if raw != "admin-session":
            raise SessionInvalidError("no such admin session")
        return {"id": "admin-1"}


class _Config:
    cookie_secure = False
    origin_allowlist = [DEV_ORIGIN]
    admin_allow_ips = ["127.0.0.1", "::1"]
    session_absolute_s = 3600
    admin_absolute_s = 3600


class _Runtime:
    """只满足路由注册与 admin 门禁需要的表面。"""

    def __init__(self) -> None:
        self.config = _Config()
        self.provisioning = _Provisioning()
        self.canonical_repo = _Canonical()
        self.auth = _Auth()
        self.admin = _Admin()


def _client(runtime: _Runtime, *, dev_mode: bool) -> TestClient:
    app = FastAPI()
    # 桩件只喂到路由注册与门禁那一层，类型上按真 runtime 交出去。
    app.include_router(
        build_admin_api(cast("AuthRuntime", runtime), dev_mode=dev_mode)
    )
    return TestClient(app, client=("127.0.0.1", 54321))


def _admin_headers() -> dict[str, str]:
    return {
        "cookie": f"{cookie_name(admin=True, secure=False)}=admin-session",
        "origin": DEV_ORIGIN,
        "x-csrf-token": "csrf-1",
    }


def test_non_dev_config_returns_404_even_for_an_authenticated_admin() -> None:
    """非 dev 配置：带着完整 admin session + CSRF + 回环来源打过去仍然是 404。

    带上凭据是为了排除「404 是门禁挡出来的」这种误读——门禁的返回是 401/403。
    """
    client = _client(_Runtime(), dev_mode=False)

    resp = client.post(
        "/api/admin/dev/tenant-switch", json={"label": "admin2"}, headers=_admin_headers()
    )
    assert resp.status_code == 404, resp.text
    paths = [str(getattr(route, "path", "")) for route in client.app.routes]
    assert not any("tenant-switch" in path for path in paths), paths


def test_dev_config_registers_the_route_and_keeps_the_admin_gate() -> None:
    """dev 配置：路由在位，但没有 admin 凭据时依旧 401（注册 ≠ 放行）。"""
    client = _client(_Runtime(), dev_mode=True)

    anonymous = client.post(
        "/api/admin/dev/tenant-switch", json={"label": "admin2"}, headers={"origin": DEV_ORIGIN}
    )
    assert anonymous.status_code == 401, anonymous.text
    assert anonymous.status_code != 404


def test_dev_switch_mints_an_invitation_for_the_target_account() -> None:
    """label 解析到 admin2 → 替该账号签发一次性邀请 Token，换会话由调用方去做。"""
    runtime = _Runtime()
    client = _client(runtime, dev_mode=True)

    resp = client.post(
        "/api/admin/dev/tenant-switch",
        json={"label": "admin2"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["account_id"], body["tenant_id"], body["display_name"]) == (
        "a-2",
        "t-2",
        "admin2",
    )
    assert body["token"] == "nxs_invitation_plain_once"
    assert runtime.auth.minted == [
        ("a-2", "dev-tenant-switch:t-2", "admin:dev-tenant-switch")
    ]


def test_dev_switch_by_tenant_id() -> None:
    """点名 tenant_id 时不查 label，两条形同一条路。"""
    runtime = _Runtime()
    client = _client(runtime, dev_mode=True)

    resp = client.post(
        "/api/admin/dev/tenant-switch",
        json={"tenant_id": "t-1"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == "t-1"
    assert runtime.auth.minted == [
        ("a-1", "dev-tenant-switch:t-1", "admin:dev-tenant-switch")
    ]


def test_dev_switch_requires_a_target() -> None:
    client = _client(_Runtime(), dev_mode=True)

    resp = client.post("/api/admin/dev/tenant-switch", json={}, headers=_admin_headers())
    assert resp.status_code == 400, resp.text


async def test_target_resolution_fails_closed() -> None:
    """解析不出唯一目标就不签发：查不到 404，一个 label 名下多个租户 409。"""
    runtime = _Runtime()

    for kwargs, code in (
        ({"label": "ghost", "tenant_id": ""}, 404),
        ({"label": "", "tenant_id": "t-9"}, 404),
    ):
        with pytest.raises(HTTPException) as exc:
            await resolve_dev_switch_target(cast("AuthRuntime", runtime), **kwargs)
        assert exc.value.status_code == code, kwargs

    TENANT_OF["t-3"] = "a-2"
    try:
        with pytest.raises(HTTPException) as exc:
            await resolve_dev_switch_target(
                cast("AuthRuntime", runtime), label="admin2", tenant_id=""
            )
        assert exc.value.status_code == 409
    finally:
        TENANT_OF.pop("t-3", None)
