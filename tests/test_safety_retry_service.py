import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent.looping.safety_retry import SafetyRetryService
from agent.looping.tool_execution import ToolDiscoveryState
from agent.provider import ContentSafetyError, ContextLengthError


def _msg():
    return SimpleNamespace(
        content="hello",
        media=[],
        channel="cli",
        chat_id="1",
        timestamp=datetime.now(timezone.utc),
    )


def _session():
    return SimpleNamespace(
        key="s:1",
        messages=[{"role": "user", "content": str(i)} for i in range(6)],
        get_history=lambda max_messages: [{"role": "user", "content": str(i)} for i in range(6)],
        last_consolidated=3,
    )


def test_safety_retry_retries_and_updates_discovery():
    discovery = ToolDiscoveryState()
    discovery._unlocked = {"s:1": OrderedDict({"old": None})}

    executor = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                ContentSafetyError("blocked"),
                ("ok", ["tool_search", "x"], [], None, None),
            ]
        )
    )
    service = SafetyRetryService(
        executor=executor,
        context=SimpleNamespace(build_messages=lambda **kwargs: kwargs["history"] + [{"role": "user"}]),
        session_manager=SimpleNamespace(save_async=AsyncMock()),
        tools=SimpleNamespace(get_always_on_names=lambda: {"always"}),
        discovery=discovery,
        tool_search_enabled=True,
        memory_window=10,
    )

    content, tools_used, chain, thinking = asyncio.run(service.run(_msg(), _session()))

    assert content == "ok"
    assert tools_used == ["tool_search", "x"]
    assert chain == []
    assert thinking is None
    assert "x" in discovery._unlocked["s:1"]


def test_safety_retry_context_length_all_fail_returns_fallback():
    service = SafetyRetryService(
        executor=SimpleNamespace(execute=AsyncMock(side_effect=[ContextLengthError("long")] * 3)),
        context=SimpleNamespace(build_messages=lambda **kwargs: kwargs["history"] + [{"role": "user"}]),
        session_manager=SimpleNamespace(save_async=AsyncMock()),
        tools=SimpleNamespace(get_always_on_names=lambda: {"always"}),
        discovery=ToolDiscoveryState(),
        tool_search_enabled=False,
        memory_window=10,
    )

    content, tools_used, chain, _thinking = asyncio.run(service.run(_msg(), _session()))
    assert "上下文过长" in content
    assert tools_used == []
    assert chain == []
