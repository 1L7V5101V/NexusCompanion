"""
tool_search / list_tools 路由机制单元测试。

覆盖三条 runtime 行为，对应设计决策 A/B：

  U1  unknown_tool_error_hint
      工具不在 registry → 错误消息包含可用作 tool_search query 的关键词
      （改动二生效前此测试失败，生效后通过）

  U2  known_invisible_auto_unlock_no_search
      工具在 registry 但不在 visible_names → 自动解锁执行，tool_search 不被调用
      （验证三段式路由的直通捷径已生效）

  U3  list_tools_returns_full_registry
      list_tools 返回全量注册工具，不受 visible_names 约束
      （验证设计决策 B 的实现与预期一致）
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from agent.looping.core import AgentLoop, AgentLoopConfig, AgentLoopDeps, LLMConfig
from agent.memory import MemoryStore
from agent.provider import LLMResponse, ToolCall
from agent.tools.base import Tool
from agent.tools.list_tools import ListToolsTool
from agent.tools.registry import ToolRegistry
from agent.tools.tool_search import ToolSearchTool
from core.memory.port import DefaultMemoryPort


# ── 工具桩 ────────────────────────────────────────────────────────────────────


class _DummyTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"dummy tool {self._name}"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"ok:{self._name}"


class _FakeProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)

    async def chat(self, **kwargs: Any) -> LLMResponse:
        if not self._responses:
            raise AssertionError("provider.chat 被调用次数超过预期")
        return self._responses.pop(0)


# ── 工厂 ──────────────────────────────────────────────────────────────────────


def _make_loop(
    tmp_path: Path,
    provider: _FakeProvider,
    registry: ToolRegistry,
) -> AgentLoop:
    return AgentLoop(
        AgentLoopDeps(
            bus=MagicMock(),
            provider=cast(Any, provider),
            tools=registry,
            session_manager=MagicMock(),
            workspace=tmp_path,
            memory_port=DefaultMemoryPort(MemoryStore(tmp_path)),
        ),
        AgentLoopConfig(llm=LLMConfig(max_iterations=10, tool_search_enabled=True)),
    )


def _base_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolSearchTool(reg), always_on=True, tags=["meta"], risk="read-only")
    reg.register(ListToolsTool(reg), always_on=True, tags=["meta"], risk="read-only")
    return reg


# ── U1: 未知工具错误消息含 query hint ─────────────────────────────────────────


class TestUnknownToolErrorHint:
    def test_error_message_contains_suggested_query(self, tmp_path: Path) -> None:
        """U1: 模型幻觉调 'rss_manage'（不在 registry），错误消息应含可用作搜索的关键词。

        改动二（query hint）生效前：消息为固定文案，不含工具名推导的关键词 → 测试失败。
        改动二生效后：消息含 'rss manage' 和 'tool_search' → 测试通过。
        """
        reg = _base_registry()
        # provider: 第一步幻觉调用不存在工具，第二步收到错误后直接结束
        provider = _FakeProvider(
            [
                LLMResponse(content="", tool_calls=[ToolCall("c1", "rss_manage", {})]),
                LLMResponse(content="好的", tool_calls=[]),
            ]
        )
        loop = _make_loop(tmp_path, provider, reg)

        # 捕获工具调用结果（通过查 tool_chain）
        _, _, tool_chain, _, _ = asyncio.run(
            loop._run_agent_loop([{"role": "user", "content": "管理RSS"}])
        )

        # 找到 rss_manage 的调用结果
        error_result = None
        for step in tool_chain:
            for call in step.get("calls", []):
                if call["name"] == "rss_manage":
                    error_result = call["result"]
                    break

        assert error_result is not None, "rss_manage 调用记录不存在"
        # 改动二生效后：错误消息应含工具名转换的查询建议
        assert "rss manage" in error_result, (
            f"错误消息未包含 query hint 'rss manage'，当前消息：{error_result!r}"
        )
        assert "tool_search" in error_result, (
            f"错误消息未引导调用 tool_search，当前消息：{error_result!r}"
        )


# ── U2: 已知但不可见的工具自动解锁，不调 tool_search ──────────────────────────


class TestKnownInvisibleAutoUnlock:
    def test_auto_unlock_without_tool_search(self, tmp_path: Path) -> None:
        """U2: 模型直接调用在 registry 但不在 visible_names 的工具，
        runtime 应自动解锁执行，且 tool_search 不应被调用。

        这是三段式路由中"已知工具名 → 直通"路径的 runtime 验证。
        """
        reg = _base_registry()
        hidden = _DummyTool("schedule")
        reg.register(
            hidden,
            tags=["scheduling"],
            risk="write",
            search_keywords=["定时任务"],
        )

        provider = _FakeProvider(
            [
                # 模型直接调 schedule（不经过 tool_search）
                LLMResponse(
                    content="",
                    tool_calls=[ToolCall("c1", "schedule", {"action": "remind", "at": "08:00"})],
                ),
                LLMResponse(content="已设置提醒", tool_calls=[]),
            ]
        )
        loop = _make_loop(tmp_path, provider, reg)

        _, tools_used, _, _, _ = asyncio.run(
            loop._run_agent_loop([{"role": "user", "content": "明天8点提醒我"}])
        )

        assert "schedule" in tools_used, "schedule 应被自动解锁并执行"
        assert len(hidden.calls) == 1, "schedule 应恰好执行一次"
        assert "tool_search" not in tools_used, (
            "已知工具名直通路径下，tool_search 不应被调用"
        )


# ── U3: list_tools 返回全量，不受 visible_names 约束 ──────────────────────────


class TestListToolsFullRegistry:
    def test_returns_all_registered_tools_not_visible_subset(self) -> None:
        """U3: list_tools.execute() 返回所有注册工具（全量），
        与当前 visible_names 无关，不是当前可见子集。

        这是设计决策 B（list_tools = 全量目录）的实现验证。
        """
        reg = _base_registry()
        # 注册 5 个额外工具，都不设 always_on（不在默认 visible_names 里）
        for name in ["schedule", "feed_manage", "fitbit_health_snapshot", "memorize", "shell"]:
            reg.register(_DummyTool(name))

        tool = ListToolsTool(reg)
        raw = asyncio.run(tool.execute())
        data = json.loads(raw)

        # list_tools 自身和 tool_search 排除在结果之外
        names = [t["name"] for t in data["tools"]]
        assert "tool_search" not in names
        assert "list_tools" not in names

        # 5 个额外工具全部出现（全量，不受 visible_names 过滤）
        for name in ["schedule", "feed_manage", "fitbit_health_snapshot", "memorize", "shell"]:
            assert name in names, f"{name} 应在 list_tools 全量结果中，但未找到"

        assert data["total"] == 5, f"预期 5 个工具，实际 {data['total']}"

    def test_tag_filter_still_works_on_full_registry(self) -> None:
        """U3b: tag 过滤在全量基础上生效，不是在 visible_names 上过滤。"""
        reg = _base_registry()
        reg.register(_DummyTool("schedule"), tags=["scheduling"])
        reg.register(_DummyTool("memorize"), tags=["memory"])

        tool = ListToolsTool(reg)
        raw = asyncio.run(tool.execute(tag="scheduling"))
        data = json.loads(raw)

        names = [t["name"] for t in data["tools"]]
        assert "schedule" in names
        assert "memorize" not in names
