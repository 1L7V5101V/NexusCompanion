"""C3 interactive>maintenance 优先级 + maintenance 合并/延后（MarkdownMemoryMaintenance）。"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, cast

from core.memory.markdown import MarkdownMemoryMaintenance


def _make_maintenance() -> MarkdownMemoryMaintenance:
    maintenance = MarkdownMemoryMaintenance(
        store=cast(Any, object()),
        provider=cast(Any, object()),
        model="test-model",
        keep_count=20,
        event_bus=None,
    )
    maintenance.bind_lifecycle(
        cast(
            Any,
            type(
                "Bind",
                (),
                {
                    "get_session": staticmethod(lambda key: None),
                    "save_session": staticmethod(
                        lambda session: _async_noop()
                    ),
                },
            )(),
        )
    )
    return maintenance


async def _async_noop() -> None:
    return None


async def test_maintenance_intent_merged_per_session() -> None:
    maintenance = _make_maintenance()
    # 同一 session 连续两次提交：per-(tenant,kind) 只保留 1 个 pending 意图
    maintenance._enqueue_maintenance("session-a")
    maintenance._enqueue_maintenance("session-a")
    queue = maintenance._maintenance_queues.get("session-a")
    assert queue is not None
    assert len(queue) == 1
    # 让任务排空后清理
    await asyncio.sleep(0.05)
    assert maintenance._maintenance_queues.get("session-a") in (None, deque())


async def test_maintenance_deferred_when_global_queue_full() -> None:
    maintenance = _make_maintenance()
    maintenance._maintenance_global_limit = 1
    # 预置一个在途意图（inflight=1 达上限）
    maintenance._maintenance_queues["busy"] = deque(["busy"])
    maintenance._enqueue_maintenance("other")
    # 满载延后：不拒绝用户消息（无异常），意图不入队，下轮重新触发
    assert "other" not in maintenance._maintenance_queues
    assert maintenance._maintenance_deferred_count == 1


async def test_maintenance_reaccepts_after_drain() -> None:
    maintenance = _make_maintenance()
    maintenance._maintenance_global_limit = 1
    maintenance._maintenance_queues["busy"] = deque(["busy"])
    maintenance._enqueue_maintenance("other")
    assert maintenance._maintenance_deferred_count == 1
    # 在途意图消费完 → 延后结束，重新接受
    maintenance._maintenance_queues["busy"].clear()
    maintenance._enqueue_maintenance("other")
    assert "other" in maintenance._maintenance_queues
    assert maintenance._maintenance_deferred_count == 1
    await asyncio.sleep(0.05)
