import asyncio
from typing import Any, cast

from agent.looping.ports import LLMConfig, LLMServices
from agent.looping.tool_execution import ToolDiscoveryState, TurnExecutor
from agent.provider import LLMResponse, ToolCall
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry


class _DummyTool(Tool):
    @property
    def name(self) -> str:
        return "dummy"

    @property
    def description(self) -> str:
        return "dummy"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return "dummy-ok"


class _Provider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = [
            LLMResponse(content="", tool_calls=[ToolCall("c1", "dummy", {})]),
            LLMResponse(content="final", tool_calls=[]),
        ]

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def test_turn_executor_runs_tool_then_returns_final():
    provider = _Provider()
    tools = ToolRegistry()
    tools.register(_DummyTool(), always_on=True)

    executor = TurnExecutor(
        llm=LLMServices(provider=cast(Any, provider), light_provider=cast(Any, provider)),
        llm_config=LLMConfig(model="m", max_iterations=4, max_tokens=512),
        tools=tools,
        discovery=ToolDiscoveryState(),
        memory_port=cast(Any, type("_M", (), {"keyword_match_procedures": lambda self, _: []})()),
        tool_search_enabled=False,
    )

    content, tools_used, tool_chain, visible, thinking = asyncio.run(
        executor.execute([{"role": "user", "content": "hi"}])
    )

    assert content == "final"
    assert tools_used == ["dummy"]
    assert len(tool_chain) == 1
    assert visible is None
    assert thinking is None
    # 第一次调用时，preflight prompt 已追加到消息列表
    first_messages = provider.calls[0]["messages"]
    assert any("本轮时间锚点" in str(m.get("content", "")) for m in first_messages)
