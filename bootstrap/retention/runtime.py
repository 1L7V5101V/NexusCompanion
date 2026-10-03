"""retention 周期任务壳（p0-retention-wiring ADR-3）。

形状对齐 C6 ``AttachmentLifecycleRuntime``（tick 循环 + 单实体异常隔离 +
``stop()``）；本体在 ``RetentionSweeper`` 一处实现，本模块只控节奏：

- **启动不跑首轮**（ADR-3：启动窗口已有 webchat 对账 / 分区 reconciliation /
  attachment 对账，不再叠加一轮删除风暴）——``run()`` 先睡一个完整周期；
- 首轮删除要么等到一个完整周期之后，要么由管理员经 ``run_once(dry_run=...)``
  显式触发（演练 / 实删共用入口）；
- ``CancelledError`` 正常退出；sweeper 单实体失败已被逐实体隔离并计入报告，
  runtime 侧再兜一层整轮异常（记录后继续下一轮）。
"""

from __future__ import annotations

import asyncio
import logging

from bootstrap.retention.sweeper import RetentionRunReport, RetentionSweeper

logger = logging.getLogger(__name__)


class RetentionRuntime:
    """数据保留期周期任务（进程内、可配置禁用、演练入口保留）。"""

    def __init__(self, sweeper: RetentionSweeper, *, interval_s: int) -> None:
        self._sweeper = sweeper
        self._interval_s = max(1, int(interval_s))
        self._task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """周期循环：**先睡后跑**（启动不执行首轮删除，spec「启动不立即执行删除」）。"""
        while True:
            try:
                await asyncio.sleep(self._interval_s)
            except asyncio.CancelledError:
                raise
            try:
                report = await self._sweeper.run_once(dry_run=False)
                if report.entities:
                    logger.info(
                        "retention 周期轮完成 recoverable=%d irrecoverable=%d ok=%s",
                        report.recoverable_deleted,
                        report.irrecoverable_deleted,
                        report.ok,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("retention 周期轮异常（下一轮继续）")

    async def run_once(self, *, dry_run: bool = True) -> RetentionRunReport:
        """管理员显式入口：演练（默认，零变更）或实删单轮。"""
        return await self._sweeper.run_once(dry_run=dry_run)

    def start(self) -> None:
        """装配周期任务；重复调用幂等。"""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(), name="retention_sweep")
        self._task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[None]) -> None:
        """task 意外退出时大声记录（CancelledError 属正常停机路径）。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "retention sweep task 意外退出",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None


__all__ = ["RetentionRuntime"]
