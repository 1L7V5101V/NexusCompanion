"""C6 附件生命周期后台接线（design ADR-5/ADR-10）。

职责：
- ``AttachmentLifecycleRuntime``：进程内周期任务，按 ``[agent.attachments]``
  的 ``cleanup_interval_s`` / ``reconcile_interval_s`` 分别跑
  ``cleanup_expired``（staged 超龄 + committed 到期无引用）与 per-tenant
  ``reconcile``（missing 标记 + orphan/staging 清理）；
- ``reconcile_now``：可手动触发的即时对账，``dry_run=True`` 只统计不删除，
  报告形态复用 ``core/telemetry/retention.SweepReport`` 契约（to_dict/dry_run）；
- 全程异常不阻断循环（记录日志继续下一轮）；CancelledError 正常退出。

启动接线在 app.py：durable 装配处创建 runtime 并 start()；关闭时 stop()。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from bootstrap.attachments.lifecycle import AttachmentLifecycle

logger = logging.getLogger(__name__)

# reconcile 轮次间隔的默认放大（tick=round(interval/cleanup_interval)）
# 在 _should_reconcile_round 中计算。


@dataclass(frozen=True)
class ReconcileResult:
    """一次对账的 metadata 报告（对齐 ``SweepReport`` 契约：dry_run/to_dict）。

    - ``marked_missing``：committed 但 blob 缺失 → 应标 missing（dry_run 只计数）；
    - ``removed_orphans``：无 metadata 的 blob → 应删（dry_run 只计数）；
    - ``removed_staging``：超龄 staging 文件清理数；
    - ``dry_run``：True 表示本次仅统计，未落盘任何变更。
    """

    tenant_id: str
    marked_missing: int = 0
    removed_orphans: int = 0
    removed_staging: int = 0
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "marked_missing": self.marked_missing,
            "removed_orphans": self.removed_orphans,
            "removed_staging": self.removed_staging,
            "dry_run": self.dry_run,
            "errors": list(self.errors),
        }


class AttachmentLifecycleRuntime:
    """附件生命周期后台任务（进程内、best-effort、可禁用）。"""

    def __init__(
        self,
        lifecycle: AttachmentLifecycle,
        *,
        cleanup_interval_s: float,
        reconcile_interval_s: float,
        tenant_ids: tuple[str, ...] = (),
        reconcile_enabled: bool = True,
        staging_ttl_s: float = 24 * 3600,
    ) -> None:
        self._lifecycle = lifecycle
        self._cleanup_interval_s = cleanup_interval_s
        self._reconcile_interval_s = reconcile_interval_s
        self._tenant_ids = tuple(tenant_ids)
        self._reconcile_enabled = reconcile_enabled
        self._reconcile_on_startup = True
        self._staging_ttl_s = staging_ttl_s
        self._task: asyncio.Task[None] | None = None

    # ── 周期循环 ────────────────────────────────────────────────

    async def run(self) -> None:
        tick = 0
        while True:
            try:
                await self._run_round(tick)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("attachment lifecycle 周期执行异常（下轮继续）")
            tick += 1
            await asyncio.sleep(self._cleanup_interval_s)

    async def _run_round(self, tick: int) -> None:
        try:
            report = await self._lifecycle.cleanup_expired()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("attachment cleanup round=%d 失败（继续对账）", tick)
            report = None
        if report is not None and (
            report.deleted_metadata or report.removed_orphans or report.marked_missing
        ):
            logger.info(
                "attachment cleanup round=%d deleted=%d orphans=%d missing=%d",
                tick,
                report.deleted_metadata,
                report.removed_orphans,
                report.marked_missing,
            )
        interval_ratio = self._reconcile_interval_s / max(self._cleanup_interval_s, 1e-9)
        is_interval_round = round(tick % max(1, round(interval_ratio))) == 0
        is_reconcile_round = (
            self._reconcile_enabled
            and self._reconcile_interval_s > 0
            and (
                (tick == 0 and self._reconcile_on_startup)
                or (tick > 0 and is_interval_round)
            )
        )
        if not is_reconcile_round:
            return
        tenant_ids = await self._tenant_ids_for_round(tick)
        for tenant_id in tenant_ids:
            try:
                result = await self.reconcile_now(tenant_id)
                if result.marked_missing or result.removed_orphans:
                    logger.info(
                        "attachment reconcile tenant=%s missing=%d orphans=%d staging=%d",
                        result.tenant_id,
                        result.marked_missing,
                        result.removed_orphans,
                        result.removed_staging,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "attachment reconcile 失败 tenant=%s（下轮重试）", tenant_id
                )

    async def _tenant_ids_for_round(self, tick: int) -> tuple[str, ...]:
        """本轮的租户集合：启动首轮（tick=0）全量有附件记录的租户；周期轮
        用配置租户（prod 通常为空 → 该轮只对账已启用的租户子集，全量交给
        reconcile_interval 触发的周期轮？prod 为空时列表为空 → 无动作）。"""
        if tick == 0:
            try:
                ids = await self._lifecycle.repo.list_tenant_ids()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("attachment 启动对账枚举租户失败（跳过）")
                return ()
            return tuple(ids)
        return self._tenant_ids

    # ── 手动 / 启动入口 ─────────────────────────────────────────

    async def reconcile_now(
        self, tenant_id: str, *, dry_run: bool = False
    ) -> ReconcileResult:
        """单租户对账（启动 + 运营手工触发共用）。

        dry_run=True 只统计（missing/orphan/staging 将处理数），不落盘删除、
        不更新 metadata。复用 ``SweepReport`` 的报告契约（dry_run/to_dict）。
        """
        errors: list[str] = []
        try:
            records = await self._lifecycle.repo.list_committed_by_tenant(tenant_id)
        except Exception as exc:
            errors.append(f"list_committed failed: {exc}")
            return ReconcileResult(tenant_id=tenant_id, errors=errors, dry_run=dry_run)
        known_keys = {r.storage_key for r in records}
        missing = 0
        for record in records:
            if not self._lifecycle.blob_store.blob_exists(record.storage_key):
                missing += 1
        orphan_keys = self._lifecycle.blob_store.list_orphan_blobs()
        orphan_mine = [
            k for k in orphan_keys if k not in known_keys
        ]
        staging = self._lifecycle.blob_store.staging_files()
        if dry_run:
            return ReconcileResult(
                tenant_id=tenant_id,
                marked_missing=missing,
                removed_orphans=len(orphan_mine),
                removed_staging=len(staging),
                dry_run=True,
            )
        # 实删路径：先删孤儿/超龄 staging，再标 missing（顺序与 lifecycle.reconcile 一致）
        removed = self._lifecycle.blob_store.cleanup_orphans(known_keys=known_keys)
        _staging_removed = self._lifecycle.blob_store.cleanup_staging(
            older_than=self._staging_ttl_s
        )
        for record in records:
            if record.storage_key not in known_keys:
                continue
            if not self._lifecycle.blob_store.blob_exists(record.storage_key):
                if record.status != "missing":
                    try:
                        await self._lifecycle.repo.mark_missing(
                            tenant_id=tenant_id, attachment_id=record.id
                        )
                    except Exception as exc:
                        errors.append(f"mark_missing failed: {exc}")
        return ReconcileResult(
            tenant_id=tenant_id,
            marked_missing=missing,
            removed_orphans=removed,
            removed_staging=_staging_removed,
            dry_run=False,
            errors=errors,
        )

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self, *, reconcile_on_startup: bool | None = None) -> None:
        if self._task is not None and not self._task.done():
            return
        if reconcile_on_startup is not None:
            self._reconcile_on_startup = reconcile_on_startup
        self._task = asyncio.create_task(self.run(), name="attachments_lifecycle")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


__all__ = ["AttachmentLifecycleRuntime", "ReconcileResult"]