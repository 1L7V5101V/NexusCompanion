"""并发兑换原子性测试（design.md ADR-2，§5.9.3）。

同一邀请 Token，两个独立 AuthRuntime（各自连接池）并发兑换，行锁串行化：
恰好一个成功，第二个 401 且不泄露原因；不产生第二个 session。

（按「暂不跑 PG」决策只写不跑；PG 可用后取消 skip 即得集成证据。）
"""

from __future__ import annotations

import asyncio

import pytest

from bootstrap.auth.runtime import create_auth_runtime
from bootstrap.db.repository.auth_repo import CredentialExchangeError

pytestmark = pytest.mark.postgres


@pytest.fixture
def c5_active_account(c5_runtime, c5_reset, tmp_path):
    """基线：active 账号 + 一个未消费邀请 Token（raw）。"""
    c5_reset()

    async def _make() -> tuple[dict, str]:
        account = await c5_runtime.provisioning.create_account(display_name="Exchange")
        await c5_runtime.provisioning.run_pending(max_jobs=4)
        account = await c5_runtime.provisioning.get_account(account["account"]["id"])
        assert account["status"] == "active"
        _, raw = await c5_runtime.auth.issue_invitation(account["id"], issued_by="test")
        return account, raw

    return _make


def _new_peer(c5_pg_url: str, tmp_path):
    """独立 AuthRuntime（单独连接池 + 单独 pepper 目录），模拟独立消费者。"""
    from agent.config_models import Config

    cfg = Config(provider="", model="", api_key="")
    cfg.storage.postgres_url = c5_pg_url
    return create_auth_runtime(config=cfg, workspace=tmp_path / f"ws-{id(tmp_path)}")


async def test_concurrent_exchange_single_success(c5_pg_url, c5_active_account, tmp_path):
    account, raw = await c5_active_account()
    peer_a = _new_peer(c5_pg_url, tmp_path)
    peer_b = _new_peer(c5_pg_url, tmp_path)
    try:
        async def _try(peer) -> dict:
            try:
                session, _ = await peer.auth.exchange_invitation(raw, user_agent="t")
                return {"ok": True, "session": session}
            except CredentialExchangeError:
                return {"ok": False}

        results = await asyncio.gather(_try(peer_a), _try(peer_b))
        ok = [r for r in results if r["ok"]]
        assert len(ok) == 1, f"并发兑换应恰好一个成功，got {len(ok)}"

        # 成功方返回正确的账号归属。
        winner = ok[0]["session"]
        assert str(winner["account_id"]) == account["id"]
        assert winner["principal_type"] == "user"

        # 再次兑换（原始字符串）同样失败：token 已消费。
        with pytest.raises(CredentialExchangeError):
            await peer_a.auth.exchange_invitation(raw, user_agent="t")
    finally:
        await peer_a.aclose()
        await peer_b.aclose()


async def test_digest_not_persisted_anywhere(c5_runtime, c5_active_account):
    """明文 token 不落库：access_tokens/auth_sessions 只存 64-hex digest（ADR-1）。"""
    account, raw = await c5_active_account()
    await c5_runtime.auth.exchange_invitation(raw, user_agent="t")

    from sqlalchemy import text

    async with c5_runtime.session_factory() as sess:
        token_row = (
            await sess.execute(text("SELECT token_digest FROM access_tokens"))
        ).fetchone()
        assert token_row is not None and len(token_row[0]) == 64
        session_row = (
            await sess.execute(
                text(
                    "SELECT session_digest FROM auth_sessions "
                    "WHERE principal_type='user'"
                )
            )
        ).fetchone()
        assert session_row is not None and len(session_row[0]) == 64
        blobs = "\n".join(
            str(row[0])
            for row in (
                await sess.execute(
                    text(
                        "SELECT token_digest FROM access_tokens "
                        "UNION ALL SELECT session_digest FROM auth_sessions"
                    )
                )
            ).fetchall()
        )
        assert raw not in blobs