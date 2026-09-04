"""resolver 正/负向、唯一约束负向、跨租户隔离与 fixture 契约测试（§5.9.2 / §5.9.9）。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.repository.canonical_repo import CanonicalConversationNotFoundError
from bootstrap.identity import (
    CanonicalIdentityResolver,
    IdentityResolutionError,
)
from tests.canonical_identity.conftest import (
    DEV_ACCOUNT_ID,
    DEV_CONVERSATION_ID,
    DEV_TENANT_ID,
)

pytestmark = pytest.mark.postgres

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "canonical_identity_chain.json"
)


def _repos(factory: async_sessionmaker):
    from bootstrap.db.repository.canonical_repo import (
        CanonicalIdentityRepository,
        CanonicalMessageRepository,
    )

    return CanonicalIdentityRepository(factory), CanonicalMessageRepository(factory)


# ── 正向解析 ─────────────────────────────────────────────────


async def test_resolve_by_tenant_seed(c1_reset, c1_factory) -> None:
    c1_reset()
    resolver = CanonicalIdentityResolver(c1_factory)
    identity = await resolver.resolve_by_tenant(DEV_TENANT_ID)
    assert identity.account_id == DEV_ACCOUNT_ID
    assert identity.tenant_id == DEV_TENANT_ID
    assert identity.conversation_id == DEV_CONVERSATION_ID


async def test_resolve_by_account_id_seed(c1_reset, c1_factory) -> None:
    c1_reset()
    resolver = CanonicalIdentityResolver(c1_factory)
    identity = await resolver.resolve_by_account(DEV_ACCOUNT_ID)
    assert identity.tenant_id == DEV_TENANT_ID
    assert identity.conversation_id == DEV_CONVERSATION_ID


async def test_provisioned_account_resolves_with_empty_history(
    c1_reset, c1_factory
) -> None:
    """新 provisioning 账号可解析且规范会话从空历史开始（无旧单体映射）。"""
    c1_reset()
    identities, messages = _repos(c1_factory)
    tenant = f"prov_{uuid.uuid4().hex[:8]}"
    created = await identities.create_account_with_conversation(tenant, status="active")
    resolver = CanonicalIdentityResolver(c1_factory)
    identity = await resolver.resolve_by_tenant(tenant)
    assert identity.account_id == created["account"]["id"]
    assert identity.conversation_id == created["conversation"]["id"]
    assert await messages.fetch_messages(tenant, identity.conversation_id) == []


# ── 负向：无 binding fail-closed，不落 DEFAULT_TENANT ──────────


async def test_unknown_account_rejected(c1_reset, c1_factory) -> None:
    c1_reset()
    resolver = CanonicalIdentityResolver(c1_factory)
    with pytest.raises(IdentityResolutionError):
        await resolver.resolve_by_account(str(uuid.uuid4()))


async def test_unknown_tenant_rejected(c1_reset, c1_factory) -> None:
    c1_reset()
    resolver = CanonicalIdentityResolver(c1_factory)
    with pytest.raises(IdentityResolutionError):
        await resolver.resolve_by_tenant("ghost-tenant")


@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_principal_rejected(c1_reset, c1_factory, blank: str) -> None:
    c1_reset()
    resolver = CanonicalIdentityResolver(c1_factory)
    with pytest.raises(IdentityResolutionError):
        await resolver.resolve_by_tenant(blank)
    with pytest.raises(IdentityResolutionError):
        await resolver.resolve_by_account(blank)


async def test_default_tenant_is_not_canonical_identity(c1_reset, c1_factory) -> None:
    """DEFAULT_TENANT 不是合法 canonical 身份：无 binding 即拒绝（§5.9.2）。"""
    c1_reset()
    resolver = CanonicalIdentityResolver(c1_factory)
    with pytest.raises(IdentityResolutionError):
        await resolver.resolve_by_tenant("default")


async def test_account_without_conversation_rejected(
    c1_reset, c1_pg_url, c1_factory
) -> None:
    """1:1 链被外力破坏（账号无会话）仍 fail-closed，不猜测。"""
    c1_reset()
    conn = psycopg.connect(c1_pg_url, autocommit=True)
    conn.execute(
        "INSERT INTO test_accounts (tenant_id, status) VALUES ('orphan_acc', 'active')"
    )
    conn.close()
    resolver = CanonicalIdentityResolver(c1_factory)
    with pytest.raises(IdentityResolutionError):
        await resolver.resolve_by_tenant("orphan_acc")


# ── 跨租户隔离 ───────────────────────────────────────────────


async def test_cross_tenant_messages_invisible(c1_reset, c1_factory) -> None:
    c1_reset()
    identities, messages = _repos(c1_factory)
    other = await identities.create_account_with_conversation(
        f"iso_{uuid.uuid4().hex[:8]}", status="active"
    )
    await messages.append_message(
        DEV_TENANT_ID, DEV_CONVERSATION_ID, role="user", content="secret-a"
    )
    other_tenant = other["conversation"]["tenant_id"]
    other_conv = other["conversation"]["id"]
    assert await messages.fetch_messages(other_tenant, DEV_CONVERSATION_ID) == []
    assert await messages.fetch_messages(other_tenant, other_conv) == []
    with pytest.raises(CanonicalConversationNotFoundError):
        await messages.latest_sequence(other_tenant, DEV_CONVERSATION_ID)
    # 反向亦然：未知 tenant 解析同样拒绝。
    with pytest.raises(IdentityResolutionError):
        await CanonicalIdentityResolver(c1_factory).resolve_by_tenant(
            DEV_TENANT_ID + "-nope"
        )


# ── DB 约束负向（绕过 repository 的直接写入必须被数据库拒绝）──


def test_duplicate_tenant_account_rejected(c1_reset, c1_pg_url) -> None:
    c1_reset()
    conn = psycopg.connect(c1_pg_url)
    try:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO test_accounts (tenant_id, status) VALUES ('dev', 'active')"
            )
        conn.rollback()
    finally:
        conn.close()


def test_duplicate_tenant_conversation_rejected(c1_reset, c1_pg_url) -> None:
    c1_reset()
    conn = psycopg.connect(c1_pg_url)
    try:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO canonical_conversations (tenant_id, account_id) "
                "VALUES ('dev', %s)",
                (DEV_ACCOUNT_ID,),
            )
        conn.rollback()
    finally:
        conn.close()


def test_duplicate_conversation_sequence_rejected(c1_reset, c1_pg_url) -> None:
    c1_reset()
    conn = psycopg.connect(c1_pg_url)
    try:
        conn.execute(
            "INSERT INTO canonical_messages (tenant_id, conversation_id, sequence, role, content) "
            "VALUES ('dev', %s, 0, 'user', 'first')",
            (DEV_CONVERSATION_ID,),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO canonical_messages (tenant_id, conversation_id, sequence, role, content) "
                "VALUES ('dev', %s, 0, 'user', 'dup')",
                (DEV_CONVERSATION_ID,),
            )
        conn.rollback()
    finally:
        conn.close()


def test_invalid_role_rejected_by_check(c1_reset, c1_pg_url) -> None:
    c1_reset()
    conn = psycopg.connect(c1_pg_url)
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO canonical_messages (tenant_id, conversation_id, sequence, role) "
                "VALUES ('dev', %s, 0, 'wizard')",
                (DEV_CONVERSATION_ID,),
            )
        conn.rollback()
    finally:
        conn.close()


def test_orphan_foreign_keys_rejected(c1_reset, c1_pg_url) -> None:
    c1_reset()
    conn = psycopg.connect(c1_pg_url)
    try:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO canonical_conversations (tenant_id, account_id) "
                "VALUES ('orphan_conv', %s)",
                (str(uuid.uuid4()),),
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO canonical_messages (tenant_id, conversation_id, sequence, role) "
                "VALUES ('dev', %s, 0, 'user')",
                (str(uuid.uuid4()),),
            )
        conn.rollback()
    finally:
        conn.close()


def test_delete_restricted_for_history(c1_reset, c1_pg_url) -> None:
    """历史 message 不级联物理删除：RESTRICT 外键挡住会话删除。"""
    c1_reset()
    conn = psycopg.connect(c1_pg_url)
    try:
        conn.execute(
            "INSERT INTO canonical_messages (tenant_id, conversation_id, sequence, role) "
            "VALUES ('dev', %s, 0, 'user')",
            (DEV_CONVERSATION_ID,),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "DELETE FROM canonical_conversations WHERE id = %s",
                (DEV_CONVERSATION_ID,),
            )
        conn.rollback()
    finally:
        conn.close()


# ── 契约 fixture 执行（C2/C4/C5/C9/C10/C14 复用同一 JSON）────


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


async def test_contract_fixture_positive_cases(c1_reset, c1_factory) -> None:
    c1_reset()
    fixture = _load_fixture()
    resolver = CanonicalIdentityResolver(c1_factory)
    for case in fixture["positive_cases"]:
        kind = case["input"]["kind"]
        value = case["input"]["value"]
        identity = (
            await resolver.resolve_by_tenant(value)
            if kind == "tenant"
            else await resolver.resolve_by_account(value)
        )
        assert identity.account_id == case["expected"]["account_id"], case["name"]
        assert identity.tenant_id == case["expected"]["tenant_id"], case["name"]
        assert identity.conversation_id == case["expected"]["conversation_id"], case[
            "name"
        ]


async def test_contract_fixture_negative_cases(c1_reset, c1_factory) -> None:
    c1_reset()
    fixture = _load_fixture()
    resolver = CanonicalIdentityResolver(c1_factory)
    for case in fixture["negative_cases"]:
        kind = case["input"]["kind"]
        value = case["input"]["value"]
        with pytest.raises(IdentityResolutionError):
            if kind == "tenant":
                await resolver.resolve_by_tenant(value)
            else:
                await resolver.resolve_by_account(value)


def test_contract_fixture_stream_semantics() -> None:
    stream = _load_fixture()["message_stream"]
    assert stream["sequence_start"] == 0
    assert stream["per_conversation_independent"] is True
    assert stream["allocation"] == "single_transaction_atomic"
    assert stream["uniqueness"] == ["conversation_id", "sequence"]
    assert stream["cursor_semantics"] == "after_sequence"
