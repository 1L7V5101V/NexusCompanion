"""C5 auth/admin HTTP 契约测试（design ADR-3/4/5，PILOT_ROADMAP §5.4）。

用 FastAPI TestClient 驱动真实 ``build_auth_api``/``build_admin_api`` 与真实
AuthRuntime，在空 PG 上验证：

- 401 语义：无 session / 无效或已消费 token → ``authentication required`` /
  ``invalid credentials``（统一措辞，不区分存在性与过期，ADR-3）；
- 403 语义：suspended/revoked、CSRF/Origin 失败、admin 路由非回环来源；
- Cookie 属性：``HttpOnly``/``Path=/``/``SameSite=Lax``/无 Domain，
  dev HTTP（cookie_secure=False）不写 ``Secure`` 且 Cookie 名退化为无前缀
  （§5.9.3 冻结项）；admin/user 会话 Cookie 相互隔离；
- mutation 依序校验 Origin/Referer → ``X-CSRF-Token``（ADR-4）；
- admin 回环边界（ADR-5）：非 ``admin_allow_ips`` 的来源一律 403。

（按「暂不跑 PG」决策只写不跑；PG 可用后取消 skip 即得集成证据。）
"""

from __future__ import annotations

from http.cookies import SimpleCookie
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.config_models import AuthConfig, Config
from bootstrap.auth import build_admin_api, build_auth_api, create_auth_runtime

pytestmark = pytest.mark.postgres

DEV_ORIGIN = "http://localhost:5173"
FOREIGN_ORIGIN = "https://evil.example.com"


def make_runtime(c5_pg_url: str, workspace: Path, *, origin_allowlist=None, admin_allow_ips=None):
    """独立 AuthRuntime（同一 scratch PG）。config 参数化用于 ADR-5 回环门禁。"""
    full = Config(provider="", model="", api_key="")
    full.storage.postgres_url = c5_pg_url
    full.auth = AuthConfig(
        cookie_secure=False,
        origin_allowlist=origin_allowlist if origin_allowlist is not None else [DEV_ORIGIN],
        admin_allow_ips=admin_allow_ips if admin_allow_ips is not None else ["127.0.0.1", "::1"],
    )
    return create_auth_runtime(config=full, workspace=workspace)


def build_app(runtime) -> FastAPI:
    app = FastAPI()
    app.include_router(build_auth_api(runtime))
    app.include_router(build_admin_api(runtime))
    return app


def make_client(runtime) -> TestClient:
    return TestClient(build_app(runtime))


def parse_cookies(set_cookie: list[str]) -> dict[str, str]:
    jar = SimpleCookie("; ".join(set_cookie))
    return {m.key: m.value for m in jar.values() if m.key and m.value}


def jar(**pairs) -> str:
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


async def seed_user_credentials(runtime) -> tuple[dict, str]:
    """自建 active 账号并签发邀请 → (账号 dict, 明文邀请 token)。"""
    created = await runtime.provisioning.create_account(display_name="Seed")
    await runtime.provisioning.run_pending(max_jobs=4)
    account = await runtime.provisioning.get_account(created["account"]["id"])
    assert account["status"] == "active"
    _, raw = await runtime.auth.issue_invitation(account["id"], issued_by="test")
    return account, raw


def user_exchange(client: TestClient, raw: str) -> tuple[int, dict, str]:
    resp = client.post("/api/auth/exchange", json={"token": raw})
    return resp.status_code, resp.json(), jar(nexus_session=parse_cookies(resp.headers.get_list("set-cookie"))["nexus_session"])


async def admin_authed(client: TestClient, runtime) -> tuple[dict, dict]:
    """bootstrap + 通过 /api/admin/auth/exchange 进入 → (会话 dict, cookie jar)。"""
    raw = await runtime.admin.bootstrap()
    resp = client.post("/api/admin/auth/exchange", json={"token": raw})
    assert resp.status_code == 200
    return resp.json(), parse_cookies(resp.headers.get_list("set-cookie"))


async def test_user_exchange_cookie_attributes(c5_pg_url, tmp_path):
    """成功后 Set-Cookie 属性冻结（§5.9.3）：HttpOnly/Path=//SameSite=Lax/无 Domain；
    dev HTTP（cookie_secure=False）Cookie 名无前缀且不写 Secure。"""
    runtime = make_runtime(c5_pg_url, tmp_path / "ws-ck")
    client = make_client(runtime)
    try:
        _, raw = await seed_user_credentials(runtime)
        resp = client.post("/api/auth/exchange", json={"token": raw})
        assert resp.status_code == 200
        set_cookies = resp.headers.get_list("set-cookie")
        cookies = parse_cookies(set_cookies)
        assert set(cookies) == {"nexus_session"}  # dev HTTP 退化名（无 __Host- 前缀）
        assert cookies["nexus_session"].startswith("ns_")
        flag = ";".join(set_cookies).lower()
        assert "httponly" in flag and "samesite=lax" in flag
        assert "path=/" in flag and "domain=" not in flag
        assert "secure" not in flag
    finally:
        await runtime.aclose()


async def test_user_exchange_invalid_and_consumed_token_401(c5_pg_url, tmp_path):
    """无效/伪造 token 与已消费 token → 统一 ``invalid credentials``（ADR-2/ADR-3）。"""
    runtime = make_runtime(c5_pg_url, tmp_path / "ws-401")
    client = make_client(runtime)
    try:
        forged = client.post("/api/auth/exchange", json={"token": "nxt_inv_forged"})
        assert forged.status_code == 401
        assert forged.json() == {"detail": "invalid credentials"}

        _, raw = await seed_user_credentials(runtime)
        ok = client.post("/api/auth/exchange", json={"token": raw})
        assert ok.status_code == 200
        again = client.post("/api/auth/exchange", json={"token": raw})
        assert again.status_code == 401
        # 不泄露存在性：与「伪造 token」错误体完全一致。
        assert again.json() == forged.json()
    finally:
        await runtime.aclose()


async def test_user_me_requires_session_401_on_suspend(c5_pg_url, tmp_path):
    """无 Cookie → 401 ``authentication required``；suspend 级联撤销会话 → 原会话 401。"""
    runtime = make_runtime(c5_pg_url, tmp_path / "ws-me")
    client = make_client(runtime)
    try:
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401
        assert resp.json() == {"detail": "authentication required"}

        account, raw = await seed_user_credentials(runtime)
        status, _, cookie = user_exchange(client, raw)
        assert status == 200
        assert client.get("/api/auth/me", headers={"cookie": cookie}).status_code == 200

        # suspend 单事务撤销名下全部 session → 旧会话按 inactive 处理（401，
        # §5.3/ADR-3：不区分「不存在 vs 已撤销」）。
        await runtime.provisioning.suspend_account(account["id"], reason="admin:test")
        assert client.get("/api/auth/me", headers={"cookie": cookie}).status_code == 401
    finally:
        await runtime.aclose()


async def test_user_mutation_requires_origin_then_csrf(c5_pg_url, tmp_path):
    """logout：缺 Origin / Origin 不在白名单 / 错误 CSRF 一律 403（ADR-4）。"""
    runtime = make_runtime(c5_pg_url, tmp_path / "ws-csrf")
    client = make_client(runtime)
    try:
        _, raw = await seed_user_credentials(runtime)
        _, data, cookie = user_exchange(client, raw)
        base = {"cookie": cookie}
        assert client.post("/api/auth/logout", headers=base).status_code == 403
        evil = {**base, "origin": FOREIGN_ORIGIN, "x-csrf-token": "0" * 64}
        assert client.post("/api/auth/logout", headers=evil).status_code == 403
        wrong = {**base, "origin": DEV_ORIGIN, "x-csrf-token": "0" * 64}
        assert client.post("/api/auth/logout", headers=wrong).status_code == 403
        # Referer 兜底 + 正确 CSRF → 200（ADR-4 第 1 步）。
        csrf = runtime.auth.csrf_token(data["session_id"])
        ok = {"cookie": cookie, "referer": f"{DEV_ORIGIN}/chat", "x-csrf-token": csrf}
        assert client.post("/api/auth/logout", headers=ok).status_code == 200
    finally:
        await runtime.aclose()


async def test_admin_loopback_rejects_non_allowlisted_source(c5_pg_url, tmp_path):
    """ADR-5：admin 路由只允许 admin_allow_ips；非白名单来源即使带合法 cookie 也 403。"""
    runtime = make_runtime(
        c5_pg_url,
        tmp_path / "ws-lb",
        admin_allow_ips=["172.30.0.5"],
    )
    client = make_client(runtime)
    try:
        # 服务层 bootstrap + 兑换拿合法 admin cookie（HTTP 不暴露 bootstrap）。
        raw = await runtime.admin.bootstrap()
        _, raw_session = await runtime.admin.exchange_recovery(raw, user_agent="t")
        cookie = jar(nexus_admin=raw_session)
        listed = client.get("/api/admin/test-accounts", headers={"cookie": cookie})
        assert listed.status_code == 403
        assert listed.json() == {"detail": "forbidden"}
    finally:
        await runtime.aclose()


async def test_admin_loopback_ok_and_create_account_flow(c5_pg_url, tmp_path):
    """本机来源（127.0.0.1 ∈ 默认 admin_allow_ips）通过回环门禁并走完整创建流程。"""
    runtime = make_runtime(c5_pg_url, tmp_path / "ws-adm")
    client = make_client(runtime)
    try:
        session, cookies = await admin_authed(client, runtime)
        cookie = jar(nexus_admin=cookies["nexus_admin"])
        csrf = runtime.auth.csrf_token(session["session_id"])
        created = client.post(
            "/api/admin/test-accounts",
            json={"display_name": "Admin Made"},
            headers={"cookie": cookie, "origin": DEV_ORIGIN, "x-csrf-token": csrf},
        )
        assert created.status_code == 200
        assert created.json()["account"]["status"] in ("active", "provisioning")
        listed = client.get("/api/admin/test-accounts", headers={"cookie": cookie})
        assert listed.status_code == 200
        assert any(a["id"] == created.json()["account"]["id"] for a in listed.json()["items"])
        # admin cookie 不能用于用户面端点（Cookie 隔离）。
        assert client.get("/api/auth/me", headers={"cookie": cookie}).status_code == 401
    finally:
        await runtime.aclose()