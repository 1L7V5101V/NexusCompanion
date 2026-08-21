from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from proactive_v2.config import ProactiveConfig
from proactive_v2.loop import ProactiveLoop
from infra.storage.partitioning import PartitionNotReady, PartitionProvisioningFailed


def make_loop() -> ProactiveLoop:
    loop = object.__new__(ProactiveLoop)
    loop._cfg = ProactiveConfig()
    loop._sense = SimpleNamespace(target_session_key=lambda: "telegram:1")
    loop._proactive_kernel = SimpleNamespace(run_tick=AsyncMock(return_value=None))
    loop._runtime_snapshot_store = None
    loop._reload_lock = asyncio.Lock()
    loop._provisioning = None  # 未接 provisioning control seam（M4H-4 commit 5）
    return loop


@pytest.mark.asyncio
async def test_tick_calls_kernel() -> None:
    loop = make_loop()

    result = await loop._tick()

    loop._proactive_kernel.run_tick.assert_awaited_once_with("telegram:1")
    assert result is None


@pytest.mark.asyncio
async def test_tick_return_is_propagated() -> None:
    loop = make_loop()
    loop._proactive_kernel.run_tick = AsyncMock(return_value=42.0)

    assert await loop._tick() == 42.0


@pytest.mark.asyncio
async def test_kernel_route_stable_across_multiple_ticks() -> None:
    loop = make_loop()

    await loop._tick()
    await loop._tick()
    await loop._tick()

    assert loop._proactive_kernel.run_tick.await_count == 3


class _ReadyProvisioning:
    async def require_ready(self, tenant_id: str) -> None:
        return None


class _PendingProvisioning:
    async def require_ready(self, tenant_id: str) -> None:
        raise PartitionNotReady(f"tenant {tenant_id} pending")


class _FailedProvisioning:
    async def require_ready(self, tenant_id: str) -> None:
        raise PartitionProvisioningFailed(f"tenant {tenant_id} failed")


def _gate_loop(provisioning: object) -> ProactiveLoop:
    loop = make_loop()
    loop._sense = SimpleNamespace(
        target_session_key=lambda: "telegram:1",
        target_tenant=lambda: "telegram:1",
    )
    loop._provisioning = provisioning
    return loop


@pytest.mark.asyncio
async def test_tick_skips_when_partition_pending() -> None:
    loop = _gate_loop(_PendingProvisioning())

    result = await loop._tick()

    assert result is None
    loop._proactive_kernel.run_tick.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_skips_when_partition_failed() -> None:
    loop = _gate_loop(_FailedProvisioning())

    result = await loop._tick()

    assert result is None
    loop._proactive_kernel.run_tick.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_runs_when_partition_ready() -> None:
    loop = _gate_loop(_ReadyProvisioning())

    result = await loop._tick()

    assert result is None
    loop._proactive_kernel.run_tick.assert_awaited_once_with("telegram:1")
