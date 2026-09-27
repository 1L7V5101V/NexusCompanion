"""C7 task 2.2：ToolRegistry 语义反转——可信归属字段不可被模型参数覆盖。

对应 spec「工具执行上下文由服务端派生且不可覆盖」：arguments 中的归属字段一律
剥离；ToolExecutionContext 的可信 kwarg 以最高优先级注入；共享 set_context()
不再承载授权字段。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.tools.base import Tool
from agent.tools.context import ToolExecutionContext
from agent.tools.registry import ToolRegistry


class _EchoTool(Tool):
    name = "echo_scope"

    description = "回显收到的归属字段，供断言。"

    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return (
            f"tenant={kwargs.get('tenant_id', '')}|"
            f"channel={kwargs.get('channel', '')}|"
            f"session_key={kwargs.get('session_key', '')}"
        )


def _context(tenant: str = "tenant:real") -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="req-1",
        account_id="acct-1",
        tenant_id=tenant,
        session_id="chat:tenant:real",
        turn_id="turn-1",
        channel="chat",
        chat_id="tenant:real",
        principal_type="user",
    )


@pytest.mark.asyncio
async def test_arguments_cannot_override_trust_fields() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())

    result = await registry.execute(
        "echo_scope",
        {
            "tenant_id": "tenant:attacker",
            "session_key": "telegram:attacker",
            "channel": "telegram",
            "content": "hi",
        },
        context=_context(),
    )

    assert result == "tenant=tenant:real|channel=chat|session_key="


@pytest.mark.asyncio
async def test_context_hint_keys_override_model_arguments() -> None:
    """per-turn 提示键由服务端派生：模型参数携带同名键不生效（C7 task 2.4）。"""

    class _HintTool(Tool):
        name = "hint_echo"
        description = "回显 current_timestamp"
        parameters = {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs: Any) -> str:
            return f"ts={kwargs.get('current_timestamp', '')}"

    registry = ToolRegistry()
    registry.register(_HintTool())
    ctx = ToolExecutionContext(
        request_id="req-1",
        account_id="acct-1",
        tenant_id="tenant:real",
        session_id="chat:tenant:real",
        turn_id="turn-1",
        channel="chat",
        chat_id="tenant:real",
        principal_type="user",
        current_timestamp="2026-01-01T00:00:00",
    )

    result = await registry.execute(
        "hint_echo",
        {"current_timestamp": "1999-01-01T00:00:00"},
        context=ctx,
    )

    assert result == "ts=2026-01-01T00:00:00"


@pytest.mark.asyncio
async def test_context_overrides_nothing_when_absent() -> None:
    """无 context 时（未接线路径）可信字段为空——宁可失败不可串租户。"""
    registry = ToolRegistry()
    registry.register(_EchoTool())

    result = await registry.execute(
        "echo_scope", {"tenant_id": "tenant:attacker", "content": "hi"}
    )

    assert result == "tenant=|channel=|session_key="


@pytest.mark.asyncio
async def test_context_hint_keys_flow_to_tools() -> None:
    """per-turn 提示键（timestamp/source_ref）经 context 注入，模型参数不参与覆盖。"""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    ctx = _context()
    result = await registry.execute(
        "echo_scope",
        {"current_timestamp": "1999-01-01T00:00:00"},
        context=ctx,
    )
    assert str(result)  # echo 工具不回显提示键，这里只验证调用不抛错

