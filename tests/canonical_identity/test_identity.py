"""resolver 正/负向、唯一约束负向、跨租户隔离与 fixture 契约测试。

覆盖 §5.9.2 / §5.9.9 语义与 account→N tenant 扩展
（openspec/changes/2026-09-05-c1-account-multi-tenant/）：账号可拥有多个
agent（tenant），tenant 与 canonical conversation 仍 1:1；单三元组解析只发生在
tenant 级，账号级走 `list_agents` 枚举。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.repository.canonical_repo import (
    CanonicalConversationNotFoundError,
    CanonicalIdentityRepository,
    CanonicalMessageRepository,
    TenantAlreadyBoundError,
)
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
    return CanonicalIdentityRepository(factory), CanonicalMessageRepository(factory)


# ── 正向解析 ─────────────────────────────────────────────────


async def test_resolve_by_tenant_seed(c1_reset, c1_factory) -> None:
    c1_reset()
    resolver = CanonicalIdentityResolver(c1_factory)
    identity = await resolver.resolve_by_tenant(DEV_TENANT_ID)
    assert identity.account_id == DEV_ACCOUNT_ID
    assert identity.tenant_id == DEV_TENANT_ID
    assert identity.conversation_id == DEV_CONVERSATION_ID


async def test_list_agents_seeded_dev(c1_reset, c1_factory) -> None:
    """dev 账号恰有一个 agent：list_agents 返回单一 dev 三元组。"""
    c1_reset()
    resolver = CanonicalIdentityResolver(c1_factory)
    agents = await resolver.list_agents(DEV_ACCOUNT_ID)
    assert len(agents) == 1
    assert agents[0].account_id == DEV_ACCOUNT_ID
    assert agents[0].tenant_id == DEV_TENANT_ID
    assert agents[0].conversation_id == DEV_CONVERSATION_ID


async def test_provisioned_account_resolves_with_empty_history(
    c1_reset, c1_factory
) -> None:
    """新 provisioning 账号可解析且规范会话从空历史开始（无旧单体映射）。"""
    c1_reset()
    identities, messages = _repos(c1_factory)
    tenant = f"prov_{uuid.uuid4().hex[:8]}"
    created = await identities.provision_account_with_agent(
        tenant, status="active"
    )
    resolver = CanonicalIdentityResolver(c1_factory)
    identity = await resolver.resolve_by_tenant(tenant)
    assert identity.account_id == created["account"]["id"]
    assert identity.conversation_id == created["conversation"]["id"]
    assert await messages.fetch_messages(tenant, identity.conversation_id) == []


async def test_one_account_many_agents_independent(
    c1_reset, c1_factory
) -> None:
    """account→N tenant：同一账号两个 agent（各自 tenant）独立解析、独立空消息流、互不串。"""
    c1_reset()
    identities, messages = _repos(c1_factory)
    account_id = str(uuid.uuid4())
    tenant_a = f"agent_a_{uuid.uuid4().hex[:8]}"
    tenant_b = f"agent_b_{uuid.uuid4().hex[:8]}"
    first = await identities.provision_account_with_agent(
        tenant_a, account_id=account_id, status="active"
    )
    second = await identities.create_agent(account_id, tenant_b, status="active")

    resolver = CanonicalIdentityResolver(c1_factory)
    ia = await resolver.resolve_by_tenant(tenant_a)
    ib = await resolver.resolve_by_tenant(tenant_b)
    assert ia.account_id == account_id == ib.account_id
    assert ia.conversation_id != ib.conversation_id
    assert ia.conversation_id == first["conversation"]["id"]
    assert ib.conversation_id == second["id"]

    # 账号级枚举两个 agent（确定性：created_at / id 升序 = tenant_a 在前）。
    agents = await resolver.list_agents(account_id)
    assert [a.tenant_id for a in agents] == [tenant_a, tenant_b]
    assert [a.conversation_id for a in agents] == [ia.conversation_id, ib.conversation_id]

    # 两个 agent 的 message stream 各自独立：写 A 不出现于 B。
    assert await messages.fetch_messages(tenant_a, ia.conversation_id) == []
    assert await messages.fetch_messages(tenant_b, ib.conversation_id) == []
    await messages.append_message(
        tenant_a, ia.conversation_id, role="user", content="secret-a"
    )
    assert await messages.fetch_messages(tenant_b, ib.conversation_id) == []
    assert await messages.latest_sequence(tenant_a, ia.conversation_id) == 0


# ── 负向：无 binding fail-closed，不落 DEFAULT_TENANT ──────────


async def test_unknown_account_rejected(c1_reset, c1_factory) -> None:
    c1_reset()
    resolver = CanonicalIdentityResolver(c1_factory)
    with pytest.raises(IdentityResolutionError):
        await resolver.list_agents(str(uuid.uuid4()))


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
        await resolver.list_agents(blank)


async def test_default_tenant_is_not_canonical_identity(c1_reset, c1_factory) -> None:
    """DEFAULT_TENANT 不是合法 canonical 身份：无 binding 即拒绝（§5.9.2）。"""
    c1_reset()
    resolver = CanonicalIdentityResolver(c1_factory)
    with pytest.raises(IdentityResolutionError):
        await resolver.resolve_by_tenant("default")


async def test_account_without_agent_is_empty_list(
    c1_reset, c1_factory
) -> None:
    """已知账号但尚无 agent 是合法状态：list_agents 返回空列表，不是默认 tenant。"""
    c1_reset()
    identities = CanonicalIdentityRepository(c1_factory)
    account_id = str(uuid.uuid4())
    await identities.create_account(account_id=account_id, status="active")
    resolver = CanonicalIdentityResolver(c1_factory)
    assert await resolver.list_agents(account_id) == []


# ── 跨租户隔离 ───────────────────────────────────────────────


async def test_cross_tenant_messages_invisible(c1_reset, c1_factory) -> None:
    c1_reset()
    identities, messages = _repos(c1_factory)
    other = await identities.provision_account_with_agent(
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


async def test_same_tenant_on_second_account_rejected(
    c1_reset, c1_factory, c1_pg_url
) -> None:
    """tenant 全局唯一：第二账号不能占用已绑定 tenant（repo 守卫 + DB 唯一约束两层）。"""
    c1_reset()
    identities, _ = _repos(c1_factory)
    other = await identities.create_account(
        account_id=str(uuid.uuid4()), status="active"
    )
    with pytest.raises(TenantAlreadyBoundError):
        await identities.create_agent(other["id"], DEV_TENANT_ID)
    conn = psycopg.connect(c1_pg_url)
    try:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO canonical_conversations (tenant_id, account_id) "
                "VALUES ('dev', %s)",
                (other["id"],),
            )
        conn.rollback()
    finally:
        conn.close()


def test_duplicate_tenant_conversation_rejected(c1_reset, c1_pg_url) -> None:
    """同账号重复建同 tenant 会话仍被唯一约束拒绝（1 tenant 只有 1 conversation）。"""
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
    """会话不可脱离账号、消息不可脱离会话存在（FK RESTRICT / 拒绝）。"""
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
        if kind == "tenant":
            identity = await resolver.resolve_by_tenant(value)
            expected = case["expected"]
            assert identity.account_id == expected["account_id"], case["name"]
            assert identity.tenant_id == expected["tenant_id"], case["name"]
            assert identity.conversation_id == expected["conversation_id"], case["name"]
        else:  # account_id → 账号拥有的 agent 列表
            agents = await resolver.list_agents(value)
            expected = case["expected"]
            assert expected["account_id"] == value, case["name"]
            got = [
                {"tenant_id": a.tenant_id, "conversation_id": a.conversation_id}
                for a in agents
            ]
            assert got == expected["agents"], case["name"]


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
                await resolver.list_agents(value)


def test_contract_fixture_stream_semantics() -> None:
    stream = _load_fixture()["message_stream"]
    assert stream["sequence_start"] == 0
    assert stream["per_conversation_independent"] is True
    assert stream["allocation"] == "single_transaction_atomic"
    assert stream["uniqueness"] == ["conversation_id", "sequence"]
    assert stream["cursor_semantics"] == "after_sequence"
