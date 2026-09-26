"""C5 会话 timeout 过期边界负向测试（design ADR-7，PILOT_ROADMAP §5.9.3）。

补齐 task-05 验收项「idle/absolute 过期边界」：配置冻结值本身由
``test_auth_config.py`` 断言，本文件断言**边界真的会拒绝**，分四层：

1. idle 边界两侧（repo 层）：把会话行的 ``last_seen_at`` 回拨到
   ``idle_timeout_s`` 边界内侧 60s / 外侧 60s，断言内侧通过、外侧
   :class:`SessionInvalidError`（即判定确为 ``now - last_seen_at >
   idle_timeout_s`` 的严格边界，而非任意回拨都会失败）；
2. absolute 优先于 idle：``last_seen_at`` 刚更新过，``expires_at`` 过期照样拒绝；
3. 真实时钟（等待式）：``idle_timeout_s=1`` 的短超时会话真实 sleep 后失效；
4. HTTP 层：过期会话在 ``/api/auth/me``、``/api/admin/test-accounts`` 上是
   401 ``authentication required``（统一措辞，非 403/500）。

回拨时间戳统一由 Python 侧 ``datetime.now(UTC)`` 计算后作为参数写入，不使用
SQL ``now()``：测试经 tunnel 打远端 PG 时，被测时钟（应用进程）与断言时钟同源，
不依赖两端主机时钟同步。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bootstrap.auth import build_admin_api, build_auth_api
from bootstrap.auth.crypto import PepperProvider, digest_value
from bootstrap.auth.service import cookie_name
from bootstrap.db.repository.auth_repo import (
    CredentialRepository,
    SessionInvalidError,
)

pytestmark = pytest.mark.postgres


def _backdate(
    pg_url: str,
    session_id: uuid.UUID,
    *,
    last_seen_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> None:
    """直接回拨会话行时间列（受控时钟）；断言恰好命中一行。"""
    assignments: list[str] = []
    params: list[object] = []
    if last_seen_at is not None:
        assignments.append("last_seen_at = %s")
        params.append(last_seen_at)
    if expires_at is not None:
        assignments.append("expires_at = %s")
        params.append(expires_at)
    assert assignments
    params.append(str(session_id))
    conn = psycopg.connect(pg_url, autocommit=True)
    try:
        cur = conn.execute(
            f"UPDATE auth_sessions SET {', '.join(assignments)} WHERE id = %s::uuid",
            params,
        )
        assert cur.rowcount == 1, "回拨未命中会话行"
    finally:
        conn.close()


def _cookie_header(admin: bool, raw_session: str, secure: bool) -> dict[str, str]:
    return {"cookie": f"{cookie_name(admin, secure=secure)}={raw_session}"}


async def test_idle_boundary_inside_passes_outside_rejected(
    c5_runtime, c5_active_account, c5_pg_url
):
    """idle 边界：``idle_timeout_s`` 内侧 60s 通过，外侧 60s → 401 语义。"""
    _, raw = await c5_active_account("IdleBoundary")
    session, raw_session = await c5_runtime.auth.exchange_invitation(raw)
    idle = c5_runtime.auth.config.session_idle_s

    _backdate(
        c5_pg_url,
        session["id"],
        last_seen_at=datetime.now(UTC) - timedelta(seconds=idle - 60),
    )
    touched = await c5_runtime.auth.validate_user_session(raw_session)
    assert touched["id"] == session["id"]

    _backdate(
        c5_pg_url,
        session["id"],
        last_seen_at=datetime.now(UTC) - timedelta(seconds=idle + 60),
    )
    with pytest.raises(SessionInvalidError):
        await c5_runtime.auth.validate_user_session(raw_session)


async def test_absolute_expiry_rejects_even_with_recent_activity(
    c5_runtime, c5_active_account, c5_pg_url
):
    """absolute 过期优先于 idle：``last_seen_at`` 刚更新过也照样拒绝。"""
    _, raw = await c5_active_account("AbsBoundary")
    session, raw_session = await c5_runtime.auth.exchange_invitation(raw)

    _backdate(
        c5_pg_url,
        session["id"],
        last_seen_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(SessionInvalidError):
        await c5_runtime.auth.validate_user_session(raw_session)

    # 反向对照：仅把 expires_at 挪回内侧即可重新通过——证明上一条失败的原因是
    # absolute 过期，而非行被改坏或 idle 判定被误触发。
    _backdate(
        c5_pg_url,
        session["id"],
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    assert (await c5_runtime.auth.validate_user_session(raw_session))["id"] == session["id"]


async def test_admin_session_idle_and_absolute_boundaries(
    c5_runtime, c5_reset, c5_pg_url
):
    """admin 会话同一边界语义（idle 30min / absolute 12h 配置源）。"""
    c5_reset()
    raw_token = await c5_runtime.admin.bootstrap()
    session, raw_session = await c5_runtime.admin.exchange_recovery(raw_token)
    idle = c5_runtime.auth.config.admin_idle_s

    _backdate(
        c5_pg_url,
        session["id"],
        last_seen_at=datetime.now(UTC) - timedelta(seconds=idle - 60),
    )
    assert (
        await c5_runtime.admin.validate_admin_session(raw_session)
    )["principal_type"] == "admin"

    _backdate(
        c5_pg_url,
        session["id"],
        last_seen_at=datetime.now(UTC) - timedelta(seconds=idle + 60),
    )
    with pytest.raises(SessionInvalidError):
        await c5_runtime.admin.validate_admin_session(raw_session)

    _backdate(
        c5_pg_url,
        session["id"],
        last_seen_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(SessionInvalidError):
        await c5_runtime.admin.validate_admin_session(raw_session)


async def test_real_clock_idle_expiry_after_short_timeout(
    c5_runtime, c5_workspace, c5_active_account
):
    """等待式断言：``idle_timeout_s=1`` 的会话真实 sleep 后失效。

    配置单位是小时/分钟（最短 admin idle 30min），无法用配置构造秒级超时，
    故直接经 repository 建行（超时值随行固化），验证真实时钟驱动同一判定。
    """
    _, raw = await c5_active_account("ShortIdle")
    pepper = PepperProvider(c5_workspace / "secrets")
    repo = CredentialRepository(c5_runtime.session_factory)
    raw_session = "ns_short_idle_probe"
    digest = digest_value(raw_session, pepper.get())

    session = await repo.consume_token_for_session(
        token_digest=digest_value(raw, pepper.get()),
        session_digest=digest,
        idle_timeout_s=1,
        absolute_timeout_s=3600,
        user_agent="timeout-test",
    )
    assert (
        await repo.validate_session(session_digest=digest, expected_principal="user")
    )["id"] == session["id"]

    await asyncio.sleep(1.3)
    with pytest.raises(SessionInvalidError):
        await repo.validate_session(session_digest=digest, expected_principal="user")


async def test_expired_session_is_401_at_http_layer(
    c5_runtime, c5_active_account, c5_pg_url
):
    """过期普通会话在用户面端点是 401 ``authentication required``（非 403/500）。"""
    app = FastAPI()
    app.include_router(build_auth_api(c5_runtime))
    client = TestClient(app)
    secure = c5_runtime.auth.config.cookie_secure

    _, raw = await c5_active_account("HttpIdle")
    session, raw_session = await c5_runtime.auth.exchange_invitation(raw)
    headers = _cookie_header(False, raw_session, secure)
    assert client.get("/api/auth/me", headers=headers).status_code == 200

    _backdate(
        c5_pg_url,
        session["id"],
        last_seen_at=datetime.now(UTC)
        - timedelta(seconds=c5_runtime.auth.config.session_idle_s + 60),
    )
    expired = client.get("/api/auth/me", headers=headers)
    assert expired.status_code == 401
    assert expired.json() == {"detail": "authentication required"}


async def test_expired_admin_session_is_401_at_http_layer(
    c5_runtime, c5_reset, c5_pg_url
):
    """过期 admin 会话在管理端点是 401（回环来源通过后仍因会话过期被拒）。"""
    c5_reset()
    app = FastAPI()
    app.include_router(build_admin_api(c5_runtime))
    # TestClient 默认来源 "testclient" 不在 admin_allow_ips，会先被 ADR-5 回环
    # 门禁挡成 403；显式指定本机来源，使本用例断言的是会话过期而非来源。
    client = TestClient(app, client=("127.0.0.1", 54321))
    secure = c5_runtime.auth.config.cookie_secure

    raw_token = await c5_runtime.admin.bootstrap()
    session, raw_session = await c5_runtime.admin.exchange_recovery(raw_token)
    headers = _cookie_header(True, raw_session, secure)
    assert client.get("/api/admin/test-accounts", headers=headers).status_code == 200

    _backdate(
        c5_pg_url,
        session["id"],
        last_seen_at=datetime.now(UTC)
        - timedelta(seconds=c5_runtime.auth.config.admin_idle_s + 60),
    )
    expired = client.get("/api/admin/test-accounts", headers=headers)
    assert expired.status_code == 401
    assert expired.json() == {"detail": "authentication required"}
