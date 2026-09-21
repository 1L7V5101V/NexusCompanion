"""WS handshake guard 单元测试（design ADR-4；通道接线归 C4 消费）。

不依赖 PG：用 fake AuthRuntime 覆盖 Cookie + Origin 校验矩阵。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent.config_models import AuthConfig

from bootstrap.auth.service import cookie_name
from bootstrap.auth.ws_guard import (
    WSHandshakeRejected,
    check_ws_handshake,
    ws_origin,
    ws_session_cookie,
)


@dataclass
class _FakeAuthService:
    valid: dict
    """原样返回的 session dict（None 表示校验失败→SessionInvalidError）。"""
    calls: list[str] = field(default_factory=list)

    async def validate_user_session(self, raw: str) -> dict:
        self.calls.append(raw)
        if self.valid is None:
            from bootstrap.db.repository.auth_repo import SessionInvalidError

            raise SessionInvalidError("invalid")
        return self.valid

    async def validate_admin_session(self, raw: str) -> dict:
        self.calls.append(raw)
        if self.valid is None:
            from bootstrap.db.repository.auth_repo import SessionInvalidError

            raise SessionInvalidError("invalid")
        return self.valid


@dataclass
class _FakeRuntime:
    config: AuthConfig
    auth: _FakeAuthService
    admin: _FakeAuthService


def _runtime(*, origin_allowlist, valid: dict | None) -> _FakeRuntime:
    cfg = AuthConfig(cookie_secure=False, origin_allowlist=origin_allowlist)
    svc = _FakeAuthService(valid=valid)
    return _FakeRuntime(config=cfg, auth=svc, admin=svc)


ORIGIN = "https://chat.example.com"


async def test_handshake_without_cookie_rejected() -> None:
    rt = _runtime(origin_allowlist=[ORIGIN], valid={"id": "s1", "account_id": "a1"})
    with pytest.raises(WSHandshakeRejected) as exc:
        await check_ws_handshake(rt, {"origin": ORIGIN})
    assert exc.value.origin is False
    assert rt.auth.calls == []


async def test_handshake_invalid_session_rejected() -> None:
    rt = _runtime(origin_allowlist=[ORIGIN], valid=None)
    user_cookie = cookie_name(admin=False, secure=False)
    with pytest.raises(WSHandshakeRejected):
        await check_ws_handshake(
            rt, {"cookie": f"{user_cookie}=ns_abc", "origin": ORIGIN}
        )
    # 校验路径确实吃到 Cookie（失败统一不泄露原因）。
    assert rt.auth.calls == [f"ns_abc"]


async def test_handshake_origin_not_allowlisted_rejected() -> None:
    rt = _runtime(origin_allowlist=["https://other.example.com"], valid={"id": "s1"})
    user_cookie = cookie_name(admin=False, secure=False)
    with pytest.raises(WSHandshakeRejected) as exc:
        await check_ws_handshake(
            rt, {"cookie": f"{user_cookie}=ns_abc", "origin": ORIGIN}
        )
    assert exc.value.origin is True


async def test_handshake_ok_returns_session() -> None:
    rt = _runtime(origin_allowlist=[ORIGIN], valid={"id": "s1", "account_id": "a1"})
    user_cookie = cookie_name(admin=False, secure=False)
    session = await check_ws_handshake(
        rt, {"cookie": f"{user_cookie}=ns_abc", "origin": ORIGIN}
    )
    assert session["account_id"] == "a1"
    assert rt.auth.calls == ["ns_abc"]


async def test_handshake_admin_uses_admin_cookie() -> None:
    rt = _runtime(origin_allowlist=[ORIGIN], valid={"id": "ad1", "principal_type": "admin"})
    admin_cookie = cookie_name(admin=True, secure=False)
    session = await check_ws_handshake(
        rt, {"cookie": f"{admin_cookie}=ns_adm", "origin": ORIGIN}, admin=True
    )
    assert session["id"] == "ad1"


async def test_handshake_accepts_referer_origin_fallback() -> None:
    """无 Origin 头时从 Referer 解析 origin 部分（ADR-4 第 1 步）。"""
    rt = _runtime(origin_allowlist=[ORIGIN], valid={"id": "s1"})
    user_cookie = cookie_name(admin=False, secure=False)
    session = await check_ws_handshake(
        rt,
        {
            "cookie": f"{user_cookie}=ns_abc",
            "referer": f"{ORIGIN}/chat",
        },
    )
    assert session["id"] == "s1"


def test_origin_helpers() -> None:
    assert ws_origin({"origin": "https://a.example.com"}) == "https://a.example.com"
    assert ws_origin({"referer": "https://a.example.com/path?q=1"}) == "https://a.example.com"
    assert ws_origin({"origin": " https://spaces.example.com "}) == "https://spaces.example.com"
    assert ws_origin({}) == ""

    jar_user = ws_session_cookie({"cookie": "nexus_session=ns_1; nexus_admin=na_1"}, "nexus_session")
    assert jar_user == "ns_1"
    jar_admin = ws_session_cookie({"cookie": "nexus_session=ns_1; nexus_admin=na_1"}, "nexus_admin")
    assert jar_admin == "na_1"
    assert ws_session_cookie({}, "nexus_session") == ""
    assert ws_session_cookie({"cookie": "garbage==="}, "nexus_session") == ""