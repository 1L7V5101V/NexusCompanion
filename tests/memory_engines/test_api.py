"""C14 HTTP 端点验收（task 3.1/3.2）。

GET/PUT /api/memory/engines(/active)：认证门禁 401、目录只读（inspector 不出现/
客户端字段无授权效果）、未授权切换三连（目录外 404 / not ready 409 / 未放行
403）、同 engine 幂等不提升、切换成功持久化 + revision 提升、未装配 404。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bootstrap.chat_api import create_chat_app
from bootstrap.memory_binding import TenantMemoryEngineBindingService
from tests.auth_provisioning.test_http_contract import (
    DEV_ORIGIN,
    make_runtime,
    seed_user_credentials,
    user_exchange,
)

pytestmark = pytest.mark.postgres


def _build_app(
    runtime: Any,
    engines_service: Any,
    tmp_path: Any,
) -> TestClient:
    app = create_chat_app(
        workspace=tmp_path,
        channel=SimpleNamespace(name="chat", _require_ctx=lambda: None),
        auth_runtime=runtime,
        static_root=tmp_path / "chat",
        memory_engines=engines_service,
    )
    return TestClient(app, client=("127.0.0.1", 54321))


def _authed_client(client: TestClient, raw: str) -> str:
    _status, _body, cookie = user_exchange(client, raw)
    return cookie


async def _run_scenarios(factory: Any, pg_url: str, tmp_path: Any, *, ready: tuple[str, ...]) -> None:
    runtime = make_runtime(pg_url, tmp_path / "me-api")
    try:
        _account, raw = await seed_user_credentials(runtime)
        service = TenantMemoryEngineBindingService(
            factory, ready_engines=ready, user_selection_allowed=True
        )
        client = _build_app(runtime, service, tmp_path)

        # ── 认证门禁 ──
        assert client.get("/api/memory/engines").status_code == 401
        assert (
            client.put(
                "/api/memory/engines/active",
                json={"engine_id": "rachael"},
                headers={"origin": DEV_ORIGIN},
            ).status_code
            == 401
        )
        cookie = _authed_client(client, raw)

        # ── 验收 1/8：目录来自服务端（只含 default/rachael，inspector 不出现）──
        status = client.get("/api/memory/engines", headers={"cookie": cookie})
        assert status.status_code == 200
        body = status.json()
        assert {e["engine_id"] for e in body["engines"]} == {"default", "rachael"}
        assert all("inspector" not in e["engine_id"] for e in body["engines"])
        assert body["active_engine"] == "default"  # 初始绑定 default
        by_id = {e["engine_id"]: e for e in body["engines"]}
        assert by_id["default"]["active"] is True
        if "rachael" in ready:
            assert by_id["rachael"]["ready"] is True
            assert by_id["rachael"]["selectable"] is True
        else:
            assert by_id["rachael"]["ready"] is False
            assert by_id["rachael"]["selectable"] is False

        # ── 验收 1：客户端字段只是设置请求（额外字段无授权效果）──
        smuggled = client.put(
            "/api/memory/engines/active",
            json={
                "engine_id": "ghost-engine",
                "binding_policy": "required",
                "capabilities": ["admin"],
                "ready": True,
            },
            headers={"cookie": cookie, "origin": DEV_ORIGIN},
        )
        assert smuggled.status_code == 404
        assert smuggled.json()["detail"] == "unknown_engine"

        # ── 验收 2：not ready（rachael 未构建）→ 409 ──
        if "rachael" not in ready:
            not_ready = client.put(
                "/api/memory/engines/active",
                json={"engine_id": "rachael"},
                headers={"cookie": cookie, "origin": DEV_ORIGIN},
            )
            assert not_ready.status_code == 409
            assert not_ready.json()["detail"] == "engine_not_ready"

        # ── 切换成功：revision +1，GET 反映；同 engine 幂等不提升 ──
        if "rachael" in ready:
            switched = client.put(
                "/api/memory/engines/active",
                json={"engine_id": "rachael"},
                headers={"cookie": cookie, "origin": DEV_ORIGIN},
            )
            assert switched.status_code == 200
            assert switched.json() == {
                "active_engine": "rachael",
                "tenant_policy_revision": 1,
            }
            again = client.put(
                "/api/memory/engines/active",
                json={"engine_id": "rachael"},
                headers={"cookie": cookie, "origin": DEV_ORIGIN},
            )
            assert again.status_code == 200
            assert again.json()["tenant_policy_revision"] == 1  # 幂等不提升
            after = client.get("/api/memory/engines", headers={"cookie": cookie})
            assert after.json()["active_engine"] == "rachael"
            assert after.json()["tenant_policy_revision"] == 1
    finally:
        try:
            await runtime.aclose()
        except RuntimeError:
            pass


async def _run_selectable_off(pg_url: str, tmp_path: Any) -> None:
    runtime = make_runtime(pg_url, tmp_path / "me-api-locked")
    try:
        _account, raw = await seed_user_credentials(runtime)
        service = TenantMemoryEngineBindingService(
            runtime.session_factory,
            ready_engines=("default", "rachael"),
            user_selection_allowed=False,
        )
        client = _build_app(runtime, service, tmp_path)
        cookie = _authed_client(client, raw)
        # 验收 2：用户侧切换关闭 → 403（GET 只读仍可用）。
        assert (
            client.get("/api/memory/engines", headers={"cookie": cookie}).status_code
            == 200
        )
        denied = client.put(
            "/api/memory/engines/active",
            json={"engine_id": "rachael"},
            headers={"cookie": cookie, "origin": DEV_ORIGIN},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "engine_not_selectable"
    finally:
        try:
            await runtime.aclose()
        except RuntimeError:
            pass


async def _run_unmounted(pg_url: str, tmp_path: Any) -> None:
    runtime = make_runtime(pg_url, tmp_path / "me-api-bare")
    try:
        _account, raw = await seed_user_credentials(runtime)
        bare = _build_app(runtime, None, tmp_path)
        cookie = _authed_client(bare, raw)
        # 未装配 → 404 不泄露能力存在性（验收 1 前提：dev 路径端点不存在）。
        assert bare.get("/api/memory/engines", headers={"cookie": cookie}).status_code == 404
        assert (
            bare.put(
                "/api/memory/engines/active",
                json={"engine_id": "default"},
                headers={"cookie": cookie, "origin": DEV_ORIGIN},
            ).status_code
            == 404
        )
    finally:
        try:
            await runtime.aclose()
        except RuntimeError:
            pass


def test_memory_engines_api_full_ready(
    me_factory: Any, me_pg_url: str, tmp_path: Any
) -> None:
    asyncio.run(
        _run_scenarios(me_factory, me_pg_url, tmp_path, ready=("default", "rachael"))
    )


def test_memory_engines_api_rachael_not_ready(
    me_factory: Any, me_pg_url: str, tmp_path: Any
) -> None:
    asyncio.run(_run_scenarios(me_factory, me_pg_url, tmp_path, ready=("default",)))


def test_memory_engines_api_selection_locked(me_pg_url: str, tmp_path: Any) -> None:
    asyncio.run(_run_selectable_off(me_pg_url, tmp_path))


def test_memory_engines_api_unmounted(me_pg_url: str, tmp_path: Any) -> None:
    asyncio.run(_run_unmounted(me_pg_url, tmp_path))
