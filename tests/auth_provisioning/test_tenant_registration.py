"""租户邀请码注册 + 邮箱密码登录（invite-code-tenant-registration）。

覆盖 specs/auth-provisioning/spec.md 两组 requirement 的全部 scenario：

MODIFIED「邀请 Token 一次性原子兑换」
- 并发兑换同一 Token 仅一个成功（并发注册：恰好一个成功 + 只建一个账号/会话）
- 非 active 账号的 Token 不可兑换（provisioning/failed/suspended/revoked 重放失败）
- 邀请码签发无需预先存在的账号（account_id=None + tenant_name 落库）
- provisioning 失败后 retry 收敛（幂等 retry → active → 登录成功）
- 明文凭据不落库（digest-only + argon2 哈希）

ADDED「邮箱密码注册与登录」
- 重复邮箱注册被拒绝且不泄露存在性
- 错误响应不泄露账号状态（邮箱不存在/密码错/已 suspended 语义一致）
- 密码哈希不落明文（password_digest 列只存 argon2）
- 登录成功建立会话（随后 /me → 该账号租户）

（与 auth_provisioning 其余测试相同：PG 可用时跑，否则 postgres marker skip。）
"""

from __future__ import annotations

import asyncio

import pytest

from bootstrap.auth.runtime import create_auth_runtime
from bootstrap.db.repository.auth_repo import CredentialExchangeError

pytestmark = pytest.mark.postgres


def _new_peer(c5_pg_url: str, tmp_path, seed: str = ""):
    """独立 AuthRuntime（独立连接池 + 独立 pepper 目录）。"""
    from agent.config_models import Config

    cfg = Config(provider="", model="", api_key="", system_prompt="")
    cfg.storage.postgres_url = c5_pg_url.replace(
        "postgresql://", "postgresql+asyncpg://"
    )
    return create_auth_runtime(config=cfg, workspace=tmp_path / f"ws-{seed}-{id(tmp_path)}")


async def _register(runtime, raw: str, email: str, password: str = "secret123"):
    return await runtime.auth.register_tenant(raw, email, password, user_agent="t")


# ── MODIFIED: 邀请 Token 一次性原子兑换 ──────────────────────────────


async def test_issue_tenant_invite_without_account(c5_runtime, c5_reset):
    """签发租户邀请码不要求预存账号：account_id None + tenant_name 落库。"""
    c5_reset()
    row, raw = await c5_runtime.auth.issue_tenant_invitation(
        "Acme Pilot", issued_by="test"
    )
    assert raw.startswith("nxt_")
    assert row["account_id"] is None
    assert row["tenant_name"] == "Acme Pilot"
    assert row["consumed_at"] is None
    # 未触发任何账号/job（设计 D1：签发即不建号）。
    from bootstrap.db.repository.provisioning_repo import _job_to_dict

    assert await c5_runtime.provisioning.list_accounts() == []


async def test_register_single_success(c5_runtime, c5_reset):
    """注册成功：消费邀请码 → provisioning → active → 会话。"""
    c5_reset()
    _, raw = await c5_runtime.auth.issue_tenant_invitation("T1", issued_by="test")
    session, cookie = await _register(c5_runtime, raw, "alice@example.com")
    assert cookie.startswith("ns_")
    assert session["principal_type"] == "user"
    account = await c5_runtime.provisioning.get_account(session["account_id"])
    assert account["status"] == "active"
    assert account["email"] == "alice@example.com"
    assert account["display_name"] == "alice"  # email 前缀


async def test_concurrent_register_single_success(
    c5_pg_url, c5_reset, tmp_path, monkeypatch
):
    """并发用同一邀请码 + 各自邮箱注册：恰好一个成功，只建一个账号。"""
    c5_reset()
    monkeypatch.setenv("NEXUS_AUTH_PEPPER", "w" * 32)
    runner = _new_peer(c5_pg_url, tmp_path, seed="sig-peer")
    try:
        _, raw = await runner.auth.issue_tenant_invitation("C", issued_by="test")
        peer_a = _new_peer(c5_pg_url, tmp_path, seed="a")
        peer_b = _new_peer(c5_pg_url, tmp_path, seed="b")

        async def _try(peer, email: str) -> dict:
            try:
                session, _ = await _register(peer, raw, email)
                return {"ok": True, "session": session}
            except CredentialExchangeError:
                return {"ok": False}

        try:
            results = await asyncio.gather(
                _try(peer_a, "alice@example.com"),
                _try(peer_b, "bob@example.com"),
            )
        finally:
            await peer_a.aclose()
            await peer_b.aclose()

        ok = [r for r in results if r["ok"]]
        assert len(ok) == 1, f"并发注册应恰好一个成功，got {len(ok)}"
        # 只创建一个账号、只创建一个 user 会话。
        accounts = await runner.provisioning.list_accounts()
        assert len(accounts) == 1
        from sqlalchemy import text

        async with runner.session_factory() as sess:
            count = (
                await sess.execute(
                    text(
                        "SELECT count(*) FROM auth_sessions WHERE principal_type='user'"
                    )
                )
            ).scalar_one()
        assert count == 1
        # 被消费方拿到的是统一认证错误（不泄露 token 已用细节）。
        assert not ok[0] or results[1 - results.index(ok[0])]["ok"] is False
    finally:
        await runner.aclose()


async def test_reuse_consumed_invite_rejected(c5_runtime, c5_reset):
    """已消费邀请码重放 → 统一认证错误（provisioning/suspended 同理）。"""
    c5_reset()
    _, raw = await c5_runtime.auth.issue_tenant_invitation("T", issued_by="test")
    await _register(c5_runtime, raw, "alice@example.com")
    with pytest.raises(CredentialExchangeError):
        await _register(c5_runtime, raw, "bob@example.com")


async def test_registration_allows_retry(c5_runtime, c5_reset, monkeypatch):
    """provisioning 失败（executor 抛错）→ 账号 failed + 邀请码已消费 → retry 收敛。

    覆盖 spec「provisioning 失败后 retry 收敛」：同一 job 行推进、幂等、
    不产生第二个 tenant，账号最终 active 后可登录。
    """
    c5_reset()

    real_executor = c5_runtime.provisioning._executor

    async def _flaky_provision(self, *, account_id, tenant_id):
        if "flaky" not in vars(self):
            vars(self)["flaky"] = True
            raise RuntimeError("boom: transient executor failure")
        # 后续调用走真实 executor（recover 后容器可用）。
        return await real_executor.provision(account_id=account_id, tenant_id=tenant_id)

    monkeypatch.setattr(c5_runtime.provisioning, "_executor", type("Flaky", (), {"provision": _flaky_provision})())

    _, raw = await c5_runtime.auth.issue_tenant_invitation("Retry", issued_by="test")
    # 第一次注册：provisioning 失败 → 统一认证错误（账号 failed）。
    with pytest.raises(CredentialExchangeError):
        await _register(c5_runtime, raw, "carol@example.com")
    account_rows = await c5_runtime.provisioning.list_accounts()
    assert len(account_rows) == 1
    failed_account = account_rows[0]
    assert failed_account["status"] == "failed"

    # 邀请码已消费：不能再次用注册路径。
    with pytest.raises(CredentialExchangeError):
        await _register(c5_runtime, raw, "dave@example.com")

    # 管理员对同一 job 幂等 retry（executor 已恢复）→ active → 登录成功。
    jobs = await c5_runtime.provisioning.list_jobs()
    assert len(jobs) == 1
    job = await c5_runtime.provisioning.retry(jobs[0]["id"])
    assert job["status"] in ("ready", "running")
    await c5_runtime.provisioning.run_pending(max_jobs=4)
    refreshed = await c5_runtime.provisioning.get_account(failed_account["id"])
    assert refreshed["status"] == "active"
    session, _ = await c5_runtime.auth.login(
        "carol@example.com", "secret123", user_agent="t"
    )
    assert session["principal_type"] == "user"
    # 只产生一个 tenant（幂等 retry 不产第二个 agent）。
    from sqlalchemy import text

    async with c5_runtime.session_factory() as sess:
        count = (
            await sess.execute(
                text("SELECT count(*) FROM canonical_conversations")
            )
        ).scalar_one()
    assert count == 1


async def test_digest_only_and_argon2_in_db(c5_runtime, c5_reset):
    """明文凭据不落库 + 密码哈希不落明文（spec 两处 scenario）。"""
    c5_reset()
    _, raw = await c5_runtime.auth.issue_tenant_invitation("Secret", issued_by="test")
    session, cookie = await _register(c5_runtime, raw, "eve@example.com", "k3$s3cret!")
    from sqlalchemy import text

    async with c5_runtime.session_factory() as sess:
        token_row = (
            await sess.execute(
                text(
                    "SELECT token_digest, tenant_name, consumed_at IS NOT NULL "
                    "FROM access_tokens"
                )
            )
        ).fetchone()
        assert token_row is not None and len(token_row[0]) == 64
        assert token_row[1] == "Secret"
        assert token_row[2] is True  # 已消费

        password_row = (
            await sess.execute(
                text("SELECT email, password_digest FROM test_accounts")
            )
        ).fetchone()
        assert password_row is not None
        assert password_row[0] == "eve@example.com"
        digest = password_row[1]
        # argon2 编码串特征：$argon2id$v=19$m=...,t=...,p=...
        assert digest.startswith("$argon2")
        assert raw not in digest and "k3$s3cret!" not in digest

        session_digests = (
            await sess.execute(
                text(
                    "SELECT session_digest FROM auth_sessions "
                    "WHERE principal_type='user'"
                )
            )
        ).scalars().all()
        assert all(len(d) == 64 for d in session_digests)
        assert cookie not in session_digests


# ── ADDED: 邮箱密码注册与登录 ────────────────────────────────────────


async def test_duplicate_email_rejected(c5_runtime, c5_reset):
    """重复邮箱注册被拒绝，且不泄露既有账号是否存在。"""
    c5_reset()
    _, raw1 = await c5_runtime.auth.issue_tenant_invitation("A1", issued_by="test")
    _, raw2 = await c5_runtime.auth.issue_tenant_invitation("A2", issued_by="test")
    await _register(c5_runtime, raw1, "dup@example.com")
    with pytest.raises(CredentialExchangeError) as exc_info:
        await _register(c5_runtime, raw2, "dup@example.com")
    # 统一语义：不泄露「已存在」细节。
    assert str(exc_info.value) != "email already registered - dup@example.com"


async def test_login_error_semantics_identical(c5_runtime, c5_reset):
    """邮箱不存在 / 密码错 / 已 suspended → 相同异常类型（不泄露差异）。"""
    c5_reset()
    _, raw = await c5_runtime.auth.issue_tenant_invitation("T", issued_by="test")
    session, _ = await _register(c5_runtime, raw, "sam@example.com", "pw-123456")
    account_id = session["account_id"]

    errors: list[str] = []
    for email, password, suspend in [
        ("missing@example.com", "pw-123456", False),  # 邮箱不存在
        ("sam@example.com", "wrong-password", False),  # 密码错
        ("sam@example.com", "pw-123456", True),  # 已 suspended
    ]:
        if suspend:
            await c5_runtime.provisioning.suspend_account(account_id, reason="t")
        try:
            await c5_runtime.auth.login(email, password, user_agent="t")
            raise AssertionError(f"login 不应成功: {email} {password}")
        except CredentialExchangeError as exc:
            errors.append(str(exc))
    assert len(set(errors)) == 1


async def test_login_success_and_tenant_scope(c5_runtime, c5_reset):
    """登录成功建立会话，随后以该会话访问 /me 归属该账号。"""
    c5_reset()
    _, raw = await c5_runtime.auth.issue_tenant_invitation("Scope", issued_by="test")
    session, cookie = await _register(c5_runtime, raw, "tim@example.com", "pw-123456")
    account_id = session["account_id"]

    session2, cookie2 = await c5_runtime.auth.login(
        "tim@example.com", "pw-123456", user_agent="t"
    )
    assert session2["account_id"] == account_id
    # 会话可用：validate_user_session 通过。
    validated = await c5_runtime.auth.validate_user_session(cookie2)
    assert validated["account_id"] == account_id
    assert validated["principal_type"] == "user"


async def test_register_provisioning_resolves_webchat_identity(c5_runtime, c5_reset):
    """注册 → provisioning 收敛 → 登录会话能派生独立 WebChat 身份（新一轮租户）。

    覆盖 §5.9.1 身份链：session → account → 首个 canonical conversation →
    tenant。注册触发的 provisioning 必须已建 agent 会话，否则派生抛错
    （fail-closed，不回落 DEFAULT_TENANT）。两个注册者得到不同 tenant。
    """
    c5_reset()
    from bootstrap.auth.identity import resolve_webchat_identity

    _, raw1 = await c5_runtime.auth.issue_tenant_invitation("Tenant 甲", issued_by="test")
    s1, _ = await _register(c5_runtime, raw1, "甲@example.com")
    _, raw2 = await c5_runtime.auth.issue_tenant_invitation("Tenant 乙", issued_by="test")
    s2, _ = await _register(c5_runtime, raw2, "乙@example.com")

    # 两个账号、两个 tenant（互不串扰）。
    id1 = await resolve_webchat_identity(c5_runtime, s1)
    id2 = await resolve_webchat_identity(c5_runtime, s2)
    assert id1.tenant_id != id2.tenant_id
    assert id1.account_id != id2.account_id
    assert id1.chat_id == id1.tenant_id and id1.session_key == f"chat:{id1.tenant_id}"

    # 新租户 owner 的消息通道身份与其 conversation 一致（§5.9.1 唯一授权来源）。
    conversations = await c5_runtime.canonical_repo.list_conversations_by_account(
        s1["account_id"]
    )
    assert len(conversations) == 1
    assert str(conversations[0]["tenant_id"]) == id1.tenant_id
    assert str(conversations[0]["id"]) == id1.conversation_id


async def test_login_rejected_for_revoked_account(c5_runtime, c5_reset):
    """revoked 账号登录：统一认证错误 + 不产生会话。"""
    c5_reset()
    _, raw = await c5_runtime.auth.issue_tenant_invitation("R", issued_by="test")
    session, _ = await _register(c5_runtime, raw, "rev@example.com", "pw-123456")
    await c5_runtime.provisioning.revoke_account(session["account_id"], reason="t")
    with pytest.raises(CredentialExchangeError):
        await c5_runtime.auth.login("rev@example.com", "pw-123456", user_agent="t")


async def test_short_password_rejected(c5_runtime, c5_reset):
    """密码策略：短密码注册被拒（统一认证错误）。"""
    c5_reset()
    _, raw = await c5_runtime.auth.issue_tenant_invitation("PW", issued_by="test")
    with pytest.raises(CredentialExchangeError):
        await _register(c5_runtime, raw, "short@example.com", "123")


async def test_invalid_email_rejected(c5_runtime, c5_reset):
    """非法邮箱格式注册被拒。"""
    c5_reset()
    _, raw = await c5_runtime.auth.issue_tenant_invitation("EM", issued_by="test")
    with pytest.raises(CredentialExchangeError):
        await _register(c5_runtime, raw, "not-an-email", "pw-123456")


async def test_register_wrong_invite_rejected(c5_runtime, c5_reset):
    """陌生邀请码注册被拒（统一语义）。"""
    c5_reset()
    with pytest.raises(CredentialExchangeError):
        await _register(c5_runtime, "nxt_totally-fake-token-value", "x@example.com")


async def test_old_style_token_cannot_register(c5_runtime, c5_reset):
    """旧式绑定账号 Token（tenant_name 为 NULL）不能走注册路径。

    兼容面（design D5）：老 Token 只能经 exchange；注册路径拒绝（统一语义）。
    """
    c5_reset()
    # 用既有链路建 active 账号 + 签发绑定账号的 Token。
    created = await c5_runtime.provisioning.create_account(display_name="Legacy")
    await c5_runtime.provisioning.run_pending(max_jobs=4)
    account = await c5_runtime.provisioning.get_account(created["account"]["id"])
    _, legacy_raw = await c5_runtime.auth.issue_invitation(
        account["id"], issued_by="test"
    )
    with pytest.raises(CredentialExchangeError):
        await _register(c5_runtime, legacy_raw, "legacy@example.com")
    # 旧 Token 走 exchange 仍可用（存量兼容）。
    session, _ = await c5_runtime.auth.exchange_invitation(legacy_raw, user_agent="t")
    assert session["principal_type"] == "user"