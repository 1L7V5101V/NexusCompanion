from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

from agent.core.llm_provider import ProviderLLMAdapter
from agent.core.reasoner import DefaultReasoner
from agent.core.runtime_support import ToolDiscoveryState
from agent.core.types import ChatMessage, ContextBundle, InboundMessage
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from agent.tools.tool_search import ToolSearchTool
from infra.providers.llm_provider import LLMResponse, ToolCall


class _DummyTool(Tool):
    def __init__(self, name: str = "dummy") -> None:
        self._name = name
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"{self._name}-ok"


class _Provider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("provider.chat called more than expected")
        return self._responses.pop(0)


class _ProcedureMemory:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items
        self.queries: list[list[str]] = []

    def keyword_match_procedures(self, tokens: list[str]) -> list[dict[str, Any]]:
        self.queries.append(tokens)
        return list(self._items)


@pytest.mark.asyncio
async def test_legacy_llm_provider_adapter_uses_system_prompt():
    provider = _Provider([LLMResponse(content="ok", tool_calls=[])])
    adapter = ProviderLLMAdapter(
        cast(Any, provider),
        model="m",
        max_tokens=256,
    )

    await adapter.step(
        system_prompt="SYSTEM",
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
    )

    first_messages = provider.calls[0]["messages"]
    assert first_messages[0]["role"] == "system"
    assert first_messages[0]["content"] == "SYSTEM"


@pytest.mark.asyncio
async def test_default_reasoner_uses_history_system_prompt_and_tool_loop():
    provider = _Provider(
        [
            LLMResponse(content="", tool_calls=[ToolCall("c1", "dummy", {})]),
            LLMResponse(content="final", tool_calls=[]),
        ]
    )
    registry = ToolRegistry()
    registry.register(_DummyTool(), always_on=True)
    reasoner = DefaultReasoner(
        llm_provider=ProviderLLMAdapter(
            cast(Any, provider),
            model="m",
            max_tokens=256,
        ),
        max_iterations=4,
        max_tokens=256,
        tool_registry=registry,
        tool_search_enabled=False,
    )

    result = await reasoner.run(
        msg=InboundMessage(
            channel="cli",
            session_key="cli:1",
            sender="u",
            content="now",
        ),
        system_prompt="SYSTEM_PROMPT",
        context=ContextBundle(
            history=[ChatMessage(role="user", content="old history")],
            memory_blocks=[],
            metadata={},
        ),
        tools=registry.get_tools(),
    )

    first_messages = provider.calls[0]["messages"]
    assert first_messages[0]["role"] == "system"
    assert first_messages[0]["content"] == "SYSTEM_PROMPT"
    assert any(m.get("content") == "old history" for m in first_messages)
    assert result.reply == "final"
    assert [call.name for call in result.invocations] == ["dummy"]
    assert result.metadata["tools_used"] == ["dummy"]


@pytest.mark.asyncio
async def test_default_reasoner_preserves_multimodal_image_blocks(tmp_path: Path):
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    provider = _Provider([LLMResponse(content="done", tool_calls=[])])
    reasoner = DefaultReasoner(
        llm_provider=ProviderLLMAdapter(
            cast(Any, provider),
            model="m",
            max_tokens=256,
        ),
        max_iterations=2,
        max_tokens=256,
        tool_search_enabled=False,
    )

    await reasoner.run(
        msg=InboundMessage(
            channel="cli",
            session_key="cli:img",
            sender="u",
            content="看下这张图",
            media=[str(image_path)],
            timestamp=datetime.now(),
        ),
        system_prompt="SYSTEM",
        context=ContextBundle(history=[], memory_blocks=[], metadata={}),
        tools=[],
    )

    user_message = next(
        item for item in provider.calls[0]["messages"] if item.get("role") == "user"
    )
    content = user_message["content"]
    assert isinstance(content, list)
    assert any(block.get("type") == "image_url" for block in content)
    assert any(block.get("type") == "text" and block.get("text") == "看下这张图" for block in content)


@pytest.mark.asyncio
async def test_default_reasoner_intercepts_procedure_before_tool_execution():
    provider = _Provider(
        [
            LLMResponse(content="", tool_calls=[ToolCall("c1", "dummy", {})]),
            LLMResponse(content="after-intercept", tool_calls=[]),
        ]
    )
    registry = ToolRegistry()
    dummy = _DummyTool()
    registry.register(dummy, always_on=True)
    memory = _ProcedureMemory(
        [
            {
                "id": "proc-1",
                "summary": "执行 dummy 前必须先查看 SOP",
                "intercept": True,
            }
        ]
    )
    reasoner = DefaultReasoner(
        llm_provider=ProviderLLMAdapter(
            cast(Any, provider),
            model="m",
            max_tokens=256,
        ),
        max_iterations=4,
        max_tokens=256,
        tool_registry=registry,
        tool_search_enabled=False,
        memory_port=cast(Any, memory),
    )

    result = await reasoner.run(
        msg=InboundMessage(
            channel="cli",
            session_key="cli:proc",
            sender="u",
            content="执行 dummy",
        ),
        system_prompt="SYSTEM",
        context=ContextBundle(history=[], memory_blocks=[], metadata={}),
        tools=registry.get_tools(),
    )

    assert result.reply == "after-intercept"
    assert dummy.calls == []
    assert memory.queries

    second_call_messages = provider.calls[1]["messages"]
    tool_message = next(item for item in second_call_messages if item.get("role") == "tool")
    assert "执行拦截" in tool_message["content"]


@pytest.mark.asyncio
async def test_default_reasoner_updates_tool_discovery_lru():
    provider = _Provider(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall("s1", "tool_search", {"query": "hidden"})],
            ),
            LLMResponse(content="", tool_calls=[ToolCall("h1", "hidden_tool", {})]),
            LLMResponse(content="done", tool_calls=[]),
        ]
    )
    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry), always_on=True, risk="read-only")
    hidden = _DummyTool("hidden_tool")
    registry.register(hidden)
    discovery = ToolDiscoveryState()
    reasoner = DefaultReasoner(
        llm_provider=ProviderLLMAdapter(
            cast(Any, provider),
            model="m",
            max_tokens=256,
        ),
        max_iterations=4,
        max_tokens=256,
        tool_registry=registry,
        tool_search_enabled=True,
        tool_discovery=discovery,
    )

    result = await reasoner.run(
        msg=InboundMessage(
            channel="cli",
            session_key="cli:lru",
            sender="u",
            content="调用 hidden",
        ),
        system_prompt="SYSTEM",
        context=ContextBundle(history=[], memory_blocks=[], metadata={}),
        tools=registry.get_tools(),
    )

    assert result.reply == "done"
    assert hidden.calls
    assert "hidden_tool" in discovery.get_preloaded("cli:lru")


@pytest.mark.asyncio
async def test_default_reasoner_tool_loop_guard_falls_back_to_summary():
    provider = _Provider(
        [
            LLMResponse(content="", tool_calls=[ToolCall("c1", "dummy", {})]),
            LLMResponse(content="", tool_calls=[ToolCall("c1", "dummy", {})]),
            LLMResponse(content="", tool_calls=[ToolCall("c1", "dummy", {})]),
            LLMResponse(content="summary-after-loop", tool_calls=[]),
        ]
    )
    registry = ToolRegistry()
    dummy = _DummyTool()
    registry.register(dummy, always_on=True)
    reasoner = DefaultReasoner(
        llm_provider=ProviderLLMAdapter(
            cast(Any, provider),
            model="m",
            max_tokens=256,
        ),
        max_iterations=6,
        max_tokens=256,
        tool_registry=registry,
        tool_search_enabled=False,
    )

    result = await reasoner.run(
        msg=InboundMessage(
            channel="cli",
            session_key="cli:loop",
            sender="u",
            content="陷入循环",
        ),
        system_prompt="SYSTEM",
        context=ContextBundle(history=[], memory_blocks=[], metadata={}),
        tools=registry.get_tools(),
    )

    assert result.reply == "summary-after-loop"
    assert len(dummy.calls) == 2
    assert result.metadata["tools_used"] == ["dummy", "dummy"]


@pytest.mark.asyncio
async def test_default_reasoner_preflight_lists_deferred_tools_but_not_preloaded():
    provider = _Provider([LLMResponse(content="done", tool_calls=[])])
    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry), always_on=True, risk="read-only")
    registry.register(_DummyTool("hidden_a"))
    registry.register(_DummyTool("hidden_b"))
    discovery = ToolDiscoveryState()
    discovery.update("cli:pref", ["hidden_b"], registry.get_always_on_names())
    reasoner = DefaultReasoner(
        llm_provider=ProviderLLMAdapter(cast(Any, provider), model="m", max_tokens=256),
        max_iterations=2,
        max_tokens=256,
        tool_registry=registry,
        tool_search_enabled=True,
        tool_discovery=discovery,
    )

    await reasoner.run(
        msg=InboundMessage(
            channel="cli",
            session_key="cli:pref",
            sender="u",
            content="test",
            timestamp=datetime.now(),
        ),
        system_prompt="SYSTEM",
        context=ContextBundle(history=[], memory_blocks=[], metadata={}),
        tools=registry.get_tools(),
    )

    preflight = next(
        str(item.get("content", ""))
        for item in provider.calls[0]["messages"]
        if item.get("role") == "system" and "本轮时间锚点" in str(item.get("content", ""))
    )
    assert "hidden_a" in preflight
    assert "hidden_b" not in preflight


@pytest.mark.asyncio
async def test_default_reasoner_blocks_deferred_tool_until_select_loaded():
    provider = _Provider(
        [
            LLMResponse(content="", tool_calls=[ToolCall("c1", "schedule", {})]),
            LLMResponse(content="done", tool_calls=[]),
        ]
    )
    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry), always_on=True, risk="read-only")
    schedule = _DummyTool("schedule")
    registry.register(schedule)
    reasoner = DefaultReasoner(
        llm_provider=ProviderLLMAdapter(cast(Any, provider), model="m", max_tokens=256),
        max_iterations=3,
        max_tokens=256,
        tool_registry=registry,
        tool_search_enabled=True,
    )

    result = await reasoner.run(
        msg=InboundMessage(
            channel="cli",
            session_key="cli:hidden",
            sender="u",
            content="设置提醒",
        ),
        system_prompt="SYSTEM",
        context=ContextBundle(history=[], memory_blocks=[], metadata={}),
        tools=registry.get_tools(),
    )

    assert result.reply == "done"
    assert schedule.calls == []
    tool_message = next(
        item
        for item in provider.calls[1]["messages"]
        if item.get("role") == "tool"
    )
    assert "select:schedule" in tool_message["content"]


@pytest.mark.asyncio
async def test_default_reasoner_reflect_prompt_includes_non_intercept_hint_only():
    provider = _Provider(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall("c1", "shell", {"command": "pacman -S jq"})],
            ),
            LLMResponse(content="done", tool_calls=[]),
        ]
    )
    registry = ToolRegistry()
    shell = _DummyTool("shell")
    registry.register(shell, always_on=True)
    memory = _ProcedureMemory(
        [
            {
                "id": "proc-1",
                "summary": "pacman 调用时必须加 --noconfirm",
                "intercept": False,
            }
        ]
    )
    reasoner = DefaultReasoner(
        llm_provider=ProviderLLMAdapter(cast(Any, provider), model="m", max_tokens=256),
        max_iterations=4,
        max_tokens=256,
        tool_registry=registry,
        tool_search_enabled=False,
        memory_port=cast(Any, memory),
    )

    await reasoner.run(
        msg=InboundMessage(
            channel="cli",
            session_key="cli:hint",
            sender="u",
            content="装 jq",
        ),
        system_prompt="SYSTEM",
        context=ContextBundle(history=[], memory_blocks=[], metadata={}),
        tools=registry.get_tools(),
    )

    tool_message = next(
        item for item in provider.calls[1]["messages"] if item.get("role") == "tool"
    )
    reflect_message = [
        item
        for item in provider.calls[1]["messages"]
        if item.get("role") == "system"
    ][-1]
    assert tool_message["content"] == "shell-ok"
    assert "【⚠️ 操作规范提醒 | 适用于本轮工具调用】" in reflect_message["content"]
    assert "--noconfirm" in reflect_message["content"]
