"""C3 optimizer lock 按 tenant 隔离（无跨 tenant global maintenance lock）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from proactive_v2.memory_optimizer import (
    DEFAULT_OPTIMIZER_TENANT,
    MemoryOptimizer,
    MemoryOptimizerBusy,
)

_OPTIMIZER_SOURCE = (
    Path("proactive_v2") / "memory_optimizer.py"
).read_text(encoding="utf-8")


def test_no_global_optimizer_lock_in_source() -> None:
    # grep 断言：进程级单 lock 已被 per-tenant locks 取代
    assert "self._lock = asyncio.Lock()" not in _OPTIMIZER_SOURCE
    assert "self._locks: dict[str, asyncio.Lock] = {}" in _OPTIMIZER_SOURCE


def _make_optimizer() -> MemoryOptimizer:
    return MemoryOptimizer(
        memory=cast(Any, object()),
        provider=cast(Any, object()),
        model="test-model",
    )


async def test_same_tenant_optimizer_serial(monkeypatch: Any) -> None:
    optimizer = _make_optimizer()
    running = asyncio.Event()

    async def slow_optimize() -> None:
        running.set()
        await asyncio.sleep(0.05)

    monkeypatch.setattr(optimizer, "_optimize", slow_optimize)
    task = asyncio.create_task(optimizer.optimize())
    await running.wait()
    with pytest.raises(MemoryOptimizerBusy):
        await optimizer.optimize()
    await task


async def test_different_tenants_optimizer_parallel(monkeypatch: Any) -> None:
    optimizer = _make_optimizer()
    entered = asyncio.Event()

    async def slow_optimize() -> None:
        entered.set()
        await asyncio.sleep(0.05)

    monkeypatch.setattr(optimizer, "_optimize", slow_optimize)
    task_a = asyncio.create_task(optimizer.optimize(DEFAULT_OPTIMIZER_TENANT))
    await entered.wait()
    # tenant B 的 optimizer 不被 tenant A 阻塞
    task_b = asyncio.create_task(optimizer.optimize("tenant-b"))
    await asyncio.wait_for(task_b, timeout=1.0)
    await task_a
