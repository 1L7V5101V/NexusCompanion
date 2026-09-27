"""C7 task 6.1：租户级工具调用取消传播（design ADR-7）。

封禁事件（admin suspend/revoke）→ :meth:`cancel_tenant` 取消该租户所有**执行中**
的工具调用任务；工具不响应取消则由其既有 timeout 兜底（§3.3 行为基线）。
其他租户不受影响。单进程 Pilot 语义（无跨实例通知）。

注册/注销由 ``ToolRegistry.execute`` 在有执行上下文时进行；取消源为账号状态
变更调用方（admin API 接线处）。

取消来源判别：Python 3.12 起 ``Task.cancel()`` 会把取消转发给当前被 await 的
子任务（``cancel() → _fut_waiter.cancel()``），所以外层 turn 取消也会让执行中的
工具任务收到 ``CancelledError``。仅凭 ``task.cancelled()`` 无法区分「租户取消」与
「外层取消」——本注册表在 :meth:`cancel_tenant` 时对目标 handle 打标，
``ToolRegistry._execute_managed`` 经 :meth:`_take_cancellation` 消费标记：
命中 → 转结构化取消结果（turn 继续收束）；未命中 → 外层取消，原样上抛。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    task: asyncio.Task[Any]
    tenant_id: str


class TenantCancellationRegistry:
    """tenant → 执行中工具任务 的进程级注册表（无状态泄漏：键为租户，值为任务句柄）。"""

    def __init__(self) -> None:
        self._entries: dict[int, _Entry] = {}
        # 本注册表主动取消过的 handle（cancel_tenant 打标，见模块 docstring）。
        self._tenant_cancelled: set[int] = set()
        self._lock = asyncio.Lock()

    async def register(self, tenant_id: str, task: asyncio.Task[Any]) -> int:
        async with self._lock:
            handle = id(task)
            self._entries[handle] = _Entry(task=task, tenant_id=tenant_id)
            return handle

    async def unregister(self, handle: int) -> None:
        async with self._lock:
            self._entries.pop(handle, None)
            # 任务已收束，取消标记不再需要。
            self._tenant_cancelled.discard(handle)

    async def _take_cancellation(self, handle: int) -> bool:
        """消费「本注册表曾对 handle 发起过租户级取消」的标记（原子、一次性）。

        为 True → 该 CancelledError 源于 :meth:`cancel_tenant`，应转结构化取消结果；
        为 False → 是外层 turn 取消转发下来的，应原样上抛。
        """
        async with self._lock:
            if handle in self._tenant_cancelled:
                self._tenant_cancelled.remove(handle)
                return True
            return False

    async def cancel_tenant(self, tenant_id: str, *, reason: str = "") -> int:
        """取消该租户全部执行中工具调用，返回取消数量。其他租户不受影响。"""
        async with self._lock:
            targets = [
                (handle, entry)
                for handle, entry in self._entries.items()
                if entry.tenant_id == tenant_id and not entry.task.done()
            ]
            for handle, entry in targets:
                self._tenant_cancelled.add(handle)
                entry.task.cancel()
        if targets:
            logger.warning(
                "tool_cancellation=cancelled tenant=%s count=%d reason=%s",
                tenant_id,
                len(targets),
                reason,
            )
        return len(targets)

    def active_count(self, tenant_id: str) -> int:
        return sum(
            1
            for entry in self._entries.values()
            if entry.tenant_id == tenant_id and not entry.task.done()
        )


_SHARED = TenantCancellationRegistry()


def shared_registry() -> TenantCancellationRegistry:
    return _SHARED