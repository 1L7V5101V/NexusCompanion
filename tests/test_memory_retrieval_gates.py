import asyncio
from typing import Any, cast
from unittest.mock import MagicMock

from agent.loop import AgentLoop, GateController
from agent.provider import LLMResponse
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry


class _NoopTool(Tool):
    @property
    def name(self) -> str:
        return "noop"

    @property
    def description(self) -> str:
        return "noop"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return "ok"


class _Provider:
    def __init__(self, texts: list[str] | None = None) -> None:
        self._texts = list(texts or [])

    async def chat(self, **kwargs):
        if self._texts:
            return LLMResponse(content=self._texts.pop(0), tool_calls=[])
        return LLMResponse(
            content='{"decision":"RETRIEVE","confidence":"high"}', tool_calls=[]
        )


def _make_loop(provider: _Provider, **kwargs: Any) -> AgentLoop:
    tools = ToolRegistry()
    tools.register(_NoopTool())
    return AgentLoop(
        bus=MagicMock(),
        provider=cast(Any, provider),
        light_provider=cast(Any, provider),
        tools=tools,
        session_manager=MagicMock(),
        workspace=MagicMock(),
        **kwargs,
    )


def test_route_gate_no_retrieve_when_high_confidence_no_retrieve():
    loop = _make_loop(
        _Provider(
            ['{"decision":"NO_RETRIEVE","rewritten_query":"q","confidence":"high"}']
        ),
        memory_route_intention_enabled=True,
    )
    needs, rewritten, reason, _ = asyncio.run(
        loop._decide_history_retrieval(user_msg="你好", metadata={})
    )
    assert needs is False
    assert rewritten == "q"
    assert reason == "ok"


def test_route_gate_fail_open_on_low_confidence():
    loop = _make_loop(
        _Provider(
            ['{"decision":"NO_RETRIEVE","rewritten_query":"q","confidence":"low"}']
        ),
        memory_route_intention_enabled=True,
    )
    needs, _, reason, _ = asyncio.run(
        loop._decide_history_retrieval(user_msg="你好", metadata={})
    )
    assert needs is True
    assert reason == "ok"


def test_sufficiency_gate_skip_history_when_high_confidence_skip():
    loop = _make_loop(
        _Provider(['{"decision":"SKIP_INJECTION","confidence":"high"}']),
        memory_sufficiency_check_enabled=True,
    )
    include, reason, _ = asyncio.run(
        loop._decide_history_injection_sufficiency(
            user_msg="今天天气如何",
            procedure_items=[],
            history_items=[{"memory_type": "event", "summary": "用户昨天聊了游戏"}],
        )
    )
    assert include is False
    assert reason == "ok"


def test_gate_controller_does_not_auto_enable_when_initially_disabled():
    ctrl = GateController(
        enabled=True,
        baseline_p95_ms=1000,
        initial_sufficiency_enabled=False,
        allow_auto_enable=False,
        eval_every_seconds=1,
        recover_windows=1,
    )
    now = 1000.0
    ctrl.record_latency(900, now)
    enabled, reason = ctrl.tick(now + 2)
    assert enabled is False
    assert reason in {"auto_enable_blocked", "no_samples", "not_due", "waiting_recover"}


def test_flow_execution_state_not_triggered_by_single_char_xian_zai():
    loop = _make_loop(_Provider(), memory_route_intention_enabled=True)
    assert loop._is_flow_execution_state("我先问个问题", {}) is False
    assert loop._is_flow_execution_state("我们再看看", {}) is False
    assert loop._is_flow_execution_state("先查再说", {}) is True


def test_flow_execution_state_uses_task_tool_flag_not_any_tool_count():
    loop = _make_loop(_Provider(), memory_route_intention_enabled=True)
    assert (
        loop._is_flow_execution_state(
            "普通问题",
            {"last_turn_tool_calls_count": 3, "last_turn_had_task_tool": False},
        )
        is False
    )
    assert (
        loop._is_flow_execution_state(
            "普通问题",
            {"last_turn_tool_calls_count": 0, "last_turn_had_task_tool": True},
        )
        is True
    )
