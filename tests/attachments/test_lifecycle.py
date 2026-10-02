"""C6 AttachmentLifecycle 清理/reconciliation 验收（真 PG + blob store）。

覆盖 spec「orphan 与 missing blob reconciliation」「引用保留与临时清理」场景：
missing 标记、orphan 清理、幂等、已引用不误删，以及关键回归——**「blob 在最终
路径 + metadata 仍为 staged」是上传成功的正常终态，不得被对账判为孤儿删除**。
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.config_models import AttachmentConfig
from bootstrap.attachments.blob_store import AttachmentBlobStore, build_storage_key
from bootstrap.attachments.lifecycle import AttachmentLifecycle, LifecycleReport
from bootstrap.db.repository.attachment_repo import AttachmentRepository

from tests.attachments.test_attachment_repo import _insert_canonical_message

pytestmark = pytest.mark.postgres


@pytest.fixture
def att_repo(att_factory: async_sessionmaker) -> AttachmentRepository:
    return AttachmentRepository(att_factory)


@pytest.fixture
def blobs(tmp_path) -> AttachmentBlobStore:
    return AttachmentBlobStore(tmp_path, multi_tenant=True)


@pytest.fixture
def lifecycle(
    att_factory: async_sessionmaker, blobs: AttachmentBlobStore
) -> AttachmentLifecycle:
    """orphan_grace_seconds=0：让对账在本用例内立即收敛孤儿，无需等待宽限期。"""
    return AttachmentLifecycle(
        AttachmentRepository(att_factory), blobs, AttachmentConfig(orphan_grace_seconds=0)
    )


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


async def test_reconcile_keeps_uploaded_staged_attachment(
    lifecycle, att_repo, att_tenant, blobs
) -> None:
    """上传成功终态（staged metadata + 最终路径 blob）不得被对账删除。"""
    att_id = uuid.uuid4()
    key = build_storage_key(att_id, ".png")
    blobs.stage_bytes(att_tenant["tenant_id"], att_id, b"real-bytes")
    blobs.commit(att_tenant["tenant_id"], att_id, ".png")
    await att_repo.create_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        size_bytes=10,
        detected_mime="image/png",
        server_ext=".png",
        filename_display="a.png",
        checksum_sha256="0" * 64,
        storage_key=key,
        temp_ttl_hours=24,
    )
    report = await lifecycle.reconcile(att_tenant["tenant_id"])
    assert report.removed_orphans == 0
    assert report.marked_missing == 0
    assert blobs.blob_exists(att_tenant["tenant_id"], key), "在库附件被对账误删"


async def test_reconcile_removes_orphan_blob(lifecycle, blobs, att_tenant) -> None:
    """无 metadata 行的 blob（崩溃窗口）由对账收敛删除。"""
    tenant = att_tenant["tenant_id"]
    orphan_id = uuid.uuid4()
    blobs.stage_bytes(tenant, orphan_id, b"orphan")
    blobs.commit(tenant, orphan_id, ".png")
    key = build_storage_key(orphan_id, ".png")
    assert blobs.blob_exists(tenant, key)
    report = await lifecycle.reconcile(tenant)
    assert report.removed_orphans == 1
    assert not blobs.blob_exists(tenant, key)


async def test_reconcile_dry_run_reports_without_mutating(
    lifecycle, blobs, att_tenant
) -> None:
    tenant = att_tenant["tenant_id"]
    orphan_id = uuid.uuid4()
    blobs.stage_bytes(tenant, orphan_id, b"orphan")
    blobs.commit(tenant, orphan_id, ".png")
    key = build_storage_key(orphan_id, ".png")

    report: LifecycleReport = await lifecycle.reconcile(tenant, dry_run=True)
    assert report.dry_run is True
    assert report.removed_orphans == 1
    assert blobs.blob_exists(tenant, key), "dry_run 不得删除任何文件"
    assert report.to_dict()["dry_run"] is True


async def test_reconcile_does_not_touch_other_tenant(
    att_factory: async_sessionmaker, blobs: AttachmentBlobStore, att_tenant
) -> None:
    """跨租户互不侵犯（复盘缺陷的守卫）：A 的对账不得删 B 的在库附件。

    即便 B 的记录是 staged（上传成功终态）、A 自己一条 metadata 都没有。
    """
    b_id = uuid.uuid4()
    b_key = build_storage_key(b_id, ".png")
    blobs.stage_bytes("tenant-b", b_id, b"b-bytes")
    blobs.commit("tenant-b", b_id, ".png")

    lifecycle = AttachmentLifecycle(
        AttachmentRepository(att_factory),
        blobs,
        AttachmentConfig(orphan_grace_seconds=0),
    )
    report = await lifecycle.reconcile("tenant-a")
    assert report.removed_orphans == 0
    assert blobs.blob_exists("tenant-b", b_key), "A 的对账删除了 B 的 blob"
    assert blobs.read_bytes("tenant-b", b_key) == b"b-bytes"


async def test_cleanup_emits_delete_events(
    att_factory: async_sessionmaker, blobs: AttachmentBlobStore, att_tenant
) -> None:
    """delete.finished 在生产路径可达（复盘发现该记录点此前无调用方）。"""
    events: list[dict[str, object]] = []

    class _SpyTelemetry:
        def delete_finished(self, *, tenant_id: str, status: str, **kw: object) -> None:
            events.append({"kind": "delete", "tenant_id": tenant_id, "status": status})

        def cleanup_finished(
            self,
            *,
            event_name: str,
            deleted: int,
            removed_orphans: int,
            marked_missing: int,
            tenants: tuple[str, ...],
        ) -> None:
            events.append({"kind": "cleanup", "deleted": deleted})

    repo = AttachmentRepository(att_factory)
    att_id = uuid.uuid4()
    row = await repo.create_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        size_bytes=3,
        detected_mime="text/plain",
        server_ext=".txt",
        filename_display="a.txt",
        checksum_sha256="3" * 64,
        storage_key=build_storage_key(att_id, ".txt"),
        temp_ttl_hours=24,
        attachment_id=att_id,
    )
    blobs.stage_bytes(att_tenant["tenant_id"], att_id, b"txt")
    blobs.commit(att_tenant["tenant_id"], att_id, ".txt")
    # 拨到期
    conn = psycopg.connect(
        "postgresql://nexus:nexus_dev@localhost:5433/nexus_c6test", autocommit=True
    )
    conn.execute(
        "UPDATE attachments SET retention_deadline = now() - interval '1 hour' WHERE id = %s",
        (str(row.id),),
    )
    conn.close()

    lifecycle = AttachmentLifecycle(
        repo,
        blobs,
        AttachmentConfig(orphan_grace_seconds=0),
        telemetry=_SpyTelemetry(),
    )
    report = await lifecycle.cleanup_expired()
    assert report.deleted_metadata == 1
    kinds = [e["kind"] for e in events]
    assert kinds.count("delete") == 1, "每个到期附件应发一条 delete.finished"
    assert kinds.count("cleanup") == 1


async def test_cleanup_idempotent_and_skips_referenced(
    lifecycle, att_repo, att_tenant, att_factory, blobs
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
    # 到期无引用 → 清理（attachment_id 与 blob 文件名同源，才能真验 blob 被删）
    exp_id = uuid.uuid4()
    exp_row = await att_repo.create_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        size_bytes=5,
        detected_mime="image/gif",
        server_ext=".gif",
        filename_display="gif.gif",
        checksum_sha256="2" * 64,
        storage_key=build_storage_key(exp_id, ".gif"),
        temp_ttl_hours=24,
        attachment_id=exp_id,
    )
    exp_key = exp_row.storage_key
    blobs.stage_bytes(att_tenant["tenant_id"], exp_row.id, b"gif")
    blobs.commit(att_tenant["tenant_id"], exp_row.id, ".gif")
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

    cleanup1 = await lifecycle.cleanup_expired()
    assert cleanup1.deleted_metadata == 1
    assert cleanup1.deleted_blobs == 1, "blob 删除数须真实计数（原实现恒 0）"
    assert not blobs.blob_exists(att_tenant["tenant_id"], exp_key)
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
    assert cleanup2.deleted_blobs == 0
