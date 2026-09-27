"""C8 RevocationGate：副作用前当前状态 recheck（fail-closed，ADR-3）。

覆盖：
- ACTIVE 放行；SUSPENDED/REVOKED/UNKNOWN/provider 异常 → RevocationRejected；
- provider=None 显式 dev-open（放行 + 结构化日志）；
- 旧 snapshot lease 不能绕过 revocation（gate 只读当前 provider 状态）；
- scheduler instant 直推前 + plugin job 执行前的接线点 recheck。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest

from agent.admission.revocation import (
    RevocationGate,
    RevocationRejected,
    TenantStatus,
)
from agent.plugins.jobs import (
    PluginJobContext,
    PluginJobRuntime,
    PluginJobSpec,
    RegisteredPluginJob,
)
from agent.plugins.snapshot import (
    get_current_runtime_snapshot,
    work_runtime_lease,
)
from agent.scheduler import SchedulerService, ScheduledJob
from bus.event_bus import EventBus
from tests.c8._helpers import (
    make_active_provider,
    make_status_provider,
    make_store,
)


# ── RevocationGate 语义 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_active_status_passes() -> None:
    gate = RevocationGate(make_active_provider(), source="pilot")

    await gate.check("tenant-x", action="passive_work")


@pytest.mark.asyncio
async def test_suspended_status_rejected() -> None:
    gate = RevocationGate(
        make_status_provider([TenantStatus.SUSPENDED]),
        source="pilot",
    )

    with pytest.raises(RevocationRejected) as exc:
        await gate.check("tenant-x", action="passive_work")

    assert exc.value.tenant_id == "tenant-x"
    assert exc.value.action == "passive_work"
    assert exc.value.status is TenantStatus.SUSPENDED


@pytest.mark.asyncio
async def test_revoked_status_rejected() -> None:
    gate = RevocationGate(
        make_status_provider([TenantStatus.REVOKED]),
        source="pilot",
    )

    with pytest.raises(RevocationRejected) as exc:
        await gate.check("tenant-x", action="proactive_tick")

    assert exc.value.status is TenantStatus.REVOKED


@pytest.mark.asyncio
async def test_unknown_status_rejected_fail_closed() -> None:
    gate = RevocationGate(
        make_status_provider([TenantStatus.UNKNOWN]),
        source="pilot",
    )

    with pytest.raises(RevocationRejected) as exc:
        await gate.check("tenant-x", action="passive_work")

    assert exc.value.status is TenantStatus.UNKNOWN


@pytest.mark.asyncio
async def test_provider_exception_fail_closed() -> None:
    async def broken_provider(tenant_id: str) -> TenantStatus:
        raise ConnectionError("account store down")

    gate = RevocationGate(broken_provider, source="pilot")

    with pytest.raises(RevocationRejected) as exc:
        await gate.check("tenant-x", action="passive_work")

    # provider 异常归入 UNKNOWN（任一无法判定时必须 fail-closed）
    assert exc.value.status is TenantStatus.UNKNOWN


@pytest.mark.asyncio
async def test_dev_open_allows_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    gate = RevocationGate(None, source="pilot")

    with caplog.at_level(logging.INFO):
        await gate.check("tenant-x", action="passive_work")

    # provider=None 是显式 dev-open：放行且记录结构化日志，不是静默降级
    assert gate.dev_open is True
    assert any(
        "revocation_gate=dev_open" in record.getMessage()
        and "action=passive_work" in record.getMessage()
        for record in caplog.records
    )


# ── 旧 snapshot 不能绕过 revocation ────────────────────────────────


@pytest.mark.asyncio
async def test_old_snapshot_cannot_bypass_revocation() -> None:
    """work 持有旧 snapshot lease 期间账号被 revoke → 副作用点仍拒绝。

    证明 gate 只读当前 provider 状态，与 work 所持 snapshot 无关。
    """
    # 进入时 ACTIVE；work 持 lease 期间账号变为 REVOKED。
    gate = RevocationGate(
        make_status_provider([TenantStatus.ACTIVE, TenantStatus.REVOKED]),
        source="pilot",
    )
    store = make_store("snap-old")

    async with work_runtime_lease(store) as snap:
        assert snap is not None and snap.snapshot_id == "snap-old"
        # work start 的 check：ACTIVE → 放行
        await gate.check("tenant-x", action="passive_work")

        # 副作用点（scheduler instant push）前的当前 recheck：REVOKED → 拒绝
        with pytest.raises(RevocationRejected) as exc:
            await gate.check("tenant-x", action="scheduler_instant_push")
        assert exc.value.status is TenantStatus.REVOKED

        # 拒绝不改变 work 的 lease 绑定——证明判定与 snapshot 完全无关
        assert get_current_runtime_snapshot() is snap

    # lease 在退出后正常释放，拒绝路径没有泄漏
    assert store.current.lease_count == 0


# ── scheduler instant 直推接线点 ──────────────────────────────────


@pytest.mark.asyncio
async def test_scheduler_instant_push_rejected_before_push(tmp_path: Path) -> None:
    push_tool = AsyncMock()
    service = SchedulerService(
        tmp_path / "jobs.json",
        push_tool,
        revocation_gate=RevocationGate(
            make_status_provider([TenantStatus.REVOKED]),
            source="pilot",
        ),
    )
    job = ScheduledJob(
        trigger="at",
        tier="instant",
        fire_at=datetime.now(timezone.utc),
        channel="telegram",
        chat_id="1",
        message="hello",
    )

    with pytest.raises(RevocationRejected):
        await service._execute(job)

    push_tool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_instant_push_passes_when_active(tmp_path: Path) -> None:
    push_tool = AsyncMock(return_value={"ok": True})
    service = SchedulerService(
        tmp_path / "jobs.json",
        push_tool,
        revocation_gate=RevocationGate(make_active_provider(), source="pilot"),
    )
    job = ScheduledJob(
        trigger="at",
        tier="instant",
        fire_at=datetime.now(timezone.utc),
        channel="telegram",
        chat_id="1",
        message="hello",
    )

    await service._execute(job)

    push_tool.execute.assert_awaited_once_with(
        channel="telegram",
        chat_id="1",
        message="hello",
    )


# ── plugin job 执行接线点 ──────────────────────────────────────────


def _make_job(handler) -> tuple[str, RegisteredPluginJob]:
    key = "p:hello"
    job = RegisteredPluginJob(
        plugin_id="p",
        plugin_context=None,
        spec=PluginJobSpec(id="hello", triggers=[], handler=handler),
    )
    return key, job


@pytest.mark.asyncio
async def test_plugin_job_rejected_before_handler() -> None:
    calls: list[str] = []

    async def handler(ctx: PluginJobContext) -> None:
        calls.append("handler")

    key, job = _make_job(handler)
    store = make_store("snap-job", jobs=MappingProxyType({key: job}))
    runtime = PluginJobRuntime(
        event_bus=EventBus(),
        llm=object(),
        snapshot_store=store,
        revocation_gate=RevocationGate(
            make_status_provider([TenantStatus.REVOKED]),
            source="pilot",
        ),
    )
    task = asyncio.create_task(runtime.run())
    runtime.enqueue(key, reason="interval")
    await asyncio.sleep(0.05)
    runtime.stop()
    await task

    # gate 拒绝 → handler 未执行
    assert calls == []
    # 且 enqueue 时获取的 snapshot lease 已释放（拒绝不泄漏 lease）
    assert store.current.lease_count == 0


@pytest.mark.asyncio
async def test_plugin_job_passes_when_active_and_binds_snapshot() -> None:
    observed: dict[str, object] = {}

    async def handler(ctx: PluginJobContext) -> None:
        snap = get_current_runtime_snapshot()
        observed["snapshot_id"] = snap.snapshot_id if snap is not None else None
        observed["reason"] = ctx.reason

    key, job = _make_job(handler)
    store = make_store("snap-job", jobs=MappingProxyType({key: job}))
    runtime = PluginJobRuntime(
        event_bus=EventBus(),
        llm=object(),
        snapshot_store=store,
        revocation_gate=RevocationGate(make_active_provider(), source="pilot"),
    )
    task = asyncio.create_task(runtime.run())
    runtime.enqueue(key, reason="interval")
    await asyncio.sleep(0.05)
    runtime.stop()
    await task

    # gate 放行 → handler 执行且绑定当前 snapshot
    assert observed == {"snapshot_id": "snap-job", "reason": "interval"}
    assert store.current.lease_count == 0