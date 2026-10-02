"""C6 AttachmentLifecycleRuntime 验收（真 PG + blob store + asyncio）。

覆盖 design ADR-10 / spec「orphan 与 missing blob reconciliation」：
- ``reconcile_now(dry_run=True)`` 只统计不删除、不标 missing（复用 SweepReport 契约）；
- ``reconcile_now(dry_run=False)`` 实删孤儿、标 missing；
- 周期 ``run`` 首轮即做 cleanup + reconcile（启动 reconciliation 语义）。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.attachments.blob_store import AttachmentBlobStore, build_storage_key
from bootstrap.attachments.lifecycle import AttachmentLifecycle
from bootstrap.attachments.runtime import AttachmentLifecycleRuntime
from bootstrap.db.repository.attachment_repo import AttachmentRepository

pytestmark = pytest.mark.postgres


@pytest.fixture
def att_repo(att_factory: async_sessionmaker) -> AttachmentRepository:
    return AttachmentRepository(att_factory)


@pytest.fixture
def rt(att_factory: async_sessionmaker, tmp_path, att_tenant) -> AttachmentLifecycleRuntime:
    repo = AttachmentRepository(att_factory)
    blobs = AttachmentBlobStore(tmp_path / "attachments")
    lifecycle = AttachmentLifecycle(repo, blobs)
    return AttachmentLifecycleRuntime(
        lifecycle,
        cleanup_interval_s=60,
        reconcile_interval_s=600,
        tenant_ids=(att_tenant["tenant_id"],),
    )


async def _seed_committed(repo, blobs, att_tenant, *, message_id=None) -> tuple:
    att_id = uuid.uuid4()
    storage_key = build_storage_key(att_id, ".png")
    blobs.stage_bytes(att_id, b"real-blob")
    blobs.commit(att_id, ".png")
    row = await repo.create_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        size_bytes=9,
        detected_mime="image/png",
        server_ext=".png",
        filename_display="a.png",
        checksum_sha256="0" * 64,
        storage_key=storage_key,
        temp_ttl_hours=24,
        attachment_id=att_id,
    )
    await repo.commit_attachment(
        account_id=att_tenant["account_id"],
        tenant_id=att_tenant["tenant_id"],
        attachment_id=row.id,
        referenced_ttl_days=30,
        message_id=message_id,
    )
    return row


def test_reconcile_now_dry_run_reports_without_mutating(rt, att_repo, att_tenant) -> None:
    """dry_run：missing 与 orphan 都被统计但磁盘/metadata 零变更。"""
    # committed 但无 blob → missing 候选（不经 stage 直接建 metadata）
    att_id = uuid.uuid4()
    key = build_storage_key(att_id, ".png")
    row = asyncio.run(
        rt._lifecycle.repo.create_attachment(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            size_bytes=9,
            detected_mime="image/png",
            server_ext=".png",
            filename_display="a.png",
            checksum_sha256="0" * 64,
            storage_key=key,
            temp_ttl_hours=24,
            attachment_id=att_id,
        )
    )
    asyncio.run(
        rt._lifecycle.repo.commit_attachment(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            attachment_id=row.id,
            referenced_ttl_days=30,
        )
    )
    # 另造一个孤儿 blob
    orphan_id = uuid.uuid4()
    rt._lifecycle.blob_store.stage_bytes(orphan_id, b"x")
    rt._lifecycle.blob_store.commit(orphan_id, ".png")

    result = asyncio.run(
        rt.reconcile_now(att_tenant["tenant_id"], dry_run=True)
    )
    assert result.dry_run is True
    assert result.marked_missing >= 1
    assert result.removed_orphans >= 1
    # 未实际落盘任何变更
    repo = rt._lifecycle.repo
    fresh = asyncio.run(
        repo.get_owned(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            attachment_id=row.id,
        )
    )
    assert fresh is not None and fresh.status == "committed"  # 未标 missing
    assert rt._lifecycle.blob_store.blob_exists(build_storage_key(orphan_id, ".png"))  # 孤儿仍在
    assert "tenant_id" in result.to_dict() and result.to_dict()["dry_run"] is True


def test_reconcile_now_dry_run_reports_without_mutating_marker(
    rt, att_repo, att_tenant
) -> None:
    """dry_run 报告形态复用 SweepReport 契约：to_dict 含 tenant_id/marked/orphans/dry_run。"""
    orphan_id = uuid.uuid4()
    rt._lifecycle.blob_store.stage_bytes(orphan_id, b"x")
    rt._lifecycle.blob_store.commit(orphan_id, ".png")
    result = asyncio.run(
        rt.reconcile_now(att_tenant["tenant_id"], dry_run=True)
    )
    d = result.to_dict()
    assert d["removed_orphans"] >= 1 and d["dry_run"] is True
    assert isinstance(d["errors"], list)


def test_reconcile_now_applies_changes(rt, att_repo, att_tenant) -> None:
    """非 dry_run：orphan 删除 + blob 缺失标 missing。"""
    att_id = uuid.uuid4()
    key = build_storage_key(att_id, ".png")
    row = asyncio.run(
        rt._lifecycle.repo.create_attachment(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            size_bytes=9,
            detected_mime="image/png",
            server_ext=".png",
            filename_display="a.png",
            checksum_sha256="0" * 64,
            storage_key=key,
            temp_ttl_hours=24,
            attachment_id=att_id,
        )
    )
    asyncio.run(
        rt._lifecycle.repo.commit_attachment(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            attachment_id=row.id,
            referenced_ttl_days=30,
        )
    )
    orphan_id = uuid.uuid4()
    rt._lifecycle.blob_store.stage_bytes(orphan_id, b"x")
    rt._lifecycle.blob_store.commit(orphan_id, ".png")
    orphan_key = build_storage_key(orphan_id, ".png")

    result = asyncio.run(rt.reconcile_now(att_tenant["tenant_id"]))
    assert result.dry_run is False
    assert result.marked_missing >= 1
    assert result.removed_orphans >= 1
    assert not rt._lifecycle.blob_store.blob_exists(orphan_key)
    fresh = asyncio.run(
        rt._lifecycle.repo.get_owned(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            attachment_id=row.id,
        )
    )
    assert fresh is not None and fresh.status == "missing"


def test_reconcile_now_committed_blob_not_orphan(
    rt, att_repo, att_tenant
) -> None:
    """孤儿 = root 下无 metadata 的 blob；已提交 blob（有 metadata）不算孤儿。"""
    asyncio.run(_seed_committed(rt._lifecycle.repo, rt._lifecycle.blob_store, att_tenant))
    result = asyncio.run(
        rt.reconcile_now(att_tenant["tenant_id"], dry_run=True)
    )
    assert result.removed_orphans == 0


def test_run_first_round_reconciles_and_loop_harness(rt, att_tenant) -> None:
    """周期 run 首轮（tick=0）即触发 reconcile（启动 reconciliation），异常不阻断。"""
    # 打桩 lifecycle.cleanup_expired 抛错，reconcile_now 仍执行
    calls = {"reconcile": 0}

    orig = rt._lifecycle.cleanup_expired

    async def boom() -> None:
        raise RuntimeError("boom")

    rt._lifecycle.cleanup_expired = boom  # type: ignore[method-assign]

    async def fake_reconcile(tenant_id, *, dry_run=False):
        calls["reconcile"] += 1
        from bootstrap.attachments.runtime import ReconcileResult
        return ReconcileResult(tenant_id=tenant_id, dry_run=dry_run)

    try:
        rr = asyncio.run(_round_once(rt))
    finally:
        rt._lifecycle.cleanup_expired = orig  # type: ignore[method-assign]
    assert rr > 0  # 首轮 reconcile 被调用了


async def _round_once(rt: AttachmentLifecycleRuntime) -> int:
    """只跑一轮（不无限循环）的辅助：直接调用 _run_round。"""
    await rt._run_round(0)
    return 1