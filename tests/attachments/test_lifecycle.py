"""C6 AttachmentLifecycle 清理/reconciliation 验收（真 PG + blob store）。

覆盖 spec「orphan 与 missing blob reconciliation」「引用保留与临时清理」场景：
missing 标记、orphan 清理、幂等、已引用不误删。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.attachments.blob_store import AttachmentBlobStore, build_storage_key
from bootstrap.attachments.lifecycle import AttachmentLifecycle, CleanupReport
from bootstrap.db.repository.attachment_repo import AttachmentRepository

from tests.attachments.test_attachment_repo import _insert_canonical_message

pytestmark = pytest.mark.postgres


@pytest.fixture
def att_repo(att_factory: async_sessionmaker) -> AttachmentRepository:
    return AttachmentRepository(att_factory)


@pytest.fixture
def lifecycle(
    att_factory: async_sessionmaker, tmp_path
) -> AttachmentLifecycle:
    repo = AttachmentRepository(att_factory)
    blobs = AttachmentBlobStore(tmp_path / "attachments")
    return AttachmentLifecycle(repo, blobs)


async def test_reconcile_marks_missing(lifecycle, att_repo, att_tenant) -> None:
    row = await att_repo.create_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        size_bytes=10,
        detected_mime="image/png",
        server_ext=".png",
        filename_display="a.png",
        checksum_sha256="0" * 64,
        storage_key=build_storage_key(uuid.uuid4(), ".png"),
        temp_ttl_hours=24,
    )
    await att_repo.commit_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
        referenced_ttl_days=30,
    )
    # blob 从未写入（模拟缺失）→ reconcile 应标记 missing
    report = await lifecycle.reconcile(att_tenant["tenant_id"])
    assert report.marked_missing == 1
    fresh = await att_repo.get_owned(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
    )
    assert fresh is not None and fresh.status == "missing"


async def test_reconcile_removes_orphan_blob(
    lifecycle, att_repo, att_tenant
) -> None:
    # 写入一个无 metadata 的 blob（孤儿）
    orphan_id = uuid.uuid4()
    key = build_storage_key(orphan_id, ".png")
    lifecycle._blobs.stage_bytes(orphan_id, b"orphan")
    lifecycle._blobs.commit(orphan_id, ".png")
    assert lifecycle._blobs.blob_exists(key)
    report = await lifecycle.reconcile(att_tenant["tenant_id"])
    assert report.removed_orphans >= 1
    assert not lifecycle._blobs.blob_exists(key)


async def test_cleanup_idempotent_and_skips_referenced(
    lifecycle, att_repo, att_tenant, att_factory
) -> None:
    # 已引用 → 永不清理
    ref_row = await att_repo.create_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        size_bytes=5,
        detected_mime="image/png",
        server_ext=".png",
        filename_display="ref.png",
        checksum_sha256="1" * 64,
        storage_key=build_storage_key(uuid.uuid4(), ".png"),
        temp_ttl_hours=24,
    )
    m1 = uuid.uuid4()
    await _insert_canonical_message(att_factory, att_tenant, m1)
    await att_repo.add_reference(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=ref_row.id,
        message_id=m1,
        referenced_ttl_days=30,
    )
    # 到期无引用 → 清理
    exp_row = await att_repo.create_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        size_bytes=5,
        detected_mime="image/gif",
        server_ext=".gif",
        filename_display="gif.gif",
        checksum_sha256="2" * 64,
        storage_key=build_storage_key(uuid.uuid4(), ".gif"),
        temp_ttl_hours=24,
    )
    exp_key = exp_row.storage_key
    lifecycle._blobs.stage_bytes(exp_row.id, b"gif")
    lifecycle._blobs.commit(exp_row.id, ".gif")
    # 拨 deadline 到过去
    conn = psycopg.connect(
        "postgresql://nexus:nexus_dev@localhost:5433/nexus_c6test", autocommit=True
    )
    conn.execute(
        "UPDATE attachments SET retention_deadline = now() - interval '1 day' "
        "WHERE id = %s",
        (str(exp_row.id),),
    )
    conn.close()

    cleanup1: CleanupReport = await lifecycle.cleanup_expired()
    assert cleanup1.deleted_metadata == 1
    assert not lifecycle._blobs.blob_exists(exp_key)
    # 已引用项仍在
    fresh = await att_repo.get_owned(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=ref_row.id,
    )
    assert fresh is not None and fresh.referencing_count == 1
    # 幂等：第二轮删除为 0
    cleanup2 = await lifecycle.cleanup_expired()
    assert cleanup2.deleted_metadata == 0