from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.core.history_types import HistoryMessage
from agent.core.retrieval_support import (
    decide_history_route,
    trace_route_reason,
)
from agent.core.runtime_support import LLMServices, MemoryConfig, MemoryServices
from agent.policies.history_route import DecisionMeta, HistoryRoutePolicy, RouteDecision
from agent.retrieval.default_pipeline import DefaultMemoryRetrievalPipeline
from agent.retrieval.protocol import RetrievalRequest
from agent.provider import LLMResponse
from memory2.query_rewriter import GateDecision


class _Provider:
    def __init__(self, texts: list[str] | None = None) -> None:
        self._texts = list(texts or [])

    async def chat(self, **kwargs):
        if self._texts:
            return LLMResponse(content=self._texts.pop(0), tool_calls=[])
        return LLMResponse(
            content='{"decision":"RETRIEVE","confidence":"high"}',
            tool_calls=[],
        )


def _make_pipeline(
    tmp_path: Path,
    *,
    provider: Any | None = None,
    query_rewriter: Any | None = None,
    sufficiency_checker: Any | None = None,
):
    memory_port = MagicMock()
    memory_port.retrieve_related = AsyncMock(return_value=[])
    memory_port.select_for_injection = MagicMock(side_effect=lambda items: items)
    memory_port.build_injection_block = MagicMock(return_value=("", []))
    pipeline = DefaultMemoryRetrievalPipeline(
        memory=MemoryServices(
            port=memory_port,
            query_rewriter=query_rewriter,
            sufficiency_checker=sufficiency_checker,
        ),
        memory_config=MemoryConfig(route_intention_enabled=True),
        llm=LLMServices(
            provider=cast(Any, provider or _Provider()),
            light_provider=cast(Any, provider or _Provider()),
        ),
        workspace=tmp_path,
        light_model="light",
    )
    return pipeline, memory_port


def _request() -> RetrievalRequest:
    return RetrievalRequest(
        message="我之前喜欢什么游戏",
        session_key="cli:1",
        channel="cli",
        chat_id="1",
        history=[HistoryMessage(role="user", content="old")],
        session_metadata={},
    )


def test_route_gate_no_retrieve_when_high_confidence_no_retrieve():
    decision = asyncio.run(
        decide_history_route(
            user_msg="你好",
            metadata={},
            recent_history="",
            light_provider=cast(
                Any,
                _Provider(
                    ['{"decision":"NO_RETRIEVE","rewritten_query":"q","confidence":"high"}']
                ),
            ),
            light_model="light",
            route_intention_enabled=True,
            gate_llm_timeout_ms=800,
            gate_max_tokens=96,
        )
    )
    assert decision.needs_history is False
    assert decision.rewritten_query == "q"
    assert trace_route_reason(decision) == "ok"


def test_route_gate_low_confidence_fail_open():
    decision = asyncio.run(
        decide_history_route(
            user_msg="你好",
            metadata={},
            recent_history="",
            light_provider=cast(
                Any,
                _Provider(
                    ['{"decision":"NO_RETRIEVE","rewritten_query":"q","confidence":"low"}']
                ),
            ),
            light_model="light",
            route_intention_enabled=True,
            gate_llm_timeout_ms=800,
            gate_max_tokens=96,
        )
    )
    assert decision.needs_history is True
    assert trace_route_reason(decision) == "ok"


def test_history_route_policy_flow_execution_uses_task_tool_state():
    assert HistoryRoutePolicy.is_flow_execution_state(
        "继续",
        {"recent_task_tools": ["task_note"]},
    )
    assert not HistoryRoutePolicy.is_flow_execution_state("先", {})


@pytest.mark.asyncio
async def test_retrieval_pipeline_prefers_query_rewriter_primary_path(tmp_path: Path):
    query_rewriter = MagicMock()
    query_rewriter.decide = AsyncMock(
        return_value=GateDecision(
            needs_episodic=False,
            episodic_query="改写 query",
            latency_ms=12,
        )
    )
    pipeline, memory_port = _make_pipeline(
        tmp_path,
        query_rewriter=query_rewriter,
    )

    result = await pipeline.retrieve(_request())

    assert result.block == ""
    query_rewriter.decide.assert_awaited_once()
    first_query = memory_port.retrieve_related.await_args_list[0].args[0]
    assert first_query == "我之前喜欢什么游戏"


@pytest.mark.asyncio
async def test_retrieval_pipeline_query_rewriter_path_returns_trace(tmp_path: Path):
    query_rewriter = MagicMock()
    query_rewriter.decide = AsyncMock(
        return_value=GateDecision(
            needs_episodic=True,
            episodic_query="游戏 偏好",
            latency_ms=8,
        )
    )
    pipeline, memory_port = _make_pipeline(
        tmp_path,
        query_rewriter=query_rewriter,
    )
    memory_port.retrieve_related = AsyncMock(
        side_effect=[
            [{"id": "p1", "memory_type": "procedure", "summary": "先查库", "score": 0.8}],
            [{"id": "h1", "memory_type": "event", "summary": "用户喜欢仁王", "score": 0.9}],
        ]
    )
    memory_port.select_for_injection = MagicMock(
        return_value=[{"id": "h1", "memory_type": "event", "summary": "用户喜欢仁王", "score": 0.9}]
    )
    memory_port.build_injection_block = MagicMock(
        return_value=("注入内容", ["h1"])
    )

    result = await pipeline.retrieve(_request())

    assert result.block == "注入内容"
    assert result.trace is not None
    assert result.trace.route_decision == "RETRIEVE"
    assert result.trace.rewritten_query == "游戏 偏好"


def test_trace_route_reason_marks_gate_exception():
    decision = RouteDecision(
        needs_history=True,
        rewritten_query="x",
        fail_open=True,
        latency_ms=1,
        meta=DecisionMeta(
            source="fallback",
            confidence="low",
            reason_code="llm_exception_fail_open",
        ),
    )
    assert trace_route_reason(decision) == "route_gate_exception"
