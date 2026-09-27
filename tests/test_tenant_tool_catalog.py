"""C7 task 3.1/2.3：TenantToolCatalog——租户可见目录与 schema 过滤。

对应 spec「三层工具目录与租户白名单」：
- 白名单外工具对租户 principal 不可见（schema 不出现）且执行时被拒；
- engine 注入工具（如 rachael 的 reinforce_memory）按注册来源归类：启用该引擎的
  租户可见可用（effect 按引擎声明映射），未启用则 registry 中不存在、自然不可见；
- dev/owner 路径（context 为 None 或 principal 非 user）保持现状全量视图。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.tools.base import Tool
from agent.tools.catalog import (
    TENANT_ALLOWED_TOOL_IDS,
    TENANT_CLOSED_TOOL_IDS,
    tenant_visible_names,
)
from agent.tools.context import ToolExecutionContext
from agent.tools.registry import ToolRegistry


class _StubTool(Tool):
    name = "stub"
    description = "stub tool"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def _make_tool(name: str) -> Tool:
    """以类属性方式生成具名 stub（Tool.__init_subclass__ 校验要求）。"""
    return type(
        f"_StubTool_{name}",
        (_StubTool,),
        {"name": name, "description": f"stub {name}"},
    )()


def _context(principal: str = "user") -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="req-1",
        account_id="acct-1",
        tenant_id="tenant:acct-1",
        session_id="chat:tenant:acct-1",
        turn_id="turn-1",
        channel="chat",
        chat_id="tenant:acct-1",
        principal_type=principal,
    )


def test_allowed_and_closed_lists_are_disjoint() -> None:
    assert not (TENANT_ALLOWED_TOOL_IDS & TENANT_CLOSED_TOOL_IDS)


def _registry_with_all() -> ToolRegistry:
    registry = ToolRegistry()
    for name in (
        "recall_memory",
        "search_messages",
        "read_file",
        "message_push",
        "tool_search",
        # 关闭面
        "shell",
        "spawn",
        "load_skill",
        "mcp_add",
    ):
        registry.register(_make_tool(name))
    return registry


def test_tenant_schema_excludes_closed_tools() -> None:
    registry = _registry_with_all()
    visible = tenant_visible_names(registry, _context())
    assert visible is not None
    assert "shell" not in visible
    assert "spawn" not in visible
    assert "load_skill" not in visible
    assert "mcp_add" not in visible
    assert {"recall_memory", "search_messages", "read_file", "message_push"} <= visible


def test_dev_principal_keeps_full_view() -> None:
    registry = _registry_with_all()
    # context 为 None（未接线路径）与 principal=dev 均不过滤。
    assert tenant_visible_names(registry, None) is None
    assert tenant_visible_names(registry, _context(principal="dev")) is None
    assert tenant_visible_names(registry, _context(principal="admin")) is None


def test_unknown_tools_not_invented_into_visible_set() -> None:
    """可见集合 = 白名单 ∩ 已注册：白名单里没注册的工具不会被凭空发明。"""
    registry = ToolRegistry()
    registry.register(_make_tool("shell"))
    visible = tenant_visible_names(registry, _context())
    assert visible == set()


def test_engine_injected_tool_visible_for_bound_tenant() -> None:
    """engine 注入工具按注册来源归类：以 memory_engine source 注册即对租户可见。"""
    registry = _registry_with_all()
    # 模拟 register_memory_meta_tools 的注册方式（source_type=memory_engine）。
    registry.register(
        _make_tool("reinforce_memory"),
        always_on=True,
        risk="write",
        source_type="memory_engine",
    )
    visible = tenant_visible_names(registry, _context())
    assert visible is not None and "reinforce_memory" in visible


def test_engine_tool_absent_when_engine_not_active() -> None:
    """租户未启用该引擎 → registry 中无此工具 → 自然不可见。"""
    registry = _registry_with_all()  # 无 reinforce_memory
    visible = tenant_visible_names(registry, _context())
    assert visible is not None and "reinforce_memory" not in visible
