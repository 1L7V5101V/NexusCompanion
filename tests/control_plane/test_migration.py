"""C2 migration 验收：七表、约束命名、部分唯一索引谓词、CHECK/FK 生效、downgrade 循环。"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from scripts.migrate.alembic_util import downgrade_to, upgrade_head
from tests.control_plane.conftest import C2_TABLES

pytestmark = pytest.mark.postgres

EXPECTED_CONSTRAINTS: dict[str, set[str]] = {
    "message_deduplication_keys": {
        "ck_message_dedup_keys_key_present",
        "fk_message_dedup_keys_account_id",
    },
    "inbox_records": {
        "uq_inbox_records_dedup_key_id",
        "fk_inbox_records_conversation_id",
        "fk_inbox_records_dedup_key_id",
        "fk_inbox_records_canonical_message_id",
        "ck_inbox_records_status",
    },
    "turns": {
        "fk_turns_conversation_id",
        "fk_turns_inbox_record_id",
        "fk_turns_final_message_id",
        "ck_turns_status",
    },
    "tool_calls": {"fk_tool_calls_turn_id", "ck_tool_calls_status"},
    "background_work_items": {
        "uq_background_work_items_idempotency_key",
        "fk_background_work_items_conversation_id",
        "ck_background_work_items_status",
    },
    "outbound_delivery_intents": {
        "uq_outbound_delivery_intents_idempotency_key",
        "fk_outbound_delivery_intents_conversation_id",
        "fk_outbound_delivery_intents_message_id",
        "fk_outbound_delivery_intents_turn_id",
        "ck_outbound_delivery_intents_status",
    },
    "delivery_attempts": {"fk_delivery_attempts_intent_id", "ck_delivery_attempts_outcome"},
}

EXPECTED_INDEXES: dict[str, set[str]] = {
    "message_deduplication_keys": {
        "uq_message_dedup_keys_source",
        "uq_message_dedup_keys_client",
    },
    "inbox_records": {"ix_inbox_records_conversation"},
    "turns": {"uq_turns_inbox_record_id", "ix_turns_tenant_status"},
    "tool_calls": {"ix_tool_calls_turn"},
    "background_work_items": {"ix_background_work_items_tenant_status"},
    "outbound_delivery_intents": {"ix_outbound_delivery_intents_claim"},
    "delivery_attempts": {"ix_delivery_attempts_intent"},
}


def _constraint_names(conn: psycopg.Connection, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def _index_names(conn: psycopg.Connection, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = %s",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def test_seven_tables_exist(c2_pg_url) -> None:
    conn = psycopg.connect(c2_pg_url)
    try:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        existing = {r[0] for r in rows}
        assert set(C2_TABLES) <= existing
    finally:
        conn.close()


def test_constraints_and_indexes_present(c2_pg_url) -> None:
    conn = psycopg.connect(c2_pg_url)
    try:
        for table, expected in EXPECTED_CONSTRAINTS.items():
            missing = expected - _constraint_names(conn, table)
            assert not missing, f"{table} 缺少约束: {missing}"
        for table, expected in EXPECTED_INDEXES.items():
            missing = expected - _index_names(conn, table)
            assert not missing, f"{table} 缺少索引: {missing}"
    finally:
        conn.close()


def test_partial_unique_index_predicates(c2_pg_url) -> None:
    """双键唯一性必须是「该键存在时唯一」的部分唯一索引（design.md ADR-2）。"""
    conn = psycopg.connect(c2_pg_url)
    try:
        defs = dict(
            conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
                " AND indexname IN ('uq_message_dedup_keys_source',"
                " 'uq_message_dedup_keys_client', 'uq_turns_inbox_record_id')"
            ).fetchall()
        )
        assert "WHERE (source_message_id IS NOT NULL)" in defs["uq_message_dedup_keys_source"]
        assert "WHERE (client_message_id IS NOT NULL)" in defs["uq_message_dedup_keys_client"]
        assert "WHERE (inbox_record_id IS NOT NULL)" in defs["uq_turns_inbox_record_id"]
        assert defs["uq_message_dedup_keys_source"].startswith("CREATE UNIQUE INDEX")
    finally:
        conn.close()


def test_status_check_constraints_reject_unknown_values(c2_pg_url) -> None:
    """非法状态值被 CHECK 拒绝（状态机由数据库兜底，不只靠应用层）。"""
    conn = psycopg.connect(c2_pg_url)
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO turns (tenant_id, conversation_id, status)"
                " VALUES ('x', '00000000-0000-0000-0000-000000000002', 'exploded')"
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO outbound_delivery_intents (tenant_id, conversation_id,"
                " message_id, idempotency_key, channel, target_chat_id, status)"
                " VALUES ('x', '00000000-0000-0000-0000-000000000002',"
                " '00000000-0000-0000-0000-000000000003', 'k-check-1', 'telegram',"
                " 'chat-1', 'mailed')"
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO message_deduplication_keys (tenant_id, source_channel,"
                " source_message_id, client_message_id)"
                " VALUES ('x', 'telegram', 'm-1', 'cm-1')"
            )
    finally:
        conn.rollback()
        conn.close()


def test_history_rows_not_cascade_deleted(c2_pg_url) -> None:
    """FK RESTRICT：历史 message/turn/attempt 不级联物理删除（§5.9.9）。"""
    engine = create_async_engine(
        c2_pg_url.replace("postgresql://", "postgresql+asyncpg://"), poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    from bootstrap.db.repository.canonical_repo import CanonicalIdentityRepository
    from bootstrap.db.repository.control_plane_repo import (
        IngressRepository,
        TurnControlRepository,
    )

    async def _seed() -> tuple[str, str, str]:
        identities = CanonicalIdentityRepository(factory)
        provisioned = await identities.provision_account_with_agent(
            f"restrict_{uuid.uuid4().hex[:8]}", status="active"
        )
        tenant_id = provisioned["conversation"]["tenant_id"]
        conv_id = provisioned["conversation"]["id"]
        ingress = IngressRepository(factory)
        accepted = await ingress.accept_inbound(
            tenant_id,
            conv_id,
            source_channel="telegram",
            source_identity_id="user-r",
            source_message_id="m-restrict-1",
            content="hello",
        )
        assert accepted.turn_id is not None
        control = TurnControlRepository(factory)
        completion = await control.complete_turn_with_delivery(
            tenant_id,
            conv_id,
            accepted.turn_id,
            expected_status="queued",
            response_content="reply",
            delivery_channel="telegram",
            delivery_target="chat-r",
        )
        return tenant_id, conv_id, completion.intent["message_id"]

    import asyncio

    tenant_id, conv_id, message_id = asyncio.run(_seed())
    engine.sync_engine.dispose()

    conn = psycopg.connect(c2_pg_url)
    try:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "DELETE FROM canonical_conversations WHERE id = %s", (conv_id,)
            )
        conn.rollback()
        # final message 被 outbox intent 引用（message_id FK RESTRICT）→ 不可物理删除
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "DELETE FROM canonical_messages WHERE id = %s", (message_id,)
            )
        conn.rollback()
    finally:
        conn.close()


def test_downgrade_upgrade_cycle(c2_pg_url, c2_alembic_cfg) -> None:
    """downgrade 删除七表（未 cutover 新对象）→ 再 upgrade 恢复（design.md §5）。"""
    downgrade_to(c2_alembic_cfg, "c4d8f2a6e9b3")
    conn = psycopg.connect(c2_pg_url)
    try:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        assert not (set(C2_TABLES) & {r[0] for r in rows})
    finally:
        conn.close()
    upgrade_head(c2_alembic_cfg)
    conn = psycopg.connect(c2_pg_url)
    try:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        assert set(C2_TABLES) <= {r[0] for r in rows}
    finally:
        conn.close()


async def test_seed_not_applicable(c2_pg_url) -> None:
    """seed = not_applicable（design.md §3）：control plane 表为空基线。"""
    engine = create_async_engine(
        c2_pg_url.replace("postgresql://", "postgresql+asyncpg://"), poolclass=NullPool
    )
    async with engine.connect() as conn:
        for table in ("inbox_records", "turns", "outbound_delivery_intents"):
            count = (await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar()
            assert count == 0
    await engine.dispose()
