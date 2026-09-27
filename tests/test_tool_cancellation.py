"""C7 task 6.1：封禁事件 → 执行中工具调用按 tenant 取消传播（design ADR-7）。

- ``cancel_tenant`` 只取消目标租户的执行中工具，其他租户不受影响；
- 被租户取消的工具转结构化取消结果（``tool_cancelled_account_status``），
  turn 继续收束，不伪装成 成功/失败 终态；
- 外层 turn 取消（非租户取消）不吞掉：CancelledError 原样上抛；
- 正常完成的工具从注册表移除（无泄漏）；
- admin 封禁端点的接线（``bootstrap.auth.api._cancel_tenant_tools``）经
  canonical conversation → tenant_id 传播取消；查不到/异常时 fail-safe 返回 0，
  不阻断封禁主流程。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent.admission.tool_cancellation import shared_registry
from agent.tools.base import Tool
from agent.tools.context import ToolExecutionContext
from agent.tools.registry import ToolRegistry

from bootstrap.auth.api import _cancel_tenant_tools


def _slow_tool_init(self: Any) -> None:
    """挂住直到事件放行；收到取消时记录并重抛。"""
    self.release = asyncio.Event()
    self.started = asyncio.Event()
    self.cancelled = False


async def _slow_tool_execute(self: Any, **kwargs: Any) -> str:
    self.started.set()
    try:
        await self.release.wait()
    except asyncio.CancelledError:
        self.cancelled = True
        raise
    return "done"


def _make_slow_tool(name: str) -> Tool:
    """每个工具独立类名/工具名——同名会互相覆盖注册，导致取消路径测不到。"""
    return type(
        f"_SlowTool_{name}",
        (Tool,),
        {
            "name": name,
            "description": "挂住直到事件放行",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "__init__": _slow_tool_init,
            "execute": _slow_tool_execute,
        },
    )()


def _context(tenant_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id=f"req-{tenant_id}",
        account_id=f"acct-{tenant_id}",
        tenant_id=tenant_id,
        session_id=f"chat:{tenant_id}",
        turn_id=f"turn-{tenant_id}",
        channel="chat",
        chat_id=tenant_id,
        principal_type="user",
    )


async def _start_inflight(
    registry: ToolRegistry, name: str, tenant_id: str
) -> tuple[asyncio.Task[Any], Tool]:
    tool = _make_slow_tool(name)
    registry.register(tool, risk="read-only")
    turn = asyncio.create_task(
        registry.execute(name, {}, context=_context(tenant_id))
    )
    await asyncio.wait_for(tool.started.wait(), timeout=5)
    return turn, tool


@pytest.mark.asyncio
async def test_ban_cancels_inflight_tools_of_target_tenant_only() -> None:
    registry = ToolRegistry()
    turn_a, tool_a = await _start_inflight(registry, "slow_a", "tenant:a")
    turn_b, tool_b = await _start_inflight(registry, "slow_b", "tenant:b")

    assert shared_registry().active_count("tenant:a") == 1
    assert shared_registry().active_count("tenant:b") == 1

    cancelled = await shared_registry().cancel_tenant(
        "tenant:a", reason="admin:suspend"
    )
    assert cancelled == 1

    # 目标租户：执行中调用被取消，转结构化取消结果（turn 收束而非崩溃）。
    result_a = await asyncio.wait_for(turn_a, timeout=5)
    assert "tool_cancelled_account_status" in result_a
    assert tool_a.cancelled is True

    # 其他租户：不受影响，正常完成。
    assert tool_b.cancelled is False
    tool_b.release.set()
    result_b = await asyncio.wait_for(turn_b, timeout=5)
    assert result_b == "done"

    # 注册表无泄漏。
    assert shared_registry().active_count("tenant:a") == 0
    assert shared_registry().active_count("tenant:b") == 0


@pytest.mark.asyncio
async def test_cancel_only_targets_entries_of_matching_tenant() -> None:
    registry = ToolRegistry()
    turn_x, tool_x = await _start_inflight(registry, "slow_x", "tenant:x")
    turn_y, tool_y = await _start_inflight(registry, "slow_y", "tenant:y")
    turn_z, tool_z = await _start_inflight(registry, "slow_z", "tenant:z")

    cancelled = await shared_registry().cancel_tenant("tenant:y", reason="test")
    assert cancelled == 1

    # 目标租户：收束为结构化取消结果（等 turn 完成即取消已被处理，避免竞态）。
    result_y = await asyncio.wait_for(turn_y, timeout=5)
    assert "tool_cancelled_account_status" in result_y
    assert tool_y.cancelled is True

    # 其他租户不受影响，正常完成。
    tool_x.release.set()
    tool_z.release.set()
    assert await asyncio.wait_for(turn_x, timeout=5) == "done"
    assert await asyncio.wait_for(turn_z, timeout=5) == "done"


@pytest.mark.asyncio
async def test_cancel_without_active_tools_returns_zero() -> None:
    n = await shared_registry().cancel_tenant("tenant:ghost", reason="test")
    assert n == 0


@pytest.mark.asyncio
async def test_normal_completion_unregisters_tool() -> None:
    registry = ToolRegistry()
    turn, tool = await _start_inflight(registry, "slow_done", "tenant:done")
    assert shared_registry().active_count("tenant:done") == 1

    tool.release.set()
    result = await asyncio.wait_for(turn, timeout=5)
    assert result == "done"
    assert tool.cancelled is False
    assert shared_registry().active_count("tenant:done") == 0


@pytest.mark.asyncio
async def test_outer_turn_cancellation_is_not_swallowed() -> None:
    """外层 turn 取消 ≠ 租户取消：CancelledError 原样上抛，不伪装成结果。

    3.12 起 Task.cancel() 会把取消转发给被 await 的子任务——工具任务同样会被取消，
    但注册表未对 handle 打标，故上传 CancelledError 而非结构化结果。
    """
    registry = ToolRegistry()
    turn, tool = await _start_inflight(registry, "slow_outer", "tenant:outer")
    inner = next(
        e.task for e in shared_registry()._entries.values()  # noqa: SLF001
        if e.tenant_id == "tenant:outer"
    )

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    # 工具任务（被转发取消）的取消同样收束；断言其确实被取消并清理。
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(inner, timeout=5)
    assert tool.cancelled is True
    assert shared_registry().active_count("tenant:outer") == 0


class _FakeCanonicalRepo:
    def __init__(self, conversations: list[dict[str, Any]]) -> None:
        self._conversations = conversations

    async def list_conversations_by_account(
        self, account_id: str
    ) -> list[dict[str, Any]]:
        return self._conversations


class _FakeRuntime:
    def __init__(self, conversations: list[dict[str, Any]]) -> None:
        self.canonical_repo = _FakeCanonicalRepo(conversations)


@pytest.mark.asyncio
async def test_admin_cancel_helper_propagates_via_canonical_tenant() -> None:
    """admin suspend/revoke 接线：经 canonical conversation 取 tenant_id 后传播。"""
    registry = ToolRegistry()
    turn, tool = await _start_inflight(registry, "slow_admin", "tenant:acct-9")
    runtime = _FakeRuntime(
        [{"tenant_id": "tenant:acct-9", "conversation_id": "c-1"}]
    )

    cancelled = await _cancel_tenant_tools(runtime, "acct-9")
    assert cancelled == 1
    result = await asyncio.wait_for(turn, timeout=5)
    assert "tool_cancelled_account_status" in result
    assert tool.cancelled is True


@pytest.mark.asyncio
async def test_admin_cancel_helper_noop_without_tools_or_error() -> None:
    # 无 canonical conversation（未完成 provisioning）→ 无事可做。
    assert await _cancel_tenant_tools(_FakeRuntime([]), "acct-null") == 0

    # 查询异常 → fail-safe 返回 0，不阻断封禁主流程。
    class _BrokenRepo:
        async def list_conversations_by_account(self, account_id: str) -> list[Any]:
            raise RuntimeError("db down")

    class _BrokenRuntime:
        canonical_repo = _BrokenRepo()

    assert await _cancel_tenant_tools(_BrokenRuntime(), "acct-err") == 0