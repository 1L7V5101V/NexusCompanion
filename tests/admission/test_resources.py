"""C3 资源 semaphore：LLM/embedding/MCP/process 分类计数、互不挤占。"""

from __future__ import annotations

import asyncio

from agent.admission.resources import (
    ResourceKind,
    ResourceLimits,
    ResourceSemaphores,
    gate,
    set_default,
)


def test_resource_limits_defaults_and_validation() -> None:
    limits = ResourceLimits()
    assert limits.llm == 30
    assert limits.embedding == 4
    assert limits.mcp == 8
    assert limits.process == 2
    assert limits.for_kind(ResourceKind.LLM) == 30
    try:
        ResourceLimits(process=0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("process=0 应被拒绝")


async def test_llm_limit_blocks_31st_but_embedding_independent() -> None:
    # LLM 上限 2：第 3 个等待；embedding 上限独立不受影响
    semaphores = ResourceSemaphores(ResourceLimits(llm=2, embedding=2))
    set_default(semaphores)
    try:
        started: list[str] = []

        async def hold(kind: ResourceKind, name: str, gate_event: asyncio.Event) -> None:
            async with semaphores.gate(kind):
                started.append(name)
                await gate_event.wait()

        llm_release: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]
        llm_tasks = [
            asyncio.create_task(hold(ResourceKind.LLM, f"llm{i}", llm_release[i]))
            for i in range(2)
        ]
        while len(started) < 2:
            await asyncio.sleep(0.01)

        blocked_release = asyncio.Event()
        blocked = asyncio.create_task(hold(ResourceKind.LLM, "llm2", blocked_release))
        embed_release = asyncio.Event()
        embed_task = asyncio.create_task(
            hold(ResourceKind.EMBEDDING, "emb0", embed_release)
        )
        await asyncio.sleep(0.05)
        # LLM 满：第 3 个未进入；embedding 立即进入（分类计数不互耗）
        assert started == ["llm0", "llm1", "emb0"]
        assert semaphores.active(ResourceKind.LLM) == 2
        assert not blocked.done()

        for event in llm_release:
            event.set()
        embed_release.set()
        blocked_release.set()
        await asyncio.gather(*llm_tasks, blocked, embed_task)
        assert semaphores.active(ResourceKind.LLM) == 0
        assert semaphores.active(ResourceKind.EMBEDDING) == 0
    finally:
        set_default(None)


async def test_module_gate_passthrough_without_default() -> None:
    set_default(None)
    entered = False
    async with gate(ResourceKind.MCP):
        entered = True
    assert entered
