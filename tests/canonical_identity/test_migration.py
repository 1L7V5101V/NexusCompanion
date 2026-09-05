"""migration 验收：空 PG `alembic upgrade head` 创建全部实体且约束符合 §5.9.9。"""

from __future__ import annotations

import psycopg
import pytest

from tests.canonical_identity.conftest import (
    DEV_ACCOUNT_ID,
    DEV_CONVERSATION_ID,
    DEV_TENANT_ID,
)

pytestmark = pytest.mark.postgres

EXPECTED_CONSTRAINTS = {
    "test_accounts": {
        "ck_test_accounts_status",
    },
    "canonical_conversations": {
        "uq_canonical_conversations_tenant_id",
        "ck_canonical_conversations_status",
        "fk_canonical_conversations_account_id",
    },
    "canonical_messages": {
        "uq_canonical_messages_conversation_sequence",
        "ck_canonical_messages_role",
        "fk_canonical_messages_conversation_id",
    },
}

EXPECTED_INDEXES = {
    "ix_canonical_messages_tenant_conversation",
}


def test_upgrade_head_creates_all_entities(c1_pg_url: str) -> None:
    with psycopg.connect(c1_pg_url) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name IN "
                "('test_accounts', 'canonical_conversations', 'canonical_messages')"
            ).fetchall()
        }
        assert tables == {"test_accounts", "canonical_conversations", "canonical_messages"}

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

        # 空库基线：canonical stream 无历史（§5.9.2 从空历史开始）。
        assert conn.execute("SELECT count(*) FROM canonical_messages").fetchone()[0] == 0

        # dev seed：账号行自身不带 tenant_id（account→N 扩展，tenant 挂在会话行上）。
        # psycopg3 会把 UUID 列解码为 uuid.UUID 对象。
        account = conn.execute(
            "SELECT status, display_name FROM test_accounts WHERE id = %s",
            (DEV_ACCOUNT_ID,),
        ).fetchone()
        assert account == ("active", "Pilot Dev Account")
        # 会话 seed 存在且计数器为 0（下一序号即 0 → 0-based）。
        conversation = conn.execute(
            "SELECT tenant_id, account_id, status, next_sequence FROM canonical_conversations "
            "WHERE id = %s",
            (DEV_CONVERSATION_ID,),
        ).fetchone()
        assert conversation is not None
        assert conversation[0] == DEV_TENANT_ID
        assert str(conversation[1]) == DEV_ACCOUNT_ID
        assert conversation[2] == "active"
        assert conversation[3] == 0


def test_unique_constraints_match_frozen_semantics(c1_pg_url: str) -> None:
    """§5.9.9 唯一约束：tenant 全局唯一（会话表，账号不持 tenant）+ (conversation, sequence) 唯一。"""
    with psycopg.connect(c1_pg_url) as conn:
        uniques = conn.execute(
            "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
            "FROM pg_constraint WHERE contype = 'u' AND connamespace = 'public'::regnamespace "
            "AND conname LIKE 'uq_%'"
        ).fetchall()
        by_name = {name: (table, definition) for table, name, definition in uniques}
        # account→N 扩展移除了账号表的 tenant_id 唯一约束；唯一性收敛到会话表。
        assert "uq_test_accounts_tenant_id" not in by_name
        assert "uq_canonical_conversations_tenant_id" in by_name
        assert "tenant_id" in by_name["uq_canonical_conversations_tenant_id"][1]
        assert "uq_canonical_messages_conversation_sequence" in by_name
        definition = by_name["uq_canonical_messages_conversation_sequence"][1]
        assert "conversation_id" in definition and "sequence" in definition


def test_downgrade_then_upgrade_cycle(c1_alembic_cfg, c1_reset) -> None:
    """C1 未 cutover，downgrade 删表安全（§5.9.9）；重放 upgrade 后表与 seed 恢复。

    先 c1_reset 收敛到 dev 单账号，保证 account→N migration 的 downgrade 守卫
    （每账号恰好一条会话才能重建 1:1 账号↔tenant）通过。
    """
    c1_reset()
    from scripts.migrate.alembic_util import downgrade_to, upgrade_head

    downgrade_to(c1_alembic_cfg, "b6e9d2c4a8f1")
    url = c1_alembic_cfg.get_main_option("sqlalchemy.url")
    with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://")) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name IN "
                "('test_accounts', 'canonical_conversations', 'canonical_messages')"
            ).fetchall()
        }
        assert tables == set(), "downgrade 后三张 C1 表应被删除"

    upgrade_head(c1_alembic_cfg)
    with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://")) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM canonical_conversations WHERE tenant_id = 'dev'"
            ).fetchone()[0]
            == 1
        )
