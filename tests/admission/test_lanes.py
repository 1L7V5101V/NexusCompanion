"""C3 lane router：同 tenant 串行、跨 tenant 异步、interactive 优先、owner 释放。"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from agent.admission.lanes import (
    LaneClosedError,
    MaintenanceDeferred,
    TenantLaneRouter,
    resolve_admission_tenant,
)
from bus.events import InboundMessage, SpawnCompletionItem


def _item(tenant_id: str) -> InboundMessage:
    return InboundMessage(
        channel="chat", sender="u", chat_id="c1", content="hi", tenant_id=tenant_id
    )


async def test_same_tenant_serial_interactive() -> None:
    router = TenantLaneRouter()
    timeline: list[str] = []

    async def work(name: str, delay: float) -> str:
        timeline.append(f"{name}:start")
        await asyncio.sleep(delay)
        timeline.append(f"{name}:end")
        return name

    first = asyncio.create_task(
        router.run_interactive("t1", "w1", lambda: work("a", 0.05))
    )
    await asyncio.sleep(0.01)
    second = asyncio.create_task(
        router.run_interactive("t1", "w2", lambda: work("b", 0.0))
    )
    assert await asyncio.gather(first, second) == ["a", "b"]
    # 同 tenant 串行：a 完整结束后 b 才开始，时间窗无重叠
    assert timeline == ["a:start", "a:end", "b:start", "b:end"]


async def test_different_tenants_run_concurrently() -> None:
    router = TenantLaneRouter()
    release_a = asyncio.Event()
    order: list[str] = []

    async def work_a() -> str:
        order.append("a:start")
        await release_a.wait()
        order.append("a:end")
        return "a"

    async def work_b() -> str:
        order.append("b:start")
        return "b"

    task_a = asyncio.create_task(router.run_interactive("t1", "w1", work_a))
    await asyncio.sleep(0.01)
    task_b = asyncio.create_task(router.run_interactive("t2", "w2", work_b))
    # tenant A 未结束时 B 已完成——跨 tenant 无交叉等待
    assert await asyncio.wait_for(task_b, timeout=1.0) == "b"
    release_a.set()
    assert await task_a == "a"
    assert order == ["a:start", "b:start", "a:end"]


async def test_maintenance_deferred_while_interactive_pending() -> None:
    router = TenantLaneRouter()
    release = asyncio.Event()

    async def interactive() -> str:
        await release.wait()
        return "done"

    async def maintenance() -> str:
        return "m"

    task_i = asyncio.create_task(router.run_interactive("t1", "i1", interactive))
    await asyncio.sleep(0.01)
    # interactive 活跃 + 排队：maintenance 等待超时后被延后
    with pytest.raises(MaintenanceDeferred):
        await router.run_maintenance(
            "t1", "m1", maintenance, acquire_timeout=0.05
        )
    release.set()
    assert await task_i == "done"


async def test_maintenance_runs_when_lane_idle() -> None:
    router = TenantLaneRouter()

    async def maintenance() -> str:
        return "m"

    assert await router.run_maintenance("t1", "m1", maintenance) == "m"


async def test_lane_owner_released_on_exception_and_cancel() -> None:
    router = TenantLaneRouter()

    async def failing() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await router.run_interactive("t1", "w1", failing)

    async def hanging() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(router.run_interactive("t1", "w2", hanging))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def work() -> str:
        return "ok"

    # 失败/取消路径后 owner 已释放，后续 work 可立即执行
    assert await asyncio.wait_for(router.run_interactive("t1", "w3", work), 1.0) == "ok"


async def test_close_rejects_new_work_but_drains_active() -> None:
    router = TenantLaneRouter()
    release = asyncio.Event()

    async def active() -> str:
        await release.wait()
        return "done"

    task = asyncio.create_task(router.run_interactive("t1", "w1", active))
    await asyncio.sleep(0.01)
    router.close()

    async def work() -> str:
        return "x"

    with pytest.raises(LaneClosedError):
        await router.run_interactive("t1", "w2", work)
    with pytest.raises(LaneClosedError):
        await router.run_maintenance("t1", "m1", work)

    release.set()
    assert await task == "done"


async def test_resolve_admission_tenant_prefers_tenant_id_and_fails_closed() -> None:
    # 有显式 tenant_id 时直接使用
    assert resolve_admission_tenant(_item("tenant-a")) == "tenant-a"
    # 空 tenant_id 回退可信 channel 派生（E9 弱对齐 sanctioned）
    legacy = InboundMessage(channel="chat", sender="u", chat_id="42", content="hi")
    assert resolve_admission_tenant(legacy) == "chat:42"
    # SpawnCompletionItem（无 tenant_id 字段）同样派生
    spawn = SpawnCompletionItem(
        channel="cli", chat_id="7", event=cast(Any, object())
    )
    assert resolve_admission_tenant(spawn) == "cli:7"
    # 空 channel/chat_id：fail-closed，不落 DEFAULT_TENANT
    with pytest.raises(ValueError):
        resolve_admission_tenant(
            InboundMessage(channel="", sender="u", chat_id="", content="x")
        )
