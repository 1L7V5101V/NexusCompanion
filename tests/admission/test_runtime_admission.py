"""C3 ConversationRuntime per-tenant admission：同 tenant 串行、跨 tenant 异步。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.control.models import TurnRequest, TurnStatus
from agent.control.runtime import ConversationRuntime
from session.store import SessionStore


def _make_runtime(tmp_path: Path, executor):
    return ConversationRuntime(
        SessionStore(tmp_path / "control.db"),
        executor,
    )


async def test_same_tenant_turns_serialize(tmp_path: Path) -> None:
    timeline: list[str] = []
    first_started = asyncio.Event()

    async def executor(request: TurnRequest) -> str:
        tenant = str(request.metadata.get("tenantId") or "")
        timeline.append(f"{request.thread_id}:{tenant}:start")
        if len(timeline) == 1:
            first_started.set()
            await asyncio.sleep(0.05)
        timeline.append(f"{request.thread_id}:{tenant}:end")
        return "ok"

    runtime = _make_runtime(tmp_path, executor)
    try:
        first = await runtime.start_turn(TurnRequest("t", "a", {"tenantId": "tenant-x"}))
        await first_started.wait()
        second = await runtime.start_turn(
            TurnRequest("t2", "b", {"tenantId": "tenant-x"})
        )
        r1 = await first.result()
        r2 = await second.result()
        assert r1.status is TurnStatus.COMPLETED
        assert r2.status is TurnStatus.COMPLETED
        # 同 tenant：第一条完整结束（含 admission 释放）后第二条才开始执行
        assert timeline == [
            "t:tenant-x:start",
            "t:tenant-x:end",
            "t2:tenant-x:start",
            "t2:tenant-x:end",
        ]
    finally:
        await runtime.shutdown()


async def test_different_tenant_turns_run_concurrently(tmp_path: Path) -> None:
    release_x = asyncio.Event()
    progress: list[str] = []

    async def executor(request: TurnRequest) -> str:
        tenant = str(request.metadata.get("tenantId") or "")
        if tenant == "tenant-x":
            progress.append("x:start")
            await release_x.wait()
            progress.append("x:end")
        else:
            progress.append("y:start")
        return "ok"

    runtime = _make_runtime(tmp_path, executor)
    try:
        task_x = asyncio.create_task(
            runtime.start_turn(TurnRequest("tx", "a", {"tenantId": "tenant-x"}))
        )
        while "x:start" not in progress:
            await asyncio.sleep(0.01)
        handle_y = await runtime.start_turn(
            TurnRequest("ty", "b", {"tenantId": "tenant-y"})
        )
        result_y = await asyncio.wait_for(handle_y.result(), timeout=2.0)
        assert result_y.status is TurnStatus.COMPLETED
        # tenant-y 在 tenant-x 执行期间完成——跨 tenant 异步
        assert progress == ["x:start", "y:start"]
        release_x.set()
        handle_x = await task_x
        assert (await handle_x.result()).status is TurnStatus.COMPLETED
        assert progress == ["x:start", "y:start", "x:end"]
    finally:
        await runtime.shutdown()
