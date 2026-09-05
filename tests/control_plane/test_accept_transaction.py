"""T1 入站接受事务验收：原子性、回滚无半写入、双键幂等、并发去重、inbox 收束。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.repository.canonical_repo import CanonicalMessageRepository
from bootstrap.db.repository.control_plane_repo import (
    IngressRepository,
    NotFoundError,
    TurnControlRepository,
)

from tests.control_plane.conftest import DEV_ACCOUNT_ID, DEV_CONVERSATION_ID, DEV_TENANT_ID

pytestmark = pytest.mark.postgres

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "control_plane_idempotency.json"


@pytest.fixture
def ingress(c2_factory: async_sessionmaker) -> IngressRepository:
    return IngressRepository(c2_factory)


@pytest.fixture
def messages(c2_factory: async_sessionmaker) -> CanonicalMessageRepository:
    return CanonicalMessageRepository(c2_factory)


def _counts(c2_pg_url: str) -> dict[str, int]:
    from tests.control_plane.conftest import C2_TABLES

    import psycopg

    conn = psycopg.connect(c2_pg_url)
    try:
        out: dict[str, int] = {}
        for table in (*C2_TABLES, "canonical_messages"):
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out
    finally:
        conn.close()


def _next_sequence(c2_pg_url: str, conversation_id: str) -> int:
    import psycopg

    conn = psycopg.connect(c2_pg_url)
    try:
        return conn.execute(
            "SELECT next_sequence FROM canonical_conversations WHERE id = %s",
            (conversation_id,),
        ).fetchone()[0]
    finally:
        conn.close()


async def test_accept_commits_all_in_one_transaction(
    make_tenant, ingress: IngressRepository, messages: CanonicalMessageRepository
) -> None:
    """接受成功 = dedupe + canonical user message + inbox + queued turn 同事务产物。"""
    tenant = await make_tenant()
    work = await ingress.accept_inbound(
        tenant["tenant_id"],
        tenant["conversation_id"],
        source_channel="telegram",
        source_identity_id="u-1",
        source_message_id="m-1",
        content="你好",
        metadata={"origin": "test"},
        work_items=[{"work_kind": "consolidation", "idempotency_key": "cons-1"}],
    )
    assert not work.duplicate
    assert work.sequence == 0
    assert work.turn_id is not None
    assert len(work.work_item_ids) == 1

    inbox = await ingress.get_inbox(tenant["tenant_id"], work.inbox_id)
    assert inbox is not None
    assert inbox["status"] == "accepted"
    assert inbox["canonical_message_id"] == work.message_id

    stream = await messages.fetch_messages(tenant["tenant_id"], tenant["conversation_id"])
    assert [m["sequence"] for m in stream] == [0]
    assert stream[0]["role"] == "user"
    assert stream[0]["content"] == "你好"

    turn = await _read_turn(ingress, tenant["tenant_id"], work.turn_id)
    assert turn is not None and turn["status"] == "queued"
    assert turn["inbox_record_id"] == work.inbox_id


async def _read_turn(
    ingress: IngressRepository, tenant_id: str, turn_id: str | None
) -> dict[str, Any] | None:
    if turn_id is None:
        return None
    control = TurnControlRepository(ingress._sf)  # type: ignore[arg-type]  # 测试同库复用 factory
    return await control.get_turn(tenant_id, turn_id)


async def test_accept_without_conversation_rejected_zero_writes(
    make_tenant, c2_reset, ingress: IngressRepository, c2_pg_url
) -> None:
    """未知规范会话 fail-closed：拒绝且所有相关表零写入（含 dedupe）。"""
    c2_reset()
    fake_conv = "00000000-0000-0000-0000-c0ffee000001"
    with pytest.raises(NotFoundError):
        await ingress.accept_inbound(
            DEV_TENANT_ID,
            fake_conv,
            account_id=DEV_ACCOUNT_ID,
            client_message_id="cm-orphan",
            content="hello",
        )
    counts = _counts(c2_pg_url)
    assert counts["message_deduplication_keys"] == 0
    assert counts["inbox_records"] == 0
    assert counts["turns"] == 0
    assert counts["canonical_messages"] == 0


async def test_accept_rollback_no_half_writes(
    make_tenant, c2_reset, ingress: IngressRepository, c2_factory, c2_pg_url
) -> None:
    """事务中途失败无半写入：跨租户 work item 幂等键碰撞 fail-closed → 全回滚。"""
    c2_reset()
    tenant_a = await make_tenant(prefix="c2a")
    tenant_b = await make_tenant(prefix="c2b")

    from bootstrap.db.repository.control_plane_repo import TurnControlRepository

    control = TurnControlRepository(c2_factory)
    await control.create_work_item(
        tenant_a["tenant_id"],
        "consolidation",
        conversation_id=tenant_a["conversation_id"],
        idempotency_key="collide-key",
    )
    before = _counts(c2_pg_url)

    with pytest.raises(NotFoundError):
        await ingress.accept_inbound(
            tenant_b["tenant_id"],
            tenant_b["conversation_id"],
            account_id=tenant_b["account_id"],
            client_message_id="cm-rollback",
            content="hello",
            work_items=[{"work_kind": "consolidation", "idempotency_key": "collide-key"}],
        )
    after = _counts(c2_pg_url)
    assert after["message_deduplication_keys"] == before["message_deduplication_keys"]
    assert after["inbox_records"] == before["inbox_records"]
    assert after["turns"] == before["turns"]
    assert after["canonical_messages"] == before["canonical_messages"]
    assert after["background_work_items"] == before["background_work_items"]
    assert _next_sequence(c2_pg_url, tenant_b["conversation_id"]) == 0


async def test_accept_key_validation(
    make_tenant, ingress: IngressRepository
) -> None:
    """幂等键二选一：两类同给 / 都不给 / 键不完整 → ValueError，不触库。"""
    tenant = await make_tenant()
    with pytest.raises(ValueError):
        await ingress.accept_inbound(
            tenant["tenant_id"],
            tenant["conversation_id"],
            content="both keys",
            source_channel="telegram",
            source_identity_id="u",
            source_message_id="m",
            account_id=tenant["account_id"],
            client_message_id="cm",
        )
    with pytest.raises(ValueError):
        await ingress.accept_inbound(
            tenant["tenant_id"], tenant["conversation_id"], content="no key"
        )
    with pytest.raises(ValueError):
        await ingress.accept_inbound(
            tenant["tenant_id"],
            tenant["conversation_id"],
            source_channel="telegram",
            source_message_id="m",
        )


@pytest.mark.parametrize(
    "channel", ["telegram", "webchat"], ids=["telegram_key", "webchat_key"]
)
async def test_idempotency_contract_from_fixture(
    make_tenant,
    ingress: IngressRepository,
    c2_pg_url,
    c2_reset,
    channel: str,
) -> None:
    """消费 tests/fixtures/control_plane_idempotency.json 双键正/负用例（C4/C10 复用）。"""
    c2_reset()
    tenant = await make_tenant()
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    spec = fixture["inbound_idempotency_keys"][channel]
    expected_accepts = 0
    for case in spec["cases"]:
        key: dict[str, Any] = dict(case["key"])
        if channel == "webchat":
            key["account_id"] = tenant["account_id"]
        result = await ingress.accept_inbound(
            tenant["tenant_id"], tenant["conversation_id"], content="hi", **key
        )
        assert result.duplicate is case["expect_duplicate"], case["name"]
        if not case["expect_duplicate"]:
            expected_accepts += 1
    assert _counts(c2_pg_url)["canonical_messages"] == expected_accepts


async def test_concurrent_same_key_single_accept(
    make_tenant, c2_reset, ingress: IngressRepository, c2_pg_url
) -> None:
    """并发同键注入只产生一条（数据库唯一约束兜底，非内存去重）。"""
    c2_reset()
    tenant = await make_tenant()
    results = await asyncio.gather(
        *(
            ingress.accept_inbound(
                tenant["tenant_id"],
                tenant["conversation_id"],
                account_id=tenant["account_id"],
                client_message_id="cm-race",
                content=f"race-{i}",
            )
            for i in range(8)
        )
    )
    accepted = [r for r in results if not r.duplicate]
    duplicates = [r for r in results if r.duplicate]
    assert len(accepted) == 1
    assert len(duplicates) == 7
    first = accepted[0]
    for dup in duplicates:
        assert dup.message_id == first.message_id
        assert dup.inbox_id == first.inbox_id
    counts = _counts(c2_pg_url)
    assert counts["canonical_messages"] == 1
    assert counts["inbox_records"] == 1
    assert counts["message_deduplication_keys"] == 1
    assert counts["turns"] == 1
    assert _next_sequence(c2_pg_url, tenant["conversation_id"]) == 1


async def test_mark_inbox_processed_idempotent(
    make_tenant, ingress: IngressRepository
) -> None:
    """accepted → processed（幂等收束）；processed ≠ channel 已展示。"""
    tenant = await make_tenant()
    accepted = await ingress.accept_inbound(
        tenant["tenant_id"],
        tenant["conversation_id"],
        account_id=tenant["account_id"],
        client_message_id="cm-close",
    )
    first = await ingress.mark_inbox_processed(tenant["tenant_id"], accepted.inbox_id)
    assert first["status"] == "processed"
    assert first["processed_at"]
    second = await ingress.mark_inbox_processed(tenant["tenant_id"], accepted.inbox_id)
    assert second["status"] == "processed"
    assert second["processed_at"] == first["processed_at"]


async def test_cross_tenant_inbox_invisible(
    make_tenant, ingress: IngressRepository
) -> None:
    """跨租户查询 inbox → 不可见（租户隔离）。"""
    tenant_a = await make_tenant(prefix="c2x")
    tenant_b = await make_tenant(prefix="c2y")
    accepted = await ingress.accept_inbound(
        tenant_a["tenant_id"],
        tenant_a["conversation_id"],
        account_id=tenant_a["account_id"],
        client_message_id="cm-iso",
    )
    assert await ingress.get_inbox(tenant_b["tenant_id"], accepted.inbox_id) is None
    with pytest.raises(NotFoundError):
        await ingress.mark_inbox_processed(tenant_b["tenant_id"], accepted.inbox_id)


async def test_webchat_key_scoped_by_account(
    make_tenant, ingress: IngressRepository
) -> None:
    """同 client_message_id 不同账号互不冲突（键以 account_id 为界）。"""
    tenant_a = await make_tenant(prefix="c2m")
    tenant_b = await make_tenant(prefix="c2n")
    ra = await ingress.accept_inbound(
        tenant_a["tenant_id"],
        tenant_a["conversation_id"],
        account_id=tenant_a["account_id"],
        client_message_id="cm-shared",
    )
    rb = await ingress.accept_inbound(
        tenant_b["tenant_id"],
        tenant_b["conversation_id"],
        account_id=tenant_b["account_id"],
        client_message_id="cm-shared",
    )
    assert not ra.duplicate and not rb.duplicate
    assert ra.message_id != rb.message_id


async def test_dev_seed_identity_acceptable(
    c2_factory, c2_reset, ingress: IngressRepository, messages: CanonicalMessageRepository
) -> None:
    """C1 dev seed 身份可直接走 T1（P0.5 dev 闭环 seam）。"""
    c2_reset()
    accepted = await ingress.accept_inbound(
        DEV_TENANT_ID,
        DEV_CONVERSATION_ID,
        account_id=DEV_ACCOUNT_ID,
        client_message_id="cm-dev",
        content="dev hello",
    )
    assert not accepted.duplicate and accepted.sequence == 0
    stream = await messages.fetch_messages(DEV_TENANT_ID, DEV_CONVERSATION_ID)
    assert len(stream) == 1
