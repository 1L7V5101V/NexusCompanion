"""C6 附件生命周期服务（design ADR-5）：清理与 reconciliation 的唯一实现。

职责：
- ``cleanup_expired``：到期 metadata（staged 超 temp_ttl / 已 committed 但
  referencing_count=0 且 retention_deadline 过期）先删行、再删 blob。顺序刻意为
  「行先行」：blob 删除失败只留下可被 reconciliation 收敛的孤儿，绝不会留下指向
  已删 blob 的 metadata 行。每个删除产生 ``delete.finished``（C12 §8.1）。
- ``reconcile``：missing = 有 metadata 行但 blob 缺失 → status=missing（读取 404）；
  orphan = blob 存在但无 metadata 行 → 删除（``orphan_grace_seconds`` 内的新写入
  不视为孤儿，覆盖 rename 与 metadata 提交之间的在途窗口）；超龄 staging 文件清理。
  ``dry_run=True`` 只统计不变更（对齐 ``core/telemetry/retention.SweepReport`` 契约）。

已知集合覆盖**全部状态**（含 staged）——「blob 在最终路径 + metadata 尚为 staged」
是上传成功的正常终态，不是孤儿。作用域按租户 blob root（见 blob_store ADR-3），
一个租户的对账物理接触不到另一个租户的目录。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bootstrap.attachments.blob_store import AttachmentBlobStore
from bootstrap.db.repository.attachment_repo import AttachmentRepository

if TYPE_CHECKING:
    from agent.config_models import AttachmentConfig
    from bootstrap.attachments.telemetry import AttachmentTelemetry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LifecycleReport:
    """一轮清理/对账的结果（``SweepReport`` 形态：dry_run/to_dict/errors）。"""

    deleted_metadata: int = 0
    deleted_blobs: int = 0
    removed_orphans: int = 0
    marked_missing: int = 0
    removed_staging: int = 0
    tenant_ids: tuple[str, ...] = ()
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_ids": list(self.tenant_ids),
            "deleted_metadata": self.deleted_metadata,
            "deleted_blobs": self.deleted_blobs,
            "removed_orphans": self.removed_orphans,
            "marked_missing": self.marked_missing,
            "removed_staging": self.removed_staging,
            "dry_run": self.dry_run,
            "errors": list(self.errors),
        }

    @property
    def changed(self) -> bool:
        return bool(
            self.deleted_metadata
            or self.deleted_blobs
            or self.removed_orphans
            or self.marked_missing
            or self.removed_staging
        )


class AttachmentLifecycle:
    def __init__(
        self,
        repo: AttachmentRepository,
        blob_store: AttachmentBlobStore,
        config: "AttachmentConfig",
        *,
        telemetry: "AttachmentTelemetry | None" = None,
    ) -> None:
        self.repo = repo
        self.blob_store = blob_store
        self.config = config
        self._telemetry = telemetry

    # ── 到期清理 ─────────────────────────────────────────────────────

    async def cleanup_expired(self, *, tenant_filter: str | None = None) -> LifecycleReport:
        """删除到期附件：先删 metadata 行再删 blob；幂等（重复跑删除数=0）。"""
        expired = await self.repo.list_expired(tenant_filter=tenant_filter)
        deleted_meta = 0
        deleted_blobs = 0
        tenants: set[str] = set()
        errors: list[str] = []
        for record in expired:
            tenants.add(record.tenant_id)
            try:
                removed_row = await self.repo.delete_attachment(
                    tenant_id=record.tenant_id, attachment_id=record.id
                )
                if not removed_row:
                    continue  # 行已被并发清理删除，幂等
                deleted_meta += 1
                if self.blob_store.delete_blob(record.tenant_id, record.storage_key):
                    deleted_blobs += 1
                self._emit_delete(record.tenant_id)
            except Exception as exc:
                errors.append(f"cleanup {record.id}: {exc}")
                logger.exception(
                    "attachment cleanup 失败 tenant=%s attachment=%s",
                    record.tenant_id,
                    record.id,
                )
        report = LifecycleReport(
            deleted_metadata=deleted_meta,
            deleted_blobs=deleted_blobs,
            tenant_ids=tuple(sorted(tenants)),
            errors=errors,
        )
        self._emit_cleanup(report)
        return report

    # ── reconciliation（唯一实现，runtime.reconcile_now 委托至此） ──────

    async def reconcile(
        self, tenant_id: str, *, dry_run: bool = False
    ) -> LifecycleReport:
        """单租户对账：missing 标记 + orphan 清理 + 超龄 staging 清理。"""
        errors: list[str] = []
        try:
            records = await self.repo.list_by_tenant(tenant_id)
        except Exception as exc:
            errors.append(f"list_by_tenant failed: {exc}")
            return LifecycleReport(tenant_ids=(tenant_id,), dry_run=dry_run, errors=errors)

        known_keys = {r.storage_key for r in records}
        orphan_keys = self.blob_store.find_orphan_keys(
            tenant_id, known_keys, grace_seconds=self.config.orphan_grace_seconds
        )
        stale_staging = self.blob_store.find_stale_staging(
            tenant_id, older_than=self.config.temp_ttl_hours * 3600
        )
        missing_candidates = [
            r
            for r in records
            if r.status in ("committed", "missing")
            and not self.blob_store.blob_exists(tenant_id, r.storage_key)
        ]

        if dry_run:
            return LifecycleReport(
                removed_orphans=len(orphan_keys),
                marked_missing=len(missing_candidates),
                removed_staging=len(stale_staging),
                tenant_ids=(tenant_id,),
                dry_run=True,
                errors=errors,
            )

        removed_orphans = self.blob_store.delete_keys(tenant_id, orphan_keys)
        removed_staging = self.blob_store.cleanup_staging(
            tenant_id, older_than=self.config.temp_ttl_hours * 3600
        )
        marked = 0
        for record in missing_candidates:
            if record.status == "missing":
                continue
            try:
                updated = await self.repo.mark_missing(
                    tenant_id=tenant_id, attachment_id=record.id
                )
                if updated is not None:
                    marked += 1
            except Exception as exc:
                errors.append(f"mark_missing {record.id}: {exc}")

        report = LifecycleReport(
            removed_orphans=removed_orphans,
            marked_missing=marked,
            removed_staging=removed_staging,
            tenant_ids=(tenant_id,),
            errors=errors,
        )
        self._emit_cleanup(report)
        return report

    # ── 记录点（C12 §8.1；异常绝不阻断清理） ───────────────────────────

    def _emit_delete(self, tenant_id: str) -> None:
        t = self._telemetry
        if t is None:
            return
        try:
            t.delete_finished(tenant_id=tenant_id, status="succeeded")
        except Exception:
            logger.exception("attachment delete 记录点异常（不阻断清理）")

    def _emit_cleanup(self, report: LifecycleReport) -> None:
        t = self._telemetry
        if t is None:
            return
        try:
            t.cleanup_finished(
                event_name="cleanup.finished",
                deleted=report.deleted_metadata,
                removed_orphans=report.removed_orphans,
                marked_missing=report.marked_missing,
                tenants=report.tenant_ids,
            )
        except Exception:
            logger.exception("attachment lifecycle 记录点异常（不阻断清理）")


__all__ = ["AttachmentLifecycle", "LifecycleReport"]
