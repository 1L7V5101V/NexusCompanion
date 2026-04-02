import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent.looping.safety_retry import SafetyRetryService
from agent.looping.tool_execution import ToolDiscoveryState
from agent.provider import ContentSafetyError, ContextLengthError


def _stub_runtime_guard_context(*, preflight_prompt: str | None = None) -> dict[str, str]:
    if not preflight_prompt:
        return {}
    return {"preflight": preflight_prompt}


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
        context=SimpleNamespace(
            build_messages=lambda **kwargs: kwargs["history"] + [{"role": "user"}],
            build_runtime_guard_context=_stub_runtime_guard_context,
        ),
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
        executor=SimpleNamespace(execute=AsyncMock(side_effect=[ContextLengthError("long")] * 7)),
        context=SimpleNamespace(
            build_messages=lambda **kwargs: kwargs["history"] + [{"role": "user"}],
            build_runtime_guard_context=_stub_runtime_guard_context,
        ),
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


def test_safety_retry_context_length_trims_dynamic_sections_before_history():
    calls: list[dict] = []

    def _build_messages(**kwargs):
        calls.append(
            {
                "history_len": len(kwargs["history"]),
                "disabled_sections": set(kwargs.get("disabled_sections") or set()),
            }
        )
        return kwargs["history"] + [{"role": "user"}]

    executor = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                ContextLengthError("long"),
                ("ok", [], [], None, None),
            ]
        )
    )
    service = SafetyRetryService(
        executor=executor,
        context=SimpleNamespace(
            build_messages=_build_messages,
            build_runtime_guard_context=_stub_runtime_guard_context,
        ),
        session_manager=SimpleNamespace(save_async=AsyncMock()),
        tools=SimpleNamespace(get_always_on_names=lambda: {"always"}),
        discovery=ToolDiscoveryState(),
        tool_search_enabled=False,
        memory_window=10,
    )

    content, tools_used, chain, _thinking = asyncio.run(service.run(_msg(), _session()))
    assert content == "ok"
    assert tools_used == []
    assert chain == []
    assert calls[0]["history_len"] == 6
    assert calls[0]["disabled_sections"] == set()
    assert calls[1]["history_len"] == 6
    assert calls[1]["disabled_sections"] == {"skills_catalog"}
