"""C3 interactive>maintenance 优先级 + maintenance 合并/延后（MarkdownMemoryMaintenance）。"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, cast

import pytest

from core.memory.markdown import MarkdownMemoryMaintenance
from tests.memory.profile_write_cases import unused_profile_stores


def _make_maintenance() -> MarkdownMemoryMaintenance:
    maintenance = MarkdownMemoryMaintenance(
        profile_stores=cast(Any, unused_profile_stores),
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
    # 同一 session 连续两次提交：per-session 单槽只保留 1 个 pending 意图
    maintenance._enqueue_maintenance("session-a", "default")
    maintenance._enqueue_maintenance("session-a", "default")
    queue = maintenance._maintenance_queues.get(("default", "session-a"))
    assert queue is not None
    assert len(queue) == 1
    # 让任务排空后清理
    await asyncio.sleep(0.05)
    assert maintenance._maintenance_queues.get(("default", "session-a")) in (None, deque())


def test_global_maintenance_limit_is_injectable() -> None:
    """[agent.admission].global_maintenance_queue 必须能覆盖模块常量默认值。"""
    assert _make_maintenance()._maintenance_global_limit == 64
    tuned = MarkdownMemoryMaintenance(
        profile_stores=cast(Any, unused_profile_stores),
        provider=cast(Any, object()),
        model="test-model",
        keep_count=20,
        event_bus=None,
        global_maintenance_limit=3,
    )
    assert tuned._maintenance_global_limit == 3
    with pytest.raises(ValueError):
        MarkdownMemoryMaintenance(
            profile_stores=cast(Any, unused_profile_stores),
            provider=cast(Any, object()),
            model="test-model",
            keep_count=20,
            event_bus=None,
            global_maintenance_limit=0,
        )


async def test_maintenance_deferred_when_global_queue_full() -> None:
    maintenance = _make_maintenance()
    maintenance._maintenance_global_limit = 1
    # 预置一个在途意图（inflight=1 达上限）
    maintenance._maintenance_queues[("default", "busy")] = deque(["busy"])
    maintenance._enqueue_maintenance("other", "default")
    # 满载延后：不拒绝用户消息（无异常），意图不入队，下轮重新触发
    assert ("default", "other") not in maintenance._maintenance_queues
    assert maintenance._maintenance_deferred_count == 1


async def test_maintenance_reaccepts_after_drain() -> None:
    maintenance = _make_maintenance()
    maintenance._maintenance_global_limit = 1
    maintenance._maintenance_queues[("default", "busy")] = deque(["busy"])
    maintenance._enqueue_maintenance("other", "default")
    assert maintenance._maintenance_deferred_count == 1
    # 在途意图消费完 → 延后结束，重新接受
    maintenance._maintenance_queues[("default", "busy")].clear()
    maintenance._enqueue_maintenance("other", "default")
    assert ("default", "other") in maintenance._maintenance_queues
    assert maintenance._maintenance_deferred_count == 1


async def test_maintenance_slots_are_keyed_by_tenant_and_session() -> None:
    """槽位按 (租户, 会话) 分：session_key 只在租户内唯一。

    两个租户的同名会话若共用一个槽，B 的整理意图会被并进 A 那一轮，而那一轮按 A 的
    租户归属写画像——正是 c18 要关掉的泄漏（ADR-16，task 6.12）。
    """
    maintenance = _make_maintenance()
    maintenance._enqueue_maintenance("telegram:1", "tenant-a")
    maintenance._enqueue_maintenance("telegram:1", "tenant-b")
    assert ("tenant-a", "telegram:1") in maintenance._maintenance_queues
    assert ("tenant-b", "telegram:1") in maintenance._maintenance_queues
    # 同一 (租户, 会话) 仍然只留一个意图
    maintenance._enqueue_maintenance("telegram:1", "tenant-a")
    assert len(maintenance._maintenance_queues[("tenant-a", "telegram:1")]) == 1
    await asyncio.sleep(0.05)
    await asyncio.sleep(0.05)
