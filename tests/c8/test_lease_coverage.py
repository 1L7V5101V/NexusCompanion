"""C8 P0 lease coverage：每类 work 入口在 work start 取一次 snapshot lease。

覆盖（ADR-1 / task-08 1.1-1.7）：
- work_runtime_lease 统一入口语义（bind/release/none-store/异常/取消/task 隔离）；
- passive work（_process_with_runtime_admission）取 lease + gate recheck；
- maintenance：control 触发的 consolidation（trigger_memory_consolidation）取 lease；
- optimizer（MemoryOptimizer.optimize）取 lease；
- proactive tick 取 lease，且 drift 在 tick lease 内执行（lease 归属证明）；
- 热更新不切 snapshot（进行中 work 保持原绑定）。
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.admission.revocation import (
    RevocationGate,
    RevocationRejected,
    TenantStatus,
)
from agent.looping.core import AgentLoop
from agent.looping.ports import AgentLoopConfig, AgentLoopDeps
from agent.plugins.snapshot import (
    get_current_runtime_snapshot,
    work_runtime_lease,
)
from bus.events import InboundMessage, OutboundMessage
from core.memory.markdown import MarkdownMemoryMaintenance
from proactive_v2.config import ProactiveConfig
from proactive_v2.loop import ProactiveLoop
from proactive_v2.memory_optimizer import MemoryOptimizer, MemoryOptimizerBusy
from tests.c8._helpers import (
    make_active_provider,
    make_snapshot,
    make_status_provider,
    make_store,
)
from tests.memory_fakes import FakeMemoryEngine


# ── work_runtime_lease 统一入口 ────────────────────────────────────


@pytest.mark.asyncio
async def test_work_lease_binds_and_releases() -> None:
    store = make_store("snap-a")

    async with work_runtime_lease(store) as snap:
        assert snap is not None and snap.snapshot_id == "snap-a"
        assert get_current_runtime_snapshot() is snap
        # lease 只计数不互斥；持有期间 snapshot 计入 lease_count
        assert store.current.lease_count == 1

    # 退出（含绑定 reset + release）后无残留
    assert get_current_runtime_snapshot() is None
    assert store.current.lease_count == 0


@pytest.mark.asyncio
async def test_work_lease_none_store_yields_none() -> None:
    async with work_runtime_lease(None) as snap:
        assert snap is None
        assert get_current_runtime_snapshot() is None


@pytest.mark.asyncio
async def test_work_lease_releases_on_exception() -> None:
    store = make_store("snap-a")

    with pytest.raises(RuntimeError, match="boom"):
        async with work_runtime_lease(store):
            raise RuntimeError("boom")

    assert store.current.lease_count == 0
    assert get_current_runtime_snapshot() is None


@pytest.mark.asyncio
async def test_work_lease_releases_on_cancel() -> None:
    store = make_store("snap-a")
    started = asyncio.Event()
    entered = asyncio.Event()

    async def work() -> None:
        async with work_runtime_lease(store):
            entered.set()
            await started.wait()

    task = asyncio.create_task(work())
    await entered.wait()
    assert store.current.lease_count == 1
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert store.current.lease_count == 0


@pytest.mark.asyncio
async def test_work_lease_binding_is_task_local() -> None:
    """ContextVar 绑定带 owner 校验：子任务不继承父任务的 snapshot 绑定。"""
    store = make_store("snap-a")
    child_seen: list[bool] = []

    async def child() -> None:
        # create_task 拷贝 ContextVar，但 owner_task 校验使其对子 task 不可见
        child_seen.append(get_current_runtime_snapshot() is not None)

    async with work_runtime_lease(store):
        assert get_current_runtime_snapshot() is not None
        task = asyncio.create_task(child())
        await task

    assert child_seen == [False]


# ── passive work（AgentLoop._process_with_runtime_admission） ──────


def _make_loop(tmp_path: Path) -> AgentLoop:
    memory = FakeMemoryEngine(tmp_path)
    runtime = cast(
        object,
        SimpleNamespace(
            engine=memory,
            markdown=SimpleNamespace(store=memory, maintenance=memory),
        ),
    )
    return AgentLoop(
        AgentLoopDeps(
            bus=MagicMock(),
            provider=MagicMock(),
            tools=MagicMock(),
            session_manager=MagicMock(),
            workspace=tmp_path,
            memory_runtime=runtime,  # type: ignore[arg-type]
        ),
        AgentLoopConfig(),
    )


def _inbound(tenant_id: str, content: str) -> InboundMessage:
    return InboundMessage(
        channel="cli",
        sender="user",
        chat_id="direct",
        content=content,
        tenant_id=tenant_id,
    )


@pytest.mark.asyncio
async def test_passive_process_holds_lease_and_gate(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    store = make_store("snap-passive")
    loop.bind_runtime_snapshot_store(store)
    loop._revocation_gate = RevocationGate(make_active_provider(), source="pilot")

    observed: dict[str, object] = {}

    async def fake_process(msg, key, *, dispatch_outbound=True):
        snap = get_current_runtime_snapshot()
        observed["snapshot_id"] = snap.snapshot_id if snap is not None else None
        observed["key"] = key
        return OutboundMessage(channel="cli", chat_id="direct", content="ok")

    loop._core_runner.process = fake_process  # type: ignore[method-assign]

    result = await loop._process_with_runtime_admission(
        _inbound("tenant-x", "hello"),
        session_key="cli:direct",
    )

    # 被动 work 在 _process 内可见 snapshot 绑定，且 gate 放行
    assert observed == {"snapshot_id": "snap-passive", "key": "cli:direct"}
    assert result.content == "ok"
    assert store.current.lease_count == 0


@pytest.mark.asyncio
async def test_passive_process_rejected_before_process(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    store = make_store("snap-passive")
    loop.bind_runtime_snapshot_store(store)
    loop._revocation_gate = RevocationGate(
        make_status_provider([TenantStatus.REVOKED]),
        source="pilot",
    )

    called = False

    async def fake_process(msg, key, *, dispatch_outbound=True):
        nonlocal called
        called = True
        return OutboundMessage(channel="cli", chat_id="direct", content="ok")

    loop._core_runner.process = fake_process  # type: ignore[method-assign]

    with pytest.raises(RevocationRejected):
        await loop._process_with_runtime_admission(
            _inbound("tenant-x", "hello"),
            session_key="cli:direct",
        )

    assert called is False
    # 拒绝发生在 _process 之前，但 lease 已释放
    assert store.current.lease_count == 0


# ── proactive tick + drift lease 归属证明 ─────────────────────────


@pytest.mark.asyncio
async def test_proactive_tick_holds_lease_and_drift_runs_inside_it() -> None:
    """drift 在 proactive tick lease 内执行（design.md §1/ADR-1）。"""
    store = make_store("snap-tick")
    observed: dict[str, object] = {}

    class _Kernel:
        async def run_tick(self, session_key: str) -> float | None:
            snap = get_current_runtime_snapshot()
            observed["snapshot_id"] = snap.snapshot_id if snap is not None else None
            observed["session_key"] = session_key
            return 0.5

    loop = object.__new__(ProactiveLoop)
    loop._cfg = ProactiveConfig()
    loop._sense = SimpleNamespace(
        target_session_key=lambda: "telegram:1",
        target_tenant=lambda: "telegram:1",
    )
    loop._proactive_kernel = _Kernel()
    loop._runtime_snapshot_store = store
    loop._revocation_gate = None
    loop._provisioning = None
    loop._reload_lock = asyncio.Lock()
    loop._active_snapshot_id = store.current.snapshot_id
    loop._kernel_started = True

    score = await loop._tick()

    assert score == 0.5
    # tick（含 drift 分支的 kernel 执行）在 tick lease 绑定内运行
    assert observed == {"snapshot_id": "snap-tick", "session_key": "telegram:1"}
    assert store.current.lease_count == 0


# ── maintenance：consolidation + optimizer ─────────────────────────


@pytest.mark.asyncio
async def test_trigger_memory_consolidation_holds_lease(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    store = make_store("snap-maintain")
    loop.bind_runtime_snapshot_store(store)
    session = SimpleNamespace(
        key="cli:test",
        messages=[{"role": "user", "content": "u"}] * 50,
        last_consolidated=0,
    )
    loop.session_manager.get_or_create = MagicMock(return_value=session)
    loop.session_manager.save_async = AsyncMock()

    maintenance = cast(FakeMemoryEngine, loop._markdown_memory.maintenance)
    observed: dict[str, object] = {}
    original = maintenance.consolidate

    async def spy(request):
        snap = get_current_runtime_snapshot()
        observed["snapshot_id"] = snap.snapshot_id if snap is not None else None
        return await original(request)

    maintenance.consolidate = spy  # type: ignore[method-assign]

    triggered = await loop.trigger_memory_consolidation("cli:test")

    assert triggered is True
    # control 触发的 consolidation work 在 work start 取到 lease
    assert observed == {"snapshot_id": "snap-maintain"}
    assert store.current.lease_count == 0


@pytest.mark.asyncio
async def test_maintenance_background_worker_holds_lease() -> None:
    """MarkdownMemoryMaintenance 后台队列 worker 逐条 maintenance 取 lease。"""
    store = make_store("snap-bg")
    maintenance = MarkdownMemoryMaintenance(
        store=cast(object, object()),  # refresh 分支被 spy，store 不被触达
        provider=MagicMock(),
        model="m",
        keep_count=20,
        event_bus=None,
        runtime_snapshot_store=store,
    )
    session = SimpleNamespace(key="sess-a", messages=[], last_consolidated=0)
    maintenance.bind_lifecycle(
        cast(
            object,
            SimpleNamespace(
                get_session=lambda _tenant_id, key: session,
                save_session=AsyncMock(return_value=None),
            ),
        )
    )
    observed: dict[str, object] = {}

    async def spy_refresh(request) -> None:
        snap = get_current_runtime_snapshot()
        observed["snapshot_id"] = snap.snapshot_id if snap is not None else None

    maintenance.refresh_recent_turns = spy_refresh  # type: ignore[method-assign]

    # 经真实入队路径启动后台 worker（空消息 → 走 refresh 分支）
    maintenance._enqueue_maintenance("sess-a", "default")
    for _ in range(200):
        if "sess-a" not in maintenance._maintenance_tasks:
            break
        await asyncio.sleep(0.02)

    assert observed == {"snapshot_id": "snap-bg"}
    assert store.current.lease_count == 0


@pytest.mark.asyncio
async def test_memory_optimizer_holds_lease() -> None:
    store = make_store("snap-opt")
    optimizer = MemoryOptimizer(
        memory=MagicMock(),
        provider=MagicMock(),
        model="m",
        runtime_snapshot_store=store,
    )
    observed: dict[str, object] = {}

    async def fake_optimize(*args: Any, **kwargs: Any) -> None:
        snap = get_current_runtime_snapshot()
        observed["snapshot_id"] = snap.snapshot_id if snap is not None else None

    optimizer._optimize = fake_optimize  # type: ignore[method-assign]

    await optimizer.optimize("default")

    assert observed == {"snapshot_id": "snap-opt"}
    assert store.current.lease_count == 0
    assert optimizer.is_running is False


@pytest.mark.asyncio
async def test_memory_optimizer_busy_rejects_concurrent_run() -> None:
    store = make_store("snap-opt")
    optimizer = MemoryOptimizer(
        memory=MagicMock(),
        provider=MagicMock(),
        model="m",
        runtime_snapshot_store=store,
    )
    release = asyncio.Event()

    async def slow_optimize(*args: Any, **kwargs: Any) -> None:
        await release.wait()

    optimizer._optimize = slow_optimize  # type: ignore[method-assign]

    first = asyncio.create_task(optimizer.optimize("default"))
    await asyncio.sleep(0.01)

    with pytest.raises(MemoryOptimizerBusy):
        await optimizer.optimize("default")

    release.set()
    await first
    assert store.current.lease_count == 0


# ── 热更新不切 snapshot ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_work_keeps_snapshot_across_publish() -> None:
    """进行中 work 的 snapshot 绑定在热更新发布后保持不变（§5.9.16）。"""
    store = make_store("snap-a")

    async with work_runtime_lease(store) as live_a:
        assert live_a is not None and live_a.snapshot_id == "snap-a"

        # 热更新：发布 snap-b（store.current 切换），进行中 work 不切换
        transaction = store.begin_publish(make_snapshot("snap-b"))
        assert get_current_runtime_snapshot().snapshot_id == "snap-a"

    # 提交后，新的 work 拿到新 snapshot
    await store.commit(transaction)
    async with work_runtime_lease(store) as live_b:
        assert live_b is not None and live_b.snapshot_id == "snap-b"


@pytest.mark.asyncio
async def test_work_keeps_snapshot_across_abort() -> None:
    store = make_store("snap-a")

    async with work_runtime_lease(store) as live_a:
        assert live_a is not None and live_a.snapshot_id == "snap-a"
        transaction = store.begin_publish(make_snapshot("snap-bad"))
        assert get_current_runtime_snapshot().snapshot_id == "snap-a"

    # abort 后旧 snapshot 恢复为 current，新 lease 仍为旧 snapshot
    await store.abort(transaction)
    assert store.current.snapshot_id == "snap-a"
    async with work_runtime_lease(store) as live_again:
        assert live_again is not None and live_again.snapshot_id == "snap-a"