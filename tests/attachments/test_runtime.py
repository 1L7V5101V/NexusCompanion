"""C6 AttachmentLifecycleRuntime 验收（真 PG + blob store + asyncio）。

覆盖 design ADR-10 / spec「orphan 与 missing blob reconciliation」：
- ``reconcile_now(dry_run)`` 委托 lifecycle 唯一实现，dry_run 只统计不落盘；
- 周期轮按 interval 比例触发对账；
- **生产默认（未显式配置 tenant_ids）每轮对账全部有附件记录的租户**——否则 24h
  staging 清理与孤儿收敛只在启动发生；
- 已入库附件（staged/committed + blob 齐全）在周期轮里不会被删。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.config_models import AttachmentConfig
from bootstrap.attachments.blob_store import AttachmentBlobStore, build_storage_key
from bootstrap.attachments.lifecycle import AttachmentLifecycle, LifecycleReport
from bootstrap.attachments.runtime import AttachmentLifecycleRuntime
from bootstrap.db.repository.attachment_repo import AttachmentRepository

pytestmark = pytest.mark.postgres


@pytest.fixture
def blobs(tmp_path) -> AttachmentBlobStore:
    return AttachmentBlobStore(tmp_path, multi_tenant=True)


def _runtime(
    att_factory: async_sessionmaker, blobs: AttachmentBlobStore, **kw: object
) -> AttachmentLifecycleRuntime:
    lifecycle = AttachmentLifecycle(
        AttachmentRepository(att_factory),
        blobs,
        AttachmentConfig(orphan_grace_seconds=0),
    )
    return AttachmentLifecycleRuntime(
        lifecycle,
        cleanup_interval_s=kw.get("cleanup_interval_s", 60),  # type: ignore[arg-type]
        reconcile_interval_s=kw.get("reconcile_interval_s", 600),  # type: ignore[arg-type]
        tenant_ids=kw.get("tenant_ids", ()),  # type: ignore[arg-type]
    )


@pytest.fixture
def rt(att_factory: async_sessionmaker, blobs: AttachmentBlobStore, att_tenant):
    return _runtime(
        att_factory, blobs, tenant_ids=(att_tenant["tenant_id"],)
    )


async def _seed_uploaded(repo, blobs, att_tenant, *, commit=False):
    """落一份真实附件：blob 在最终路径 + metadata 行（commit=True 推进 committed）。"""
    att_id = uuid.uuid4()
    storage_key = build_storage_key(att_id, ".png")
    blobs.stage_bytes(att_tenant["tenant_id"], att_id, b"real-blob")
    blobs.commit(att_tenant["tenant_id"], att_id, ".png")
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
    if commit:
        await repo.commit_attachment(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            attachment_id=row.id,
            referenced_ttl_days=30,
        )
    return row


def test_reconcile_now_dry_run_reports_without_mutating(
    rt, att_tenant, blobs
) -> None:
    """dry_run：missing 与 orphan 都被统计，但磁盘与 metadata 零变更。"""
    repo = rt._lifecycle.repo
    # committed 但无 blob → missing 候选
    row = asyncio.run(_seed_uploaded(repo, blobs, att_tenant))
    asyncio.run(
        repo.commit_attachment(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            attachment_id=row.id,
            referenced_ttl_days=30,
        )
    )
    blobs.delete_blob(att_tenant["tenant_id"], row.storage_key)  # 模拟 blob 缺失
    # 另造一个孤儿 blob
    orphan_id = uuid.uuid4()
    blobs.stage_bytes(att_tenant["tenant_id"], orphan_id, b"x")
    blobs.commit(att_tenant["tenant_id"], orphan_id, ".png")
    orphan_key = build_storage_key(orphan_id, ".png")

    result = asyncio.run(rt.reconcile_now(att_tenant["tenant_id"], dry_run=True))
    assert result.dry_run is True
    assert result.marked_missing == 1
    assert result.removed_orphans == 1
    # 未落盘任何变更
    fresh = asyncio.run(
        repo.get_owned(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            attachment_id=row.id,
        )
    )
    assert fresh is not None and fresh.status == "committed"  # 未标 missing
    assert blobs.blob_exists(att_tenant["tenant_id"], orphan_key)  # 孤儿仍在
    d = result.to_dict()
    assert "tenant_ids" in d and d["dry_run"] is True and isinstance(d["errors"], list)


def test_reconcile_now_applies_changes(rt, att_tenant, blobs) -> None:
    """非 dry_run：orphan 删除 + blob 缺失标 missing。"""
    repo = rt._lifecycle.repo
    row = asyncio.run(_seed_uploaded(repo, blobs, att_tenant))
    asyncio.run(
        repo.commit_attachment(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            attachment_id=row.id,
            referenced_ttl_days=30,
        )
    )
    blobs.delete_blob(att_tenant["tenant_id"], row.storage_key)
    orphan_id = uuid.uuid4()
    blobs.stage_bytes(att_tenant["tenant_id"], orphan_id, b"x")
    blobs.commit(att_tenant["tenant_id"], orphan_id, ".png")
    orphan_key = build_storage_key(orphan_id, ".png")

    result = asyncio.run(rt.reconcile_now(att_tenant["tenant_id"]))
    assert result.dry_run is False
    assert result.marked_missing == 1
    assert result.removed_orphans == 1
    assert not blobs.blob_exists(att_tenant["tenant_id"], orphan_key)
    fresh = asyncio.run(
        repo.get_owned(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            attachment_id=row.id,
        )
    )
    assert fresh is not None and fresh.status == "missing"


def test_uploaded_attachment_survives_roundtrip(
    att_factory: async_sessionmaker, blobs: AttachmentBlobStore, att_tenant
) -> None:
    """端到端回归：Service.upload → 对账 → 仍能取回（原实现在此删除自己的附件）。"""
    from bootstrap.attachments.service import AttachmentService

    repo = AttachmentRepository(att_factory)
    config = AttachmentConfig()
    svc = AttachmentService(repo, blobs, config)

    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="PNG")
    png = buf.getvalue()

    result = asyncio.run(
        svc.upload(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            filename="pic.png",
            data=png,
        )
    )
    rec = asyncio.run(
        repo.get_owned(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            attachment_id=result.attachment_id,
        )
    )
    assert rec is not None

    rt = _runtime(att_factory, blobs)
    # 启动轮 + 周期轮各跑一次，附件都必须还在
    asyncio.run(rt._run_round(0))
    asyncio.run(rt._run_round(1))
    assert blobs.blob_exists(att_tenant["tenant_id"], rec.storage_key), (
        "刚上传的附件被对账判为孤儿删除"
    )
    _record, path = asyncio.run(
        svc.fetch(
            account_id=att_tenant["account_id"],
            tenant_id=att_tenant["tenant_id"],
            attachment_id=result.attachment_id,
        )
    )
    assert path.read_bytes() == png


def test_periodic_round_covers_all_tenants_when_unset(
    att_factory: async_sessionmaker, blobs: AttachmentBlobStore
) -> None:
    """生产默认不配 tenant_ids：周期轮（tick>0）也要覆盖所有有附件记录的租户。"""
    repo = AttachmentRepository(att_factory)
    seen: list[str] = []

    async def _create(tenant: str) -> None:
        await repo.create_attachment(
            account_id="00000000-0000-0000-0000-000000000001",
            tenant_id=tenant,
            size_bytes=1,
            detected_mime="text/plain",
            server_ext=".txt",
            filename_display="a.txt",
            checksum_sha256="0" * 64,
            storage_key=build_storage_key(uuid.uuid4(), ".txt"),
            temp_ttl_hours=24,
        )

    asyncio.run(_create("tenant-a"))
    asyncio.run(_create("tenant-b"))

    rt = _runtime(att_factory, blobs, reconcile_interval_s=60)
    orig = rt._lifecycle.reconcile

    async def spy(tenant_id: str, *, dry_run: bool = False) -> LifecycleReport:
        seen.append(tenant_id)
        return await orig(tenant_id, dry_run=dry_run)

    rt._lifecycle.reconcile = spy  # type: ignore[method-assign]
    try:
        asyncio.run(rt._run_round(1))  # tick>0 的周期轮
    finally:
        rt._lifecycle.reconcile = orig  # type: ignore[method-assign]
    assert set(seen) == {"tenant-a", "tenant-b"}


def test_run_first_round_reconciles_and_loop_harness(
    att_factory: async_sessionmaker, blobs: AttachmentBlobStore, att_tenant
) -> None:
    """周期 run 首轮（tick=0）即触发对账（启动 reconciliation），cleanup 异常不阻断。"""
    rt = _runtime(att_factory, blobs, tenant_ids=(att_tenant["tenant_id"],))
    calls = {"reconcile": 0}
    orig_cleanup = rt._lifecycle.cleanup_expired
    orig_reconcile = rt._lifecycle.reconcile

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    async def counting_reconcile(tenant_id: str, *, dry_run: bool = False):
        calls["reconcile"] += 1
        return LifecycleReport(tenant_ids=(tenant_id,), dry_run=dry_run)

    rt._lifecycle.cleanup_expired = boom  # type: ignore[method-assign]
    rt._lifecycle.reconcile = counting_reconcile  # type: ignore[method-assign]
    try:
        asyncio.run(rt._run_round(0))
    finally:
        rt._lifecycle.cleanup_expired = orig_cleanup  # type: ignore[method-assign]
        rt._lifecycle.reconcile = orig_reconcile  # type: ignore[method-assign]
    assert calls["reconcile"] == 1
