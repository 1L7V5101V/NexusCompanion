"""Tenant-scoped serial lane（PILOT_ROADMAP §6.1 A）。

- lane key 固定为服务端派生的 ``tenant_id``；Pilot 一个 tenant 只有一个规范会话，
  不另建跨 session 调度模型；
- 同一 tenant 内 interactive / maintenance 各自串行，且 interactive 优先：
  maintenance 在 lane 空闲时执行，新 interactive 到达时排队中的 maintenance 被延后；
- 不使用跨 tenant 的 global maintenance lock；一个 tenant 的 backlog、取消或失败
  不持有其他 tenant 的 lane；
- lane owner 在成功、失败、取消、超时和异常路径统一释放（锁的 ``finally`` 语义）；
- ``close()`` 后拒绝产生新 work（进程关闭不产生新 work item，未完成项交给启动恢复扫描）。

每次入队/开始/结束按 §6.1 A 记录 ``work_id``/``tenant_id``/``work_kind``/时间戳观测字段。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TypeVar

from bus.events import InboundItem
from infra.storage.tenancy import tenant_id_for_channel

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_MAINTENANCE_ACQUIRE_TIMEOUT_SECONDS = 5.0


class WorkKind(StrEnum):
    INTERACTIVE = "interactive"
    MAINTENANCE = "maintenance"


class LaneClosedError(RuntimeError):
    """lane 已关闭（进程排空中），拒绝产生新 work。"""


class MaintenanceDeferred(RuntimeError):
    """maintenance 意图被延后（interactive 优先或全局上限），可从持久状态重算。"""


def resolve_admission_tenant(item: InboundItem) -> str:
    """由可信入站 item 解析 admission tenant key。

    E9 弱对齐切换点：当前沿用 adapter 边界 ``tenant_id_for_channel()`` 的可信派生
    （§5.9.5 sanctioned）；C5 channel 认证落地后此处改接 C1 ``CanonicalIdentityResolver``
    的 canonical mapping，调用方不变。禁止回退 ``DEFAULT_TENANT``；channel 与
    chat_id 均缺失时 fail-closed 拒绝。
    """
    tenant = str(getattr(item, "tenant_id", "") or "").strip()
    if tenant:
        return tenant
    channel = str(getattr(item, "channel", "") or "").strip()
    chat_id = str(getattr(item, "chat_id", "") or "").strip()
    if not channel or not chat_id:
        raise ValueError(
            "admission tenant 无法解析：入站 item 缺少可信 channel/chat_id 标识"
        )
    return tenant_id_for_channel(channel, chat_id)


class _TenantLane:
    """单 tenant 的执行锁与 interactive 占用计数。"""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.lock = asyncio.Lock()
        self.interactive_pending = 0
        self.interactive_active = False
        self._idle = asyncio.Event()
        self._idle.set()

    async def wait_interactive_idle(self) -> None:
        while self.interactive_active or self.interactive_pending > 0:
            self._idle.clear()
            await self._idle.wait()


class TenantLaneRouter:
    """同 tenant 串行、跨 tenant 异步、interactive 优先于 maintenance。"""

    def __init__(self) -> None:
        self._lanes: dict[str, _TenantLane] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """进入关闭流程：拒绝新 work；在途 work 继续执行至收束。"""
        self._closed = True

    def _lane(self, tenant_id: str) -> _TenantLane:
        lane = self._lanes.get(tenant_id)
        if lane is None:
            lane = _TenantLane(tenant_id)
            self._lanes[tenant_id] = lane
        return lane

    def interactive_pending(self, tenant_id: str) -> int:
        return self._lane(tenant_id).interactive_pending

    async def run_interactive(
        self,
        tenant_id: str,
        work_id: str,
        func: Callable[[], Awaitable[_T]],
    ) -> _T:
        """在 tenant interactive lane 内执行；owner 在所有退出路径统一释放。"""
        if self._closed:
            raise LaneClosedError(f"lane 已关闭，拒绝新 interactive work: {work_id}")
        lane = self._lane(tenant_id)
        lane.interactive_pending += 1
        lane._idle.clear()
        started = time.monotonic()
        try:
            async with lane.lock:
                if self._closed:
                    raise LaneClosedError(
                        f"lane 已关闭，放弃 interactive work: {work_id}"
                    )
                lane.interactive_pending -= 1
                if lane.interactive_pending == 0 and not lane.interactive_active:
                    lane._idle.set()
                lane.interactive_active = True
                try:
                    self._log(tenant_id, work_id, WorkKind.INTERACTIVE, "started")
                    return await func()
                finally:
                    lane.interactive_active = False
                    if lane.interactive_pending == 0:
                        lane._idle.set()
                    self._log(
                        tenant_id,
                        work_id,
                        WorkKind.INTERACTIVE,
                        "finished",
                        started,
                    )
        except asyncio.CancelledError:
            self._log(tenant_id, work_id, WorkKind.INTERACTIVE, "cancelled", started)
            raise
        except BaseException:
            self._log(tenant_id, work_id, WorkKind.INTERACTIVE, "failed", started)
            raise

    async def run_maintenance(
        self,
        tenant_id: str,
        work_id: str,
        func: Callable[[], Awaitable[_T]],
        *,
        acquire_timeout: float = _MAINTENANCE_ACQUIRE_TIMEOUT_SECONDS,
    ) -> _T:
        """在 tenant maintenance lane 内执行；interactive 活跃/排队时延后。

        maintenance work 必须可从持久状态重算（§5.9.5），被延后即抛
        :class:`MaintenanceDeferred`，由调用方跳过本轮（下轮重新触发）。
        """
        if self._closed:
            raise LaneClosedError(f"lane 已关闭，拒绝新 maintenance work: {work_id}")
        lane = self._lane(tenant_id)
        started = time.monotonic()
        acquired = False
        try:
            try:
                async with asyncio.timeout(acquire_timeout):
                    while lane.interactive_active or lane.interactive_pending > 0:
                        await lane.wait_interactive_idle()
                    await lane.lock.acquire()
                    acquired = True
            except TimeoutError:
                self._log(tenant_id, work_id, WorkKind.MAINTENANCE, "deferred", started)
                raise MaintenanceDeferred(
                    f"tenant {tenant_id} interactive 忙，maintenance 延后: {work_id}"
                ) from None
            try:
                self._log(tenant_id, work_id, WorkKind.MAINTENANCE, "started")
                return await func()
            finally:
                acquired = False
                lane.lock.release()
                self._log(
                    tenant_id, work_id, WorkKind.MAINTENANCE, "finished", started
                )
        except asyncio.CancelledError:
            self._log(tenant_id, work_id, WorkKind.MAINTENANCE, "cancelled", started)
            raise
        except BaseException:
            self._log(tenant_id, work_id, WorkKind.MAINTENANCE, "failed", started)
            raise
        finally:
            if acquired:
                # 获取成功后、进入执行 try 前被取消/超时的兜底释放。
                acquired = False
                lane.lock.release()

    @staticmethod
    def _log(
        tenant_id: str,
        work_id: str,
        kind: WorkKind,
        event: str,
        started: float | None = None,
    ) -> None:
        extra: dict[str, object] = {
            "tenant_id": tenant_id,
            "work_id": work_id,
            "work_kind": kind.value,
            "lane_event": event,
        }
        if started is not None:
            extra["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        logger.info("lane work %s", event, extra=extra)
