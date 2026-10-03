"""过期凭据摘要抹除（tasks 1.3 / 5.1 / 5.2 / 5.3 / ADR-4）。

spec「过期凭据只抹除摘要并保留审计可核验的元数据」三 Scenario + fail-closed
负向 + NULLS DISTINCT 共存固化 + 幂等/批次。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.config_models import RetentionConfig
from bootstrap.db.repository.auth_repo import (
    CredentialRepository,
    SessionInvalidError,
)
from bootstrap.retention import RetentionSweeper

pytestmark = pytest.mark.postgres

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_D = "d" * 64


@pytest.fixture
def credentials(rt_factory: async_sessionmaker) -> CredentialRepository:
    return CredentialRepository(rt_factory)


def _seed_credentials(
    exec_sql: Callable[..., Any],
    *,
    account_id: str = "00000000-0000-0000-0000-000000000001",
) -> None:
    """四条凭据：
    - session DIGEST_A：已撤销 + 已过期 + 撤销于 40 天前（超 30d 宽限）→ 应清除
    - session DIGEST_B：活跃（未撤销未过期）→ 不触碰
    - session DIGEST_C：已撤销但未过期（expires_at 在未来）→ 不清除
    - token DIGEST_D：已撤销 + 已过期 + 超宽限 → 应清除
    """
    exec_sql(
        """
        INSERT INTO auth_sessions
            (id, principal_type, account_id, session_digest, idle_timeout_s,
             absolute_timeout_s, expires_at, revoked_at, revoked_reason, created_at, updated_at)
        VALUES (%s, 'user', %s, %s, 3600, 86400,
                now() - interval '10 days', now() - interval '40 days', 'rotate',
                now() - interval '50 days', now() - interval '40 days')
        """,
        (str(uuid.uuid4()), account_id, DIGEST_A),
    )
    exec_sql(
        """
        INSERT INTO auth_sessions
            (id, principal_type, account_id, session_digest, idle_timeout_s,
             absolute_timeout_s, expires_at, last_seen_at, created_at, updated_at)
        VALUES (%s, 'user', %s, %s, 3600, 86400,
                now() + interval '10 days', now(), now() - interval '1 day', now())
        """,
        (str(uuid.uuid4()), account_id, DIGEST_B),
    )
    exec_sql(
        """
        INSERT INTO auth_sessions
            (id, principal_type, account_id, session_digest, idle_timeout_s,
             absolute_timeout_s, expires_at, revoked_at, revoked_reason, created_at, updated_at)
        VALUES (%s, 'user', %s, %s, 3600, 86400,
                now() + interval '30 days', now() - interval '40 days', 'rotate',
                now() - interval '50 days', now() - interval '40 days')
        """,
        (str(uuid.uuid4()), account_id, DIGEST_C),
    )
    exec_sql(
        """
        INSERT INTO access_tokens
            (id, account_id, token_digest, expires_at, revoked_at, revoked_reason,
             created_at, updated_at)
        VALUES (%s, %s, %s, now() - interval '10 days', now() - interval '40 days',
                'rotate', now() - interval '50 days', now() - interval '40 days')
        """,
        (str(uuid.uuid4()), account_id, DIGEST_D),
    )


def test_revoked_expired_past_grace_cleared_metadata_kept(
    rt_reset: None,
    sweeper_rt: RetentionSweeper,
    exec_sql: Callable[..., Any],
) -> None:
    """Scenario「已撤销且已过期的会话摘要被清除但行保留」。"""
    _seed_credentials(exec_sql)

    report = asyncio_run(sweeper_rt.run_once(dry_run=False))

    cred = next(e for e in report.entities if e.target == "auth_sessions+access_tokens")
    assert cred.deleted == 2  # DIGEST_A session + DIGEST_D token
    row = exec_sql(
        "SELECT session_digest, principal_type, account_id, created_at, expires_at, "
        "revoked_at, revoked_reason FROM auth_sessions WHERE session_digest = %s",
        (DIGEST_A,),
    )
    assert row == []  # digest 已清除 → 摘要查不到该行
    purged = exec_sql(
        "SELECT principal_type, account_id, created_at, expires_at, revoked_at, "
        "revoked_reason, session_digest FROM auth_sessions WHERE revoked_reason = 'rotate' "
        "AND expires_at < now()",
    )
    assert len(purged) == 1
    r = purged[0]
    assert r[0] == "user"  # 归属保留
    assert r[1] is not None  # account 归属保留
    assert r[2] is not None and r[3] is not None and r[4] is not None  # 时间 metadata 完整
    assert r[6] is None  # 摘要已抹除
    token = exec_sql("SELECT token_digest FROM access_tokens WHERE token_digest IS NOT NULL")
    assert token == []


def test_active_credentials_untouched(
    rt_reset: None,
    sweeper_rt: RetentionSweeper,
    credentials: CredentialRepository,
    exec_sql: Callable[..., Any],
) -> None:
    """Scenario「活跃会话不被触碰」：校验与兑换行为不受影响。"""
    _seed_credentials(exec_sql)

    report = asyncio_run(sweeper_rt.run_once(dry_run=False))

    info = asyncio_run(
        credentials.validate_session(
            session_digest=DIGEST_B, expected_principal="user"
        )
    )
    assert info["principal_type"] == "user"  # 活跃会话照常校验（无摘要回显字段）


def test_revoked_but_not_expired_not_cleared(
    rt_reset: None,
    sweeper_rt: RetentionSweeper,
    exec_sql: Callable[..., Any],
) -> None:
    """Scenario「已撤销但未过期不清除」：清除条件要求同时满足三者。"""
    _seed_credentials(exec_sql)

    asyncio_run(sweeper_rt.run_once(dry_run=False))

    row = exec_sql(
        "SELECT session_digest FROM auth_sessions WHERE session_digest = %s",
        (DIGEST_C,),
    )
    assert row == [(DIGEST_C,)]  # 摘要原样保留


def test_token_without_expiry_never_cleared(
    rt_reset: None,
    sweeper_rt: RetentionSweeper,
    exec_sql: Callable[..., Any],
) -> None:
    """token 的 expires_at 为 NULL 视为不过期、不清除（task 5.1）。"""
    exec_sql(
        """
        INSERT INTO access_tokens
            (id, token_digest, revoked_at, revoked_reason, created_at, updated_at)
        VALUES (%s, %s, now() - interval '100 days', 'rotate', now(), now())
        """,
        (str(uuid.uuid4()), DIGEST_A),
    )

    report = asyncio_run(sweeper_rt.run_once(dry_run=False))

    cred = next(e for e in report.entities if e.target == "auth_sessions+access_tokens")
    assert cred.deleted == 0
    assert exec_sql("SELECT token_digest FROM access_tokens")[0][0] == DIGEST_A


def test_grace_window_holds_purge(
    rt_reset: None,
    rt_factory: async_sessionmaker,
    exec_sql: Callable[..., Any],
) -> None:
    """已撤销 + 已过期但撤销未满 purge_grace_s → 本轮不清除。"""
    sweeper = RetentionSweeper(rt_factory, RetentionConfig())
    _seed_credentials(exec_sql)
    # 把 DIGEST_A 的撤销时间挪进宽限窗口（10 天前 < 30d 宽限）。
    exec_sql(
        "UPDATE auth_sessions SET revoked_at = now() - interval '10 days', "
        "updated_at = now() - interval '10 days' WHERE session_digest = %s",
        (DIGEST_A,),
    )

    report = asyncio_run(sweeper.run_once(dry_run=False))

    cred = next(e for e in report.entities if e.target == "auth_sessions+access_tokens")
    assert cred.deleted == 1  # 只有 token（40 天前撤销）满足三条件


def test_fail_closed_after_purge(
    rt_reset: None,
    sweeper_rt: RetentionSweeper,
    credentials: CredentialRepository,
    exec_sql: Callable[..., Any],
) -> None:
    """task 5.2：清除后按原摘要校验必然失败（401 语义），不因 NULL 误命中。"""
    _seed_credentials(exec_sql)

    asyncio_run(sweeper_rt.run_once(dry_run=False))

    with pytest.raises(SessionInvalidError):
        asyncio_run(
            credentials.validate_session(
                session_digest=DIGEST_A, expected_principal="user"
            )
        )


def test_count_active_admin_sessions_unaffected(
    rt_reset: None,
    sweeper_rt: RetentionSweeper,
    rt_factory: async_sessionmaker,
    exec_sql: Callable[..., Any],
) -> None:
    """task 5.2：既有查询（count_active_admin_sessions 等）不受 NULL 摘要影响。"""
    from bootstrap.db.repository.auth_repo import AdminRepository

    exec_sql(
        """
        INSERT INTO auth_sessions
            (id, principal_type, session_digest, idle_timeout_s, absolute_timeout_s,
             expires_at, revoked_at, created_at, updated_at)
        VALUES (%s, 'admin', %s, 3600, 86400, now() - interval '10 days',
                now() - interval '40 days', now(), now())
        """,
        (str(uuid.uuid4()), DIGEST_A),
    )
    admin = AdminRepository(rt_factory)
    before = asyncio_run(admin.count_active_admin_sessions())
    assert before == 0  # 已撤销 admin 会话不计入

    asyncio_run(sweeper_rt.run_once(dry_run=False))

    assert asyncio_run(admin.count_active_admin_sessions()) == 0


def test_null_digest_rows_coexist_nulls_distinct(
    rt_reset: None,
    sweeper_rt: RetentionSweeper,
    exec_sql: Callable[..., Any],
) -> None:
    """task 1.3：固化 PG 默认 NULLS DISTINCT —— 两条已清除行（digest NULL）共存。

    若部署版本行为不同（treat NULL as equal），本用例失败 → 回到 ADR-4 改
    partial unique index，不得擅自换方案。
    """
    # 两条 session 用不同 digest 入库（UNIQUE 只约束非 NULL 值），清除后两行
    # digest 均为 NULL —— 固化部署 PG 的默认 NULLS DISTINCT 行为。
    exec_sql(
        """
        INSERT INTO auth_sessions
            (id, principal_type, account_id, session_digest, idle_timeout_s,
             absolute_timeout_s, expires_at, revoked_at, revoked_reason, created_at, updated_at)
        VALUES (%s, 'user', '00000000-0000-0000-0000-000000000001', %s, 3600, 86400,
                now() - interval '10 days', now() - interval '40 days', 'rotate',
                now() - interval '50 days', now() - interval '40 days')
        """,
        (str(uuid.uuid4()), DIGEST_A),
    )
    exec_sql(
        """
        INSERT INTO auth_sessions
            (id, principal_type, account_id, session_digest, idle_timeout_s,
             absolute_timeout_s, expires_at, revoked_at, revoked_reason, created_at, updated_at)
        VALUES (%s, 'user', '00000000-0000-0000-0000-000000000001', %s, 3600, 86400,
                now() - interval '10 days', now() - interval '40 days', 'rotate',
                now() - interval '50 days', now() - interval '40 days')
        """,
        (str(uuid.uuid4()), DIGEST_B),
    )

    report = asyncio_run(sweeper_rt.run_once(dry_run=False))

    cred = next(e for e in report.entities if e.target == "auth_sessions+access_tokens")
    assert cred.deleted >= 2
    null_sessions = exec_sql(
        "SELECT count(*) FROM auth_sessions WHERE session_digest IS NULL"
    )[0][0]
    assert null_sessions == 2  # UNIQUE(digest) 下两行 NULL 共存（NULLS DISTINCT）


def test_purge_idempotent_and_batched(
    rt_reset: None,
    rt_factory: async_sessionmaker,
    exec_sql: Callable[..., Any],
) -> None:
    """task 5.3：已清除行不重复计入（第二轮 cleared=0）；清除走 batch/max_batches。"""
    sweeper = RetentionSweeper(rt_factory, RetentionConfig(batch_size=1, max_batches=20))
    # 3 条满足条件的 session（batch_size=1 → 需多批收敛）。
    for _ in range(3):
        exec_sql(
            """
            INSERT INTO auth_sessions
                (id, principal_type, account_id, session_digest, idle_timeout_s,
                 absolute_timeout_s, expires_at, revoked_at, revoked_reason,
                 created_at, updated_at)
            VALUES (%s, 'user', '00000000-0000-0000-0000-000000000001', %s, 3600, 86400,
                    now() - interval '10 days', now() - interval '40 days', 'rotate',
                    now() - interval '50 days', now() - interval '40 days')
            """,
            (str(uuid.uuid4()), uuid.uuid4().hex * 2),
        )

    first = asyncio_run(sweeper.run_once(dry_run=False))
    cred1 = next(e for e in first.entities if e.target == "auth_sessions+access_tokens")
    assert cred1.deleted == 3  # 分批收敛：1+1+1

    second = asyncio_run(sweeper.run_once(dry_run=False))
    cred2 = next(e for e in second.entities if e.target == "auth_sessions+access_tokens")
    assert cred2.deleted == 0


def test_dry_run_purge_counts_without_mutating(
    rt_reset: None,
    sweeper_rt: RetentionSweeper,
    exec_sql: Callable[..., Any],
) -> None:
    """演练模式对凭据腿同样零变更：只报"将清除"数。"""
    _seed_credentials(exec_sql)

    dry = asyncio_run(sweeper_rt.run_once(dry_run=True))

    cred = next(e for e in dry.entities if e.target == "auth_sessions+access_tokens")
    assert cred.deleted == 2
    assert cred.dry_run is True
    assert (
        exec_sql("SELECT count(*) FROM auth_sessions WHERE session_digest IS NULL")[0][0]
        == 0
    )
    assert exec_sql("SELECT count(*) FROM access_tokens")[0][0] == 1


def asyncio_run(awaitable: Any) -> Any:
    import asyncio

    return asyncio.run(awaitable)
