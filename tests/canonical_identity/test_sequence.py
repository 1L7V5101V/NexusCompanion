"""并发 sequence 分配验收：唯一、连续、0-based、跨会话独立、失败不留空洞（§5.9.2）。"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.repository.canonical_repo import (
    CanonicalConversationNotFoundError,
    CanonicalIdentityRepository,
    CanonicalMessageRepository,
)
from tests.canonical_identity.conftest import (
    DEV_CONVERSATION_ID,
    DEV_TENANT_ID,
)

pytestmark = pytest.mark.postgres

CONCURRENCY = 20


def _repos(
    factory: async_sessionmaker,
) -> tuple[CanonicalIdentityRepository, CanonicalMessageRepository]:
    return CanonicalIdentityRepository(factory), CanonicalMessageRepository(factory)


async def test_first_message_sequence_is_zero(c1_reset, c1_factory) -> None:
    c1_reset()
    _, messages = _repos(c1_factory)
    row = await messages.append_message(
        DEV_TENANT_ID, DEV_CONVERSATION_ID, role="user", content="hello"
    )
    assert row["sequence"] == 0
    assert await messages.latest_sequence(DEV_TENANT_ID, DEV_CONVERSATION_ID) == 0


async def test_concurrent_sequence_allocation(c1_reset, c1_factory) -> None:
    """多并发插入下 (conversation_id, sequence) 唯一、连续、0-based、跨会话独立。"""
    c1_reset()
    identities, messages = _repos(c1_factory)
    conv_b = await identities.create_account_with_conversation(
        f"seq_b_{uuid.uuid4().hex[:8]}", status="active"
    )
    conv_b_id = conv_b["conversation"]["id"]

    async def _append(conversation_id: str, tag: str, index: int) -> int:
        row = await messages.append_message(
            (
                DEV_TENANT_ID
                if conversation_id == DEV_CONVERSATION_ID
                else conv_b["conversation"]["tenant_id"]
            ),
            conversation_id,
            role="user",
            content=f"{tag}-{index}",
        )
        return row["sequence"]

    tenant_b = conv_b["conversation"]["tenant_id"]

    async def _run_a() -> list[int]:
        return await asyncio.gather(
            *[_append(DEV_CONVERSATION_ID, "A", i) for i in range(CONCURRENCY)]
        )

    async def _run_b() -> list[int]:
        return await asyncio.gather(
            *[_append(conv_b_id, "B", j) for j in range(CONCURRENCY)]
        )

    seq_a, seq_b = await asyncio.gather(_run_a(), _run_b())
    assert sorted(seq_a) == list(
        range(CONCURRENCY)
    ), f"A 序号非 0..N-1: {sorted(seq_a)}"
    assert sorted(seq_b) == list(
        range(CONCURRENCY)
    ), f"B 序号非 0..N-1: {sorted(seq_b)}"
    assert len(set(seq_a)) == CONCURRENCY and len(set(seq_b)) == CONCURRENCY

    rows_a = await messages.fetch_messages(DEV_TENANT_ID, DEV_CONVERSATION_ID)
    rows_b = await messages.fetch_messages(tenant_b, conv_b_id)
    assert [r["sequence"] for r in rows_a] == list(range(CONCURRENCY))
    assert [r["sequence"] for r in rows_b] == list(range(CONCURRENCY))

    assert (
        await messages.latest_sequence(DEV_TENANT_ID, DEV_CONVERSATION_ID)
        == CONCURRENCY - 1
    )
    assert await messages.latest_sequence(tenant_b, conv_b_id) == CONCURRENCY - 1


async def test_failed_append_leaves_no_hole(c1_reset, c1_factory) -> None:
    """取号后写行失败 → 事务整体回滚，计数器保持原值，序号无空洞。"""
    c1_reset()
    _, messages = _repos(c1_factory)
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await messages.append_message(
            DEV_TENANT_ID, DEV_CONVERSATION_ID, role="not-a-role", content="x"
        )
    row = await messages.append_message(
        DEV_TENANT_ID, DEV_CONVERSATION_ID, role="user", content="after-failure"
    )
    assert row["sequence"] == 0, "失败分配必须回滚，下一次追加应重用序号 0"
    assert await messages.latest_sequence(DEV_TENANT_ID, DEV_CONVERSATION_ID) == 0


async def test_append_unknown_conversation_fails_closed(c1_reset, c1_factory) -> None:
    c1_reset()
    _, messages = _repos(c1_factory)
    ghost = uuid.uuid4()
    with pytest.raises(CanonicalConversationNotFoundError):
        await messages.append_message(DEV_TENANT_ID, ghost, role="user", content="x")
    # tenant 不匹配同样拒绝（fail-closed，不泄露会话存在性）。
    with pytest.raises(CanonicalConversationNotFoundError):
        await messages.append_message(
            "other-tenant", DEV_CONVERSATION_ID, role="user", content="x"
        )
    assert await messages.fetch_messages(DEV_TENANT_ID, DEV_CONVERSATION_ID) == []


async def test_cursor_fetch_after_sequence(c1_reset, c1_factory) -> None:
    """断线补拉游标：after_sequence=K 返回 K+1 起的升序消息。"""
    c1_reset()
    _, messages = _repos(c1_factory)
    for i in range(5):
        await messages.append_message(
            DEV_TENANT_ID, DEV_CONVERSATION_ID, role="user", content=f"m{i}"
        )
    rows = await messages.fetch_messages(
        DEV_TENANT_ID, DEV_CONVERSATION_ID, after_sequence=2
    )
    assert [r["sequence"] for r in rows] == [3, 4]
    rows = await messages.fetch_messages(DEV_TENANT_ID, DEV_CONVERSATION_ID, limit=2)
    assert [r["sequence"] for r in rows] == [0, 1]
