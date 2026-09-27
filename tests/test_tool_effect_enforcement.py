"""C7 task 4.1/4.2：effect 等级执行面强制（design ADR-4）。

- process-exec / admin 对普通租户一律拒绝；
- external-write 无补偿登记即默认拒绝（不伪装 typed terminal）；
- dev/owner 路径不受限；
- registry.execute 分配 tool_call_id 并附着到 ToolResult。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.tools.base import Tool, ToolEffect, ToolResult
from agent.tools.context import ToolExecutionContext
from agent.tools.registry import ToolRegistry


def _tool(name: str) -> Tool:
    async def _run(self, **kwargs: Any) -> ToolResult:
        return ToolResult(text="ran")

    return type(
        f"_EffT_{name}",
        (Tool,),
        {
            "name": name,
            "description": f"effect stub {name}",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "execute": _run,
        },
    )()


def _context(principal: str = "user") -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="req-abc",
        account_id="acct-1",
        tenant_id="tenant:acct-1",
        session_id="chat:tenant:acct-1",
        turn_id="turn-1",
        channel="chat",
        chat_id="tenant:acct-1",
        principal_type=principal,
    )


def _register(
    registry: ToolRegistry,
    name: str,
    effect: ToolEffect,
    *,
    compensation: bool = False,
) -> None:
    registry.register(_tool(name), effect=effect, requires_compensation=compensation)


@pytest.mark.asyncio
async def test_external_write_denied_without_compensation() -> None:
    registry = ToolRegistry()
    _register(registry, "ext_write", ToolEffect.EXTERNAL_WRITE)
    result = await registry.execute("ext_write", {}, context=_context())
    assert "tool_denied_effect" in str(result)
    assert "external-write" in str(result)


@pytest.mark.asyncio
async def test_external_write_allowed_with_compensation() -> None:
    registry = ToolRegistry()
    _register(registry, "ext_write", ToolEffect.EXTERNAL_WRITE, compensation=True)
    result = await registry.execute("ext_write", {}, context=_context())
    assert isinstance(result, ToolResult) and result.text == "ran"


@pytest.mark.asyncio
async def test_process_exec_denied_for_user_even_with_compensation() -> None:
    registry = ToolRegistry()
    _register(registry, "shellish", ToolEffect.PROCESS_EXEC, compensation=True)
    result = await registry.execute("shellish", {}, context=_context())
    assert "tool_denied_effect" in str(result)


@pytest.mark.asyncio
async def test_admin_denied_for_user() -> None:
    registry = ToolRegistry()
    _register(registry, "admin_tool", ToolEffect.ADMIN)
    result = await registry.execute("admin_tool", {}, context=_context())
    assert "tool_denied_effect" in str(result)


@pytest.mark.asyncio
async def test_dev_principal_unaffected_by_effect_gate() -> None:
    registry = ToolRegistry()
    _register(registry, "admin_tool", ToolEffect.ADMIN)
    _register(registry, "shellish", ToolEffect.PROCESS_EXEC)
    _register(registry, "ext_write", ToolEffect.EXTERNAL_WRITE)
    for name in ("admin_tool", "shellish", "ext_write"):
        result = await registry.execute(name, {}, context=_context(principal="dev"))
        assert isinstance(result, ToolResult) and result.text == "ran", name


@pytest.mark.asyncio
async def test_read_only_and_tenant_write_allowed() -> None:
    registry = ToolRegistry()
    _register(registry, "reader", ToolEffect.READ_ONLY)
    _register(registry, "writer", ToolEffect.TENANT_LOCAL_WRITE)
    for name in ("reader", "writer"):
        result = await registry.execute(name, {}, context=_context())
        assert isinstance(result, ToolResult) and result.text == "ran"


@pytest.mark.asyncio
async def test_tool_call_id_attached_to_tool_result() -> None:
    registry = ToolRegistry()
    _register(registry, "reader", ToolEffect.READ_ONLY)
    result = await registry.execute("reader", {}, context=_context())
    assert isinstance(result, ToolResult)
    assert result.tool_call_id == "req-abc"


def test_risk_label_auto_mapping() -> None:
    """存量 risk 标签自动映射（external-side-effect → external-write 保守映射）。"""
    registry = ToolRegistry()
    registry.register(_tool("legacy_rw"), risk="write")
    registry.register(_tool("legacy_ext"), risk="external-side-effect")
    assert registry.get_tool("legacy_rw") is not None
    meta_rw = registry._metadata["legacy_rw"]
    meta_ext = registry._metadata["legacy_ext"]
    assert meta_rw.effect is ToolEffect.TENANT_LOCAL_WRITE
    assert meta_ext.effect is ToolEffect.EXTERNAL_WRITE
