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
async def test_context_overrides_legacy_shared_context() -> None:
    """context 注入优先于兼容 set_context 的同名键（若有残留）。"""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    registry.set_context(channel="stale-channel", current_timestamp="2026-01-01")

    result = await registry.execute("echo_scope", {}, context=_context())

    assert "channel=chat" in str(result)
    assert "stale-channel" not in str(result)


@pytest.mark.asyncio
async def test_set_context_drops_trust_keys() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    # 旧调用方式（before_reasoning 时代）：trust 键被丢弃并告警，不进 merged。
    registry.set_context(
        tenant_id="tenant:attacker",
        channel="telegram",
        current_timestamp="2026-01-01",
    )

    result = await registry.execute("echo_scope", {})

    assert result == "tenant=|channel=|session_key="
    assert registry.get_context() == {"current_timestamp": "2026-01-01"}


@pytest.mark.asyncio
async def test_without_context_trust_fields_stay_absent() -> None:
    """无 context 时（未接线路径）可信字段为空——宁可失败不可串租户。"""
    registry = ToolRegistry()
    registry.register(_EchoTool())

    result = await registry.execute(
        "echo_scope", {"tenant_id": "tenant:attacker"}
    )

    assert result == "tenant=|channel=|session_key="
