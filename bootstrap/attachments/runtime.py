"""C6 附件生命周期后台接线（design ADR-5/ADR-10）。

``AttachmentLifecycleRuntime`` 是进程内周期任务壳：每个 tick 跑
``cleanup_expired``，按 ``reconcile_interval_s`` 比例轮跑 per-tenant ``reconcile``；
对账本体只在 ``bootstrap/attachments/lifecycle.py`` 实现一处，本模块负责选租户、
控节奏与异常隔离（异常记录后继续下一轮，``CancelledError`` 正常退出）。

租户集合：构造时显式传入 ``tenant_ids`` 则只对这些租户操作；为空（生产默认）则
每轮取 ``repo.list_tenant_ids()``——即所有有附件记录的租户，使 24h staging 清理
与孤儿收敛不只在启动发生。

``reconcile_now(dry_run=True)`` 提供演练入口（P3 要求），报告形态对齐
``core/telemetry/retention.SweepReport``（dry_run/to_dict/errors）。
"""

from __future__ import annotations

import asyncio
import logging

from bootstrap.attachments.lifecycle import AttachmentLifecycle, LifecycleReport

logger = logging.getLogger(__name__)


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
        reconcile_on_startup: bool = True,
    ) -> None:
        self._lifecycle = lifecycle
        self._cleanup_interval_s = cleanup_interval_s
        self._reconcile_interval_s = reconcile_interval_s
        self._tenant_ids = tuple(tenant_ids)
        self._reconcile_enabled = reconcile_enabled
        self._reconcile_on_startup = reconcile_on_startup
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

    async def _run_round(self, tick: int) -> LifecycleReport:
        """一轮：先清理到期，再按节奏对账。清理异常不阻断对账。"""
        cleanup = await self._cleanup_round(tick)
        if not self._should_reconcile(tick):
            return cleanup
        for tenant_id in await self._tenant_ids_for_round():
            try:
                report = await self._lifecycle.reconcile(tenant_id)
                if report.changed:
                    logger.info(
                        "attachment reconcile tenant=%s orphans=%d missing=%d staging=%d errors=%d",
                        tenant_id,
                        report.removed_orphans,
                        report.marked_missing,
                        report.removed_staging,
                        len(report.errors),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("attachment reconcile 失败 tenant=%s（下轮重试）", tenant_id)
        return cleanup

    async def _cleanup_round(self, tick: int) -> LifecycleReport:
        try:
            report = await self._lifecycle.cleanup_expired()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("attachment cleanup round=%d 失败（继续对账）", tick)
            return LifecycleReport()
        if report.changed:
            logger.info(
                "attachment cleanup round=%d metadata=%d blobs=%d tenants=%d",
                tick,
                report.deleted_metadata,
                report.deleted_blobs,
                len(report.tenant_ids),
            )
        return report

    def _should_reconcile(self, tick: int) -> bool:
        if not self._reconcile_enabled or self._reconcile_interval_s <= 0:
            return False
        if tick == 0:
            return self._reconcile_on_startup
        period = max(1, round(self._reconcile_interval_s / max(self._cleanup_interval_s, 1e-9)))
        return tick % period == 0

    async def _tenant_ids_for_round(self) -> tuple[str, ...]:
        if self._tenant_ids:
            return self._tenant_ids
        try:
            return tuple(await self._lifecycle.repo.list_tenant_ids())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("attachment 对账枚举租户失败（跳过本轮）")
            return ()

    # ── 手动 / 启动入口 ─────────────────────────────────────────

    async def reconcile_now(
        self, tenant_id: str, *, dry_run: bool = False
    ) -> LifecycleReport:
        """单租户对账（启动 + 运营手工触发共用）；dry_run 只统计不落盘。"""
        return await self._lifecycle.reconcile(tenant_id, dry_run=dry_run)

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
        self._task = None


__all__ = ["AttachmentLifecycleRuntime"]
