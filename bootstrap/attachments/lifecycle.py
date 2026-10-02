"""C6 附件生命周期服务（design ADR-5）：清理与 reconciliation 编排。

职责：
- ``cleanup_expired``：staged 超龄（temp_ttl）与 committed/missing 到期且
  referencing_count=0（硬条件）的 metadata + blob 删除，幂等（重复跑删除数=0）；
- ``reconcile``：missing = committed 但 blob 不存在 → status=missing（读取会 404）；
  orphan = blob 存在但无 metadata（含 staging 超龄）→ 删除；
- 全部按 tenant 维度执行；``tenant_filter`` 缺省为全租户扫描（进程内定时任务用）。

事件回调（C12 §8.1 伴随落地）：``on_cleanup`` 返回本轮的 (deleted, removed_orphans,
marked_missing) 供 telemetry 记录点消费（字段 ⊇ 单一来源）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bootstrap.attachments.blob_store import AttachmentBlobStore
from bootstrap.db.repository.attachment_repo import AttachmentRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanupReport:
    deleted_metadata: int
    deleted_blobs: int
    removed_orphans: int
    marked_missing: int
    tenant_ids: tuple[str, ...] = ()


class AttachmentLifecycle:
    def __init__(
        self,
        repo: AttachmentRepository,
        blob_store: AttachmentBlobStore,
        *,
        telemetry: Any | None = None,
    ) -> None:
        self._repo = repo
        self._blobs = blob_store
        self._telemetry = telemetry

    async def cleanup_expired(self) -> CleanupReport:
        """加入期附件清理：staged 超 temp_ttl / committed 到期且 refcount=0。

        幂等论证：metadata 删除后 blob delete 幂等返回 False；下一轮 list_expired
        不再返回（行已删）。重复跑删除数为 0。
        """
        expired = await self._repo.list_expired()
        deleted_meta = 0
        deleted_blobs = 0
        tenants: set[str] = set()
        for record in expired:
            try:
                self._blobs.delete_blob(record.storage_key)
                ok = await self._repo.delete_attachment(
                    tenant_id=record.tenant_id, attachment_id=record.id
                )
                if ok:
                    deleted_meta += 1
                else:
                    deleted_blobs += 0
                tenants.add(record.tenant_id)
            except Exception:
                logger.exception(
                    "attachment cleanup 失败 tenant=%s attachment=%s",
                    record.tenant_id,
                    record.id,
                )
        report = CleanupReport(
            deleted_metadata=deleted_meta,
            deleted_blobs=deleted_blobs,
            removed_orphans=0,
            marked_missing=0,
            tenant_ids=tuple(sorted(tenants)),
        )
        self._emit("cleanup.finished", report)
        return report

    async def reconcile(self, tenant_id: str) -> CleanupReport:
        """reconciliation（启动 + 可手动触发）：missing 标记 + orphan 清理。

        - committed/missing 的 metadata → blob 不存在 → status=missing；
        - blob 存在但不在 committed 列表（无 metadata）→ 删除孤儿；
        - staging 超龄文件 → 随 temp_ttl 清理。
        """
        records = await self._repo.list_committed_by_tenant(tenant_id)
        known_keys = {r.storage_key for r in records}
        marked_missing = 0
        for record in records:
            if not self._blobs.blob_exists(record.storage_key):
                if record.status != "missing":
                    await self._repo.mark_missing(
                        tenant_id=tenant_id, attachment_id=record.id
                    )
                    marked_missing += 1
        removed_orphans = self._blobs.cleanup_orphans(known_keys=known_keys)
        removed_staging = self._blobs.cleanup_staging(
            older_than=24 * 3600
        )
        report = CleanupReport(
            deleted_metadata=0,
            deleted_blobs=0,
            removed_orphans=removed_orphans,
            marked_missing=marked_missing,
            tenant_ids=(tenant_id,),
        )
        if removed_staging:
            logger.info(
                "attachment staging 清理 tenant=%s removed=%d",
                tenant_id,
                removed_staging,
            )
        self._emit("cleanup.finished", report)
        return report

    def _emit(self, event_name: str, report: CleanupReport) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.cleanup_finished(
                event_name=event_name,
                deleted=report.deleted_metadata,
                removed_orphans=report.removed_orphans,
                marked_missing=report.marked_missing,
                tenants=report.tenant_ids,
            )
        except Exception:
            logger.exception("attachment lifecycle 记录点异常（不阻断清理）")


__all__ = ["AttachmentLifecycle", "CleanupReport"]