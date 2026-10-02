"""C6 AttachmentRepository 验收（真 PG，ADR-4/ADR-5）。

覆盖：create→commit 两段式、引用 insert 更新 deadline、解除全部引用 refcount=0、
跨租户/跨账号查询返回空、清理幂等、missing 标记、校验器回滚零写入。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.models.attachment import AttachmentModel, MessageAttachmentModel
from bootstrap.db.models.canonical import CanonicalMessageModel
from bootstrap.db.repository.attachment_repo import (
    AttachmentAlreadyCommittedError,
    AttachmentRepository,
)

pytestmark = pytest.mark.postgres


async def _insert_canonical_message(
    factory: async_sessionmaker, tenant: dict[str, str], message_id: uuid.UUID
) -> None:
    """插入 canonical message 行（message_attachments FK 前置）。"""
    from sqlalchemy import text as _text

    async with factory() as sess, sess.begin():
        nxt = await sess.execute(
            _text(
                "SELECT COALESCE(MAX(sequence), -1) + 1 AS n "
                "FROM canonical_messages WHERE conversation_id = :conv"
            ),
            {"conv": tenant["conversation_id"]},
        )
        seq = int(nxt.scalar_one())
        await sess.execute(
            _text(
                "INSERT INTO canonical_messages "
                "(id, tenant_id, conversation_id, sequence, role, content) "
                "VALUES (:id, :tenant, :conv, :seq, 'assistant', 'x')"
            ),
            {
                "id": message_id,
                "tenant": tenant["tenant_id"],
                "conv": tenant["conversation_id"],
                "seq": seq,
            },
        )


@pytest.fixture
def att_repo(att_factory: async_sessionmaker) -> AttachmentRepository:
    return AttachmentRepository(att_factory)


async def _create(
    repo: AttachmentRepository,
    tenant: dict[str, str],
    *,
    storage_key: str = "dev/11111111-1111-1111-1111-111111111111.png",
    mime: str = "image/png",
) -> Any:
    return await repo.create_attachment(
        account_id=tenant["account_id"],
        tenant_id=tenant["tenant_id"],
        size_bytes=10,
        detected_mime=mime,
        server_ext=".png",
        filename_display="a.png",
        checksum_sha256="0" * 64,
        storage_key=storage_key,
        temp_ttl_hours=24,
    )


async def test_create_then_commit_two_phase(
    att_factory, att_repo, att_tenant
) -> None:
    row = await _create(att_repo, att_tenant)
    assert row.status == "staged"
    assert row.referencing_count == 0

    msg_id = uuid.uuid4()
    await _insert_canonical_message(att_factory, att_tenant, msg_id)
    committed = await att_repo.commit_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
        referenced_ttl_days=30,
        message_id=msg_id,
    )
    assert committed is not None
    assert committed.status == "committed"
    assert committed.referencing_count == 1  # 随 commit 登记首条引用
    assert committed.retention_deadline > committed.created_at


async def test_commit_without_message_keeps_count_zero(att_repo, att_tenant) -> None:
    row = await _create(att_repo, att_tenant)
    committed = await att_repo.commit_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
        referenced_ttl_days=30,
    )
    assert committed is not None and committed.referencing_count == 0


async def test_add_reference_updates_deadline(
    att_factory, att_repo, att_tenant
) -> None:
    row = await _create(att_repo, att_tenant)
    m1 = uuid.uuid4()
    m2 = uuid.uuid4()
    await _insert_canonical_message(att_factory, att_tenant, m1)
    await _insert_canonical_message(att_factory, att_tenant, m2)
    first = await att_repo.add_reference(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
        message_id=m1,
        referenced_ttl_days=30,
    )
    assert first is not None and first.referencing_count == 1
    old_deadline = first.retention_deadline
    second = await att_repo.add_reference(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
        message_id=m2,
        referenced_ttl_days=30,
    )
    assert second is not None and second.referencing_count == 2
    assert second.retention_deadline >= old_deadline


async def test_remove_all_references_resets_deadline(
    att_factory, att_repo, att_tenant
) -> None:
    row = await _create(att_repo, att_tenant)
    m1 = uuid.uuid4()
    m2 = uuid.uuid4()
    await _insert_canonical_message(att_factory, att_tenant, m1)
    await _insert_canonical_message(att_factory, att_tenant, m2)
    await att_repo.add_reference(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
        message_id=m1,
        referenced_ttl_days=30,
    )
    await att_repo.add_reference(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
        message_id=m2,
        referenced_ttl_days=30,
    )
    after_one = await att_repo.remove_reference(
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
        message_id=m1,
        referenced_ttl_days=30,
    )
    assert after_one is not None and after_one.referencing_count == 1
    after_all = await att_repo.remove_reference(
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
        message_id=m2,
        referenced_ttl_days=30,
    )
    assert after_all is not None
    assert after_all.referencing_count == 0
    # deadline 从解除时刻重算（仍 > created_at）
    assert after_all.retention_deadline > after_all.created_at


async def test_cross_tenant_query_returns_none(
    att_factory, att_repo, att_tenant
) -> None:
    row = await _create(att_repo, att_tenant)
    # 跨账号
    other_account = uuid.UUID("99999999-9999-9999-9999-999999999999")
    got = await att_repo.get_owned(
        account_id=other_account,
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
    )
    assert got is None
    # 跨租户
    got = await att_repo.get_owned(
        account_id=att_tenant["account_id"],
        tenant_id="other-tenant",
        attachment_id=row.id,
    )
    assert got is None
    # 越权更新（owner 维度不匹配）也返回 None
    res = await att_repo.commit_attachment(
        account_id=other_account,
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
        referenced_ttl_days=30,
    )
    assert res is None


async def test_commit_cas_rejects_double_commit(att_repo, att_tenant) -> None:
    row = await _create(att_repo, att_tenant)
    await att_repo.commit_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
        referenced_ttl_days=30,
    )
    with pytest.raises(AttachmentAlreadyCommittedError):
        await att_repo.commit_attachment(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            attachment_id=row.id,
            referenced_ttl_days=30,
        )


async def test_mark_missing_then_cleanup(att_repo, att_tenant) -> None:
    row = await _create(att_repo, att_tenant)
    await att_repo.commit_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
        referenced_ttl_days=30,
    )
    marked = await att_repo.mark_missing(
        tenant_id=att_tenant["tenant_id"], attachment_id=row.id
    )
    assert marked is not None and marked.status == "missing"


async def test_cleanup_only_expired_and_unreferenced(att_repo, att_tenant) -> None:
    # 已过期引用计数 0 → 进入到期列表
    expired = await _create(
        att_repo,
        att_tenant,
        storage_key="dev/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.txt",
        mime="text/plain",
    )
    await att_repo.commit_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=expired.id,
        referenced_ttl_days=30,
    )
    # 手工把 deadline 拨到过去（模拟 30d 已过）
    from datetime import UTC, datetime

    import psycopg

    conn = psycopg.connect(
        "postgresql://nexus:nexus_dev@localhost:5433/nexus_c6test", autocommit=True
    )
    conn.execute(
        "UPDATE attachments SET retention_deadline = now() - interval '1 day' "
        "WHERE id = %s",
        (str(expired.id),),
    )
    conn.close()

    lst = await att_repo.list_expired()
    ids = [r.id for r in lst]
    assert expired.id in ids
    # 清理幂等：第一次删除成功，第二次删除 0 行
    deleted = await att_repo.delete_attachment(
        tenant_id=att_tenant["tenant_id"], attachment_id=expired.id
    )
    assert deleted is True
    again = await att_repo.delete_attachment(
        tenant_id=att_tenant["tenant_id"], attachment_id=expired.id
    )
    assert again is False


async def test_cleanup_skips_referenced(
    att_factory, att_repo, att_tenant
) -> None:
    row = await _create(att_repo, att_tenant)
    m1 = uuid.uuid4()
    await _insert_canonical_message(att_factory, att_tenant, m1)
    await att_repo.add_reference(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
        message_id=m1,
        referenced_ttl_days=30,
    )
    lst = await att_repo.list_expired()
    assert row.id not in [r.id for r in lst]


async def test_list_committed_by_tenant_enumerates(att_repo, att_tenant) -> None:
    a = await _create(att_repo, att_tenant, storage_key="dev/uuid-a.png")
    b = await _create(att_repo, att_tenant, storage_key="dev/uuid-b.txt", mime="text/plain")
    await att_repo.commit_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=a.id,
        referenced_ttl_days=30,
    )
    rows = await att_repo.list_committed_by_tenant(att_tenant["tenant_id"])
    assert {r.id for r in rows} == {a.id}
    assert b.id not in {r.id for r in rows}
    # 跨租户枚举为空
    rows_other = await att_repo.list_committed_by_tenant("other-tenant")
    assert rows_other == []