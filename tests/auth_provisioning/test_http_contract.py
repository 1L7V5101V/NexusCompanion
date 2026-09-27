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
    full = Config(provider="", model="", api_key="", system_prompt="")
    full.storage.postgres_url = c5_pg_url.replace(
        "postgresql://", "postgresql+asyncpg://"
    )
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
    # TestClient 默认 request.client.host = "testclient"，会触发 _loopback
    # （admin_allow_ips）403；显式指定本机来源使回环判定与实际部署一致。
    return TestClient(build_app(runtime), client=("127.0.0.1", 54321))


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
    # exchange 是 mutation：Origin 必须命中 allowlist（ADR-4）。
    resp = client.post(
        "/api/auth/exchange", json={"token": raw}, headers={"origin": DEV_ORIGIN}
    )
    return resp.status_code, resp.json(), jar(nexus_session=parse_cookies(resp.headers.get_list("set-cookie"))["nexus_session"])


async def admin_authed(client: TestClient, runtime) -> tuple[dict, dict]:
    """bootstrap + 通过 /api/admin/auth/exchange 进入 → (会话 dict, cookie jar)。"""
    raw = await runtime.admin.bootstrap()
    resp = client.post(
        "/api/admin/auth/exchange", json={"token": raw}, headers={"origin": DEV_ORIGIN}
    )
    assert resp.status_code == 200
    return resp.json(), parse_cookies(resp.headers.get_list("set-cookie"))


async def test_user_exchange_cookie_attributes(c5_pg_url, tmp_path):
    """成功后 Set-Cookie 属性冻结（§5.9.3）：HttpOnly/Path=//SameSite=Lax/无 Domain；
    dev HTTP（cookie_secure=False）Cookie 名无前缀且不写 Secure。"""
    runtime = make_runtime(c5_pg_url, tmp_path / "ws-ck")
    client = make_client(runtime)
    try:
        _, raw = await seed_user_credentials(runtime)
        resp = client.post(
            "/api/auth/exchange", json={"token": raw}, headers={"origin": DEV_ORIGIN}
        )
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
        forged = client.post(
            "/api/auth/exchange", json={"token": "nxt_forged"}, headers={"origin": DEV_ORIGIN}
        )
        assert forged.status_code == 401
        assert forged.json() == {"detail": "invalid credentials"}

        _, raw = await seed_user_credentials(runtime)
        ok = client.post(
            "/api/auth/exchange", json={"token": raw}, headers={"origin": DEV_ORIGIN}
        )
        assert ok.status_code == 200
        again = client.post(
            "/api/auth/exchange", json={"token": raw}, headers={"origin": DEV_ORIGIN}
        )
        assert again.status_code == 401
        # 不泄露存在性：与「伪造 token」错误体完全一致。
        assert again.json() == forged.json()
    finally:
        await runtime.aclose()


async def test_user_me_forbidden_403_on_suspend(c5_pg_url, tmp_path):
    """无 Cookie → 401；账号被 suspend 后其 Cookie 请求 → **403**（spec 场景）。

    spec「Scenario: 封禁账号请求返回 403 而非 401」：封禁级联会写 revoked_at
    （审计痕迹），但响应必须是 403（principal 有效但被禁），不是 401。
    """
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

        # suspend 单事务撤销名下全部 session（§5.3）；响应语义仍是 403。
        await runtime.provisioning.suspend_account(account["id"], reason="admin:test")
        denied = client.get("/api/auth/me", headers={"cookie": cookie})
        assert denied.status_code == 403
        assert denied.json() == {"detail": "forbidden"}
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


async def test_admin_loopback_ok_and_create_account_flow(c5_pg_url, c5_reset, tmp_path):
    """本机来源（127.0.0.1 ∈ 默认 admin_allow_ips）通过回环门禁并走完整创建流程。

    `c5_reset` 保证空基线：admin 单行 bootstrap 幂等（共享 scratch DB 中前置
    测试可能已 bootstrap，重复 bootstrap 抛 AdminBootstrapError）。
    """
    c5_reset()
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


class _AlwaysFailExecutor:
    """供失败分支注入：``provision`` 必抛错，job 落 failed。"""

    async def provision(self, *, account_id, tenant_id):
        raise RuntimeError("simulated admin-surface failure")


async def test_exchange_requires_origin_allowlist(c5_pg_url, tmp_path):
    """exchange 是 mutation：Origin/Referer 必须命中 allowlist，否则 403（ADR-4）。

    ``exchange`` 在建立会话之前没有 session，故无 session-bound CSRF 可绑定；
    但 Origin 仍适用——否则跨站可直接发起登录（login CSRF）。
    """
    runtime = make_runtime(c5_pg_url, tmp_path / "ws-origin")
    client = make_client(runtime)
    try:
        _, raw = await seed_user_credentials(runtime)

        # 缺 Origin / 外来 Origin → 403 统一文案，且不签发任何 Cookie。
        for headers in ({}, {"origin": FOREIGN_ORIGIN}):
            denied = client.post("/api/auth/exchange", json={"token": raw}, headers=headers)
            assert denied.status_code == 403
            assert denied.json() == {"detail": "forbidden"}
            assert not parse_cookies(denied.headers.get_list("set-cookie"))

        # 上面两次被拒不应消费 token：命中 allowlist 后仍可兑换成功。
        allowed = client.post(
            "/api/auth/exchange", json={"token": raw}, headers={"origin": DEV_ORIGIN}
        )
        assert allowed.status_code == 200

        # Referer 兜底同样放行（ADR-4 第 1 步）。
        _, raw2 = await seed_user_credentials(runtime)
        by_referer = client.post(
            "/api/auth/exchange",
            json={"token": raw2},
            headers={"referer": f"{DEV_ORIGIN}/chat"},
        )
        assert by_referer.status_code == 200
    finally:
        await runtime.aclose()


async def test_admin_create_account_reports_failed_job(c5_pg_url, c5_reset, tmp_path):
    """建号未收敛时 admin 端点必须回显 job 状态与失败原因，而不是 500。

    回归用例：``bootstrap/auth/api.py`` 曾调用 ``runtime.provisioning.get_job``，
    而 ``ProvisioningService`` 并无该方法（只有 repo 层有）→ 该分支 AttributeError
    （500）。该分支正是 design ADR-6「失败 → ``failed`` + ``last_error`` 对管理员
    可见」的落点，即 provisioning 真失败、管理员最需要看到原因的时刻。
    """
    c5_reset()
    runtime = make_runtime(c5_pg_url, tmp_path / "ws-jobfail")
    client = make_client(runtime)
    try:
        session, cookies = await admin_authed(client, runtime)
        cookie = jar(nexus_admin=cookies["nexus_admin"])
        csrf = runtime.auth.csrf_token(session["session_id"])

        # 注入必败 executor → job failed、账号停在 provisioning。
        runtime.provisioning._executor = _AlwaysFailExecutor()

        resp = client.post(
            "/api/admin/test-accounts",
            json={"display_name": "Will Fail"},
            headers={"cookie": cookie, "origin": DEV_ORIGIN, "x-csrf-token": csrf},
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["job"]["status"] == "failed"
        assert "simulated admin-surface failure" in payload["job"]["last_error"]
        assert payload["account"]["status"] == "provisioning"
        # 未 ready 不签发邀请 Token。
        assert "token" not in payload

        # admin exchange 同样受 Origin 约束（ADR-4），且在 token 校验之前生效。
        foreign = client.post(
            "/api/admin/auth/exchange",
            json={"token": "nad_not-a-real-token"},
            headers={"origin": FOREIGN_ORIGIN},
        )
        assert foreign.status_code == 403
        assert foreign.json() == {"detail": "forbidden"}
    finally:
        await runtime.aclose()


# ── invite-code-tenant-registration: HTTP 层注册/登录契约 ────────────────


async def _issue_tenant_invite_http(runtime, client, *, tenant_name: str) -> str:
    """admin 签发租户邀请码（不经 create-account，test_accounts 无新行）。

    每次调用复用既定 admin（bootstrap 单行幂等），避免重复 bootstrap 抛
    AdminBootstrapError（共享 scratch DB 跨用例）。
    """
    cached = getattr(runtime, "_admin_http_auth", None)
    if cached is None:
        session, cookies = await admin_authed(client, runtime)
        cached = (
            jar(nexus_admin=cookies["nexus_admin"]),
            runtime.auth.csrf_token(session["session_id"]),
        )
        object.__setattr__(runtime, "_admin_http_auth", cached)
    cookie, csrf = cached
    resp = client.post(
        "/api/admin/tenant-invites",
        json={"tenant_name": tenant_name},
        headers={"cookie": cookie, "origin": DEV_ORIGIN, "x-csrf-token": csrf},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["token"].startswith("nxt_")
    assert payload["tenant_name"] == tenant_name
    return payload["token"]


async def test_admin_tenant_invite_does_not_create_account(c5_pg_url, c5_reset, tmp_path):
    """admin 签发租户邀请码不建号（D1：签发即不触发 provisioning）。"""
    c5_reset()
    runtime = make_runtime(c5_pg_url, tmp_path / "ws-inv")
    client = make_client(runtime)
    try:
        await _issue_tenant_invite_http(runtime, client, tenant_name="Acme HTTP")
        # 校验服务层：签发路径不产生任何账号/job。
        assert await runtime.provisioning.list_accounts() == []
        assert await runtime.provisioning.list_jobs() == []
    finally:
        await runtime.aclose()


async def test_register_http_flow(c5_pg_url, c5_reset, tmp_path):
    """注册 → Set-Cookie → /me 可用 → logout → 邮箱密码登录 → /me 一致。

    覆盖 ADDED 场景「登录成功建立会话」在 HTTP 层的完整闭环。
    """
    c5_reset()
    runtime = make_runtime(c5_pg_url, tmp_path / "ws-reg")
    client = make_client(runtime)
    try:
        raw = await _issue_tenant_invite_http(runtime, client, tenant_name="HTTP Reg")

        # 注册：Origin 校验（缺 Origin → 403 且不建号）。
        no_origin = client.post(
            "/api/auth/register",
            json={
                "invite_token": raw,
                "email": "http@example.com",
                "password": "pw-123456",
            },
        )
        assert no_origin.status_code == 403
        assert await runtime.provisioning.list_accounts() == []

        ok = client.post(
            "/api/auth/register",
            json={
                "invite_token": raw,
                "email": "http@example.com",
                "password": "pw-123456",
            },
            headers={"origin": DEV_ORIGIN},
        )
        assert ok.status_code == 200, ok.text
        account_id = ok.json()["account_id"]
        cookies = parse_cookies(ok.headers.get_list("set-cookie"))
        assert set(cookies) == {"nexus_session"}
        assert cookies["nexus_session"].startswith("ns_")
        cookie = jar(nexus_session=cookies["nexus_session"])

        me = client.get("/api/auth/me", headers={"cookie": cookie})
        assert me.status_code == 200
        assert me.json()["account_id"] == account_id
        assert me.json()["display_name"] == "http"  # email 前缀

        # 重复邮箱以新邀请码注册：401 统一文案。
        raw2 = await _issue_tenant_invite_http(runtime, client, tenant_name="HTTP Reg 2")
        dup = client.post(
            "/api/auth/register",
            json={
                "invite_token": raw2,
                "email": "http@example.com",
                "password": "pw-123456",
            },
            headers={"origin": DEV_ORIGIN},
        )
        assert dup.status_code == 401
        assert dup.json() == {"detail": "invalid credentials"}

        # logout（Origin + CSRF）→ /me 401。
        csrf = runtime.auth.csrf_token(ok.json()["session_id"])
        logout = client.post(
            "/api/auth/logout",
            headers={"cookie": cookie, "origin": DEV_ORIGIN, "x-csrf-token": csrf},
        )
        assert logout.status_code == 200
        assert client.get("/api/auth/me", headers={"cookie": cookie}).status_code == 401

        # 邮箱密码登录 → /me 一致。
        login_resp = client.post(
            "/api/auth/login",
            json={"email": "http@example.com", "password": "pw-123456"},
            headers={"origin": DEV_ORIGIN},
        )
        assert login_resp.status_code == 200, login_resp.text
        login_cookies = parse_cookies(login_resp.headers.get_list("set-cookie"))
        me2 = client.get(
            "/api/auth/me", headers={"cookie": jar(nexus_session=login_cookies["nexus_session"])}
        )
        assert me2.status_code == 200
        assert me2.json()["account_id"] == account_id
    finally:
        await runtime.aclose()


async def test_login_and_register_error_semantics_uniform(c5_pg_url, c5_reset, tmp_path):
    """登录/注册失败响应统一：401 ``invalid credentials``，不区分原因。

    邮箱不存在 / 密码错 / 账号被 suspend + 注册邀请码无效 → 完全一致 body。
    """
    c5_reset()
    runtime = make_runtime(c5_pg_url, tmp_path / "ws-err")
    client = make_client(runtime)
    try:
        raw = await _issue_tenant_invite_http(runtime, client, tenant_name="Err")
        ok = client.post(
            "/api/auth/register",
            json={"invite_token": raw, "email": "u@example.com", "password": "pw-123456"},
            headers={"origin": DEV_ORIGIN},
        )
        assert ok.status_code == 200
        account_id = ok.json()["account_id"]
        await runtime.provisioning.suspend_account(account_id, reason="t")

        login_headers = {"origin": DEV_ORIGIN}
        responses = [
            client.post(
                "/api/auth/login",
                json={"email": "missing@example.com", "password": "pw-123456"},
                headers=login_headers,
            ),  # 邮箱不存在
            client.post(
                "/api/auth/login",
                json={"email": "u@example.com", "password": "wrong-pass"},
                headers=login_headers,
            ),  # 密码错
            client.post(
                "/api/auth/login",
                json={"email": "u@example.com", "password": "pw-123456"},
                headers=login_headers,
            ),  # 已 suspended
            client.post(
                "/api/auth/register",
                json={
                    "invite_token": "nxt_forged-token",
                    "email": "who@example.com",
                    "password": "pw-123456",
                },
                headers=login_headers,
            ),  # 无效邀请码
        ]
        for resp in responses:
            assert resp.status_code == 401
            assert resp.json() == {"detail": "invalid credentials"}
    finally:
        await runtime.aclose()


async def test_register_requires_origin_allowlist(c5_pg_url, c5_reset, tmp_path):
    """register/login 是 mutation：Origin 不命中 allowlist → 403 且不消费邀请码。"""
    c5_reset()
    runtime = make_runtime(c5_pg_url, tmp_path / "ws-reg-origin")
    client = make_client(runtime)
    try:
        raw = await _issue_tenant_invite_http(runtime, client, tenant_name="Origin")
        foreign = client.post(
            "/api/auth/register",
            json={"invite_token": raw, "email": "o@example.com", "password": "pw-123456"},
            headers={"origin": FOREIGN_ORIGIN},
        )
        assert foreign.status_code == 403
        # 未消费（虽然前端不会自动重试，但 token 必须保持可用）。
        ok = client.post(
            "/api/auth/register",
            json={"invite_token": raw, "email": "o@example.com", "password": "pw-123456"},
            headers={"origin": DEV_ORIGIN},
        )
        assert ok.status_code == 200
    finally:
        await runtime.aclose()
