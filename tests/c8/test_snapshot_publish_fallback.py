"""C8 P2 snapshot 发布失败保留旧 committed snapshot 测试（task-08 2.4，§5.9.16）。

冻结决策：snapshot compile/publish 失败时继续使用上一份 committed snapshot，
不能发布半成品，也不能清空当前可用 snapshot。

覆盖：编译期非法候选（Channel 名称冲突）抛错且不动 store；发布事务
begin_publish → abort 回退（旧 snapshot 仍 current、accepting_leases 恢复、
失败候选进入 aborted 并被 drain）；进行中 work 持旧 lease 时经历
publish_pending + abort 全程内容不变。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent.plugins.generation import GateResult, PluginContributions, PluginGeneration
from agent.plugins.snapshot import (
    RuntimeSnapshotCompiler,
    RuntimeSnapshotStore,
    get_current_runtime_snapshot,
    work_runtime_lease,
)

from tests.c8._helpers import make_snapshot


def _generation_with_channel(plugin_id: str, channel_name: str) -> PluginGeneration:
    return PluginGeneration(
        plugin_id=plugin_id,
        generation_id=f"{plugin_id}-g1",
        module_path=f"plugins/{plugin_id}/plugin.py",
        source_revision="rev0",
        config_revision="cfg0",
        instance=SimpleNamespace(),
        scope=SimpleNamespace(),
        contributions=PluginContributions(
            manifest={},
            channels=(SimpleNamespace(name=channel_name),),
        ),
        gate_result=GateResult(
            gate_id=f"{plugin_id}-gate",
            plugin_id=plugin_id,
            candidate_revision="rev0",
            status="passed",
            checks=(),
        ),
    )


def test_compile_channel_conflict_raises_before_publish() -> None:
    """非法候选（Channel 名称冲突）在 compile 阶段抛错，store 未被触碰。"""
    store = RuntimeSnapshotStore()
    old = make_snapshot("snap-old")
    store.install(old)

    compiler = RuntimeSnapshotCompiler()
    with pytest.raises(RuntimeError, match="Channel 名称冲突"):
        compiler.compile(
            {
                "alpha": _generation_with_channel("alpha", "dup"),
                "beta": _generation_with_channel("beta", "dup"),
            }
        )
    assert store.current is old
    assert store.retained_snapshot_ids == ("snap-old",)


def test_publish_abort_keeps_previous_committed_snapshot() -> None:
    async def scenario() -> None:
        store = RuntimeSnapshotStore()
        old = make_snapshot("snap-old")
        store.install(old)

        candidate = make_snapshot("snap-bad")
        tx = store.begin_publish(candidate)
        assert store.current is candidate  # published_pending 期间候选可见
        assert candidate.accepting_leases  # 未 gate 的发布窗口候选可租约

        await store.abort(tx)
        # 回退：旧 committed snapshot 继续服务
        assert store.current is old
        assert old.state == "committed"
        assert old.accepting_leases
        assert candidate.state == "aborted"
        # 失败候选被 drain（不在 retained 集合中）
        assert "snap-bad" not in store.retained_snapshot_ids

        lease = await store.acquire()
        assert lease.snapshot is old
        await lease.release()

    asyncio.run(scenario())


def test_inflight_work_keeps_snapshot_through_failed_publish() -> None:
    """进行中 work（持旧 lease）不因发布失败切换 snapshot（task-08 P0 验收）。"""

    async def scenario() -> tuple[str, str, str]:
        store = RuntimeSnapshotStore()
        old = make_snapshot("snap-old")
        store.install(old)

        async with work_runtime_lease(store) as work_snapshot:
            assert work_snapshot is not None
            during_work = work_snapshot.snapshot_id

            candidate = make_snapshot("snap-bad")
            tx = store.begin_publish(candidate)
            await store.abort(tx)

            # 发布失败回退后，进行中 work 上下文内解析到的仍是旧 snapshot
            still = get_current_runtime_snapshot()
            assert still is not None
            after = still.snapshot_id

        # work 结束后新 lease 解析到回退后的 current（旧 snapshot）
        lease = await store.acquire()
        fresh = lease.snapshot.snapshot_id
        await lease.release()
        return during_work, after, fresh

    during_work, after, fresh = asyncio.run(scenario())
    assert during_work == "snap-old"
    assert after == "snap-old"
    assert fresh == "snap-old"


def test_publish_success_drains_retired_previous() -> None:
    """对照：发布成功后旧 snapshot 置 retired，lease 清零后被 drain。"""

    async def scenario() -> tuple[str, str]:
        store = RuntimeSnapshotStore()
        old = make_snapshot("snap-old")
        store.install(old)

        candidate = make_snapshot("snap-new")
        tx = store.begin_publish(candidate)
        await store.commit(tx)

        lease = await store.acquire()
        current_id = lease.snapshot.snapshot_id
        await lease.release()
        return current_id, old.state

    current_id, old_state = asyncio.run(scenario())
    assert current_id == "snap-new"
    assert old_state == "retired"
