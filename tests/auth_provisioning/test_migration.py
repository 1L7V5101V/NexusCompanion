"""C5 auth migration 验收：五表 + 唯一约束 + FK(RESTRICT) + CHECK + 索引。

复用 C1 scratch DB 整套 fixture（``tests.auth_provisioning.conftest``），在空 PG 上
``alembic upgrade head``（Create→Verify 的 Create 步）断言 design.md §1 的每一条
DDL；downgrade 后重放 upgrade 验证循环安全（§5.9.9）。当前按「暂不跑 PG」只写
不跑。
"""

from __future__ import annotations

import psycopg
import pytest

pytestmark = pytest.mark.postgres

C5_TABLES = {
    "access_tokens",
    "auth_sessions",
    "admin_credentials",
    "admin_audit_events",
    "tenant_provisioning_jobs",
}

EXPECTED_CONSTRAINTS = {
    "access_tokens": {
        "uq_access_tokens_digest",
        "ck_access_tokens_digest_sha256",
        "fk_access_tokens_account_id",
    },
    "auth_sessions": {
        "uq_auth_sessions_digest",
        "ck_auth_sessions_principal",
        "fk_auth_sessions_account_id",
    },
    "admin_credentials": {
        "ck_admin_credentials_singleton",
        "ck_admin_credentials_digest_sha256",
    },
    "tenant_provisioning_jobs": {
        "uq_tenant_provisioning_jobs_tenant",
        "ck_tenant_provisioning_jobs_status",
        "ck_tenant_provisioning_jobs_tenant_present",
        "fk_tenant_provisioning_jobs_account",
    },
}

EXPECTED_INDEXES = {
    "ix_access_tokens_account",
    "ix_auth_sessions_account",
    "ix_admin_audit_created",
}


def test_upgrade_head_creates_all_five_tables(c5_pg_url: str, c5_reset) -> None:
    # 本会话 scratch DB 被全量运行前面文件共享：先复位到空库基线。
    c5_reset()
    with psycopg.connect(c5_pg_url) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = ANY(%s)",
                (list(C5_TABLES),),
            ).fetchall()
        }
        assert tables == C5_TABLES

        for table, names in EXPECTED_CONSTRAINTS.items():
            found = {
                row[0]
                for row in conn.execute(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = %s::regclass AND contype IN ('u', 'c', 'f')",
                    (table,),
                ).fetchall()
            }
            missing = names - found
            assert not missing, f"{table} 缺约束: {missing}"

        found_indexes = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            ).fetchall()
        }
        assert EXPECTED_INDEXES <= found_indexes

        # 空库基线：admin 单行由 CLI bootstrap 显式创建（not_applicable seed）。
        assert conn.execute("SELECT count(*) FROM admin_credentials").fetchone()[0] == 0


def test_admin_single_row_check(c5_pg_url: str, c5_reset) -> None:
    """CHECK (id = 1) 强制单行：插入第二行被拒。"""
    c5_reset()
    with psycopg.connect(c5_pg_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO admin_credentials (id, recovery_digest) "
            "VALUES (1, %s)",
            ("a" * 64,),
        )
        try:
            conn.execute(
                "INSERT INTO admin_credentials (id, recovery_digest) "
                "VALUES (2, %s)",
                ("b" * 64,),
            )
        except psycopg.Error:
            pass  # 期望被 CHECK 拒绝
        else:
            raise AssertionError("admin_credentials 允许了第二行（违反单行约束）")
        assert conn.execute("SELECT count(*) FROM admin_credentials").fetchone()[0] == 1


def test_digest_check_requires_64_hex(c5_pg_url: str, c5_reset) -> None:
    """access_tokens / admin_credentials 只接受 64 字符（SHA-256 hex digest，ADR-1）。"""
    c5_reset()
    with psycopg.connect(c5_pg_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO test_accounts (id, status) VALUES "
            "(%s, 'active')",
            ("20000000-0000-0000-0000-0000000000aa",),
        )
        try:
            conn.execute(
                "INSERT INTO access_tokens (account_id, token_digest) VALUES (%s, %s)",
                (
                    "20000000-0000-0000-0000-0000000000aa",
                    "short-not-64",
                ),
            )
        except psycopg.Error:
            pass
        else:
            raise AssertionError("非 64 字符 digest 被接受")
        assert (
            conn.execute("SELECT count(*) FROM access_tokens").fetchone()[0] == 0
        )


def test_downgrade_then_upgrade_cycle(
    c5_pg_url: str, c5_alembic_cfg, c5_reset
) -> None:
    """downgrade 删 C5 五表 → 再 upgrade 恢复（未 cutover 安全降级，§5.9.9）。"""
    c5_reset()
    from scripts.migrate.alembic_util import downgrade_to, upgrade_head

    downgrade_to(c5_alembic_cfg, "f3c8a9d2e7b4")
    with psycopg.connect(c5_pg_url) as conn:
        found = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = ANY(%s)",
                (list(C5_TABLES),),
            ).fetchall()
        }
        assert found == set(), "downgrade 后 C5 五表应被删除"

    upgrade_head(c5_alembic_cfg)
    with psycopg.connect(c5_pg_url) as conn:
        found = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = ANY(%s)",
                (list(C5_TABLES),),
            ).fetchall()
        }
        assert found == C5_TABLES