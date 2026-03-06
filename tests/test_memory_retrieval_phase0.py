import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

from agent.config import Config
from agent.loop import AgentLoop
from agent.provider import LLMResponse
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from memory2.retriever import Retriever
from session.manager import Session


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


class _FakeProvider:
    async def chat(self, **kwargs):
        return LLMResponse(content="ok", tool_calls=[])


def _write_config(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_memory_v2_top_k_history_compat_from_legacy_fields(tmp_path: Path):
    cfg_path = tmp_path / "config.json"
    _write_config(
        cfg_path,
        {
            "provider": "openai",
            "model": "x",
            "api_key": "k",
            "system_prompt": "s",
            "memory_v2": {
                "enabled": True,
                "retrieve_top_k": 9,
                "score_threshold": 0.5,
            },
        },
    )
    cfg = Config.load(cfg_path)
    assert cfg.memory_v2.top_k_history == 9
    assert cfg.memory_v2.retrieve_top_k == 9
    assert cfg.memory_v2.score_threshold_procedure == 0.60
    assert cfg.memory_v2.score_threshold_event == 0.68


def test_memory_v2_top_k_history_prefers_new_field(tmp_path: Path):
    cfg_path = tmp_path / "config.json"
    _write_config(
        cfg_path,
        {
            "provider": "openai",
            "model": "x",
            "api_key": "k",
            "system_prompt": "s",
            "memory_v2": {
                "enabled": True,
                "top_k_history": 12,
                "recall_top_k": 7,
                "retrieve_top_k": 5,
            },
        },
    )
    cfg = Config.load(cfg_path)
    assert cfg.memory_v2.top_k_history == 12
    assert cfg.memory_v2.retrieve_top_k == 12


def test_loop_updates_session_runtime_metadata(tmp_path: Path):
    tools = ToolRegistry()
    tools.register(_NoopTool())
    loop = AgentLoop(
        bus=MagicMock(),
        provider=cast(Any, _FakeProvider()),
        tools=tools,
        session_manager=MagicMock(),
        workspace=tmp_path,
    )
    session = Session("telegram:1")

    loop._update_session_runtime_metadata(
        session,
        tools_used=["web_search", "skill_action_status", "update_now"],
        tool_chain=[{"calls": [{"name": "a"}, {"name": "b"}]}],
    )

    assert session.metadata["last_turn_tool_calls_count"] == 2
    assert session.metadata["last_turn_had_task_tool"] is True
    assert "skill_action_status" in session.metadata["recent_task_tools"]
    assert "update_now" in session.metadata["recent_task_tools"]
    assert isinstance(session.metadata.get("last_turn_ts"), str)

    loop._update_session_runtime_metadata(
        session,
        tools_used=["web_search"],
        tool_chain=[{"calls": [{"name": "c"}]}],
    )

    assert session.metadata["last_turn_tool_calls_count"] == 1
    assert isinstance(session.metadata.get("_task_tools_turns"), list)
    assert len(session.metadata["_task_tools_turns"]) <= 2


def test_retriever_select_for_injection_applies_type_threshold_and_relative_delta():
    retriever = Retriever(
        store=MagicMock(),
        embedder=MagicMock(),
        score_threshold=0.45,
        score_thresholds={
            "procedure": 0.60,
            "preference": 0.60,
            "event": 0.68,
            "profile": 0.68,
        },
        relative_delta=0.06,
    )
    items = [
        {"id": "a", "memory_type": "event", "score": 0.74, "summary": "A"},
        {
            "id": "b",
            "memory_type": "event",
            "score": 0.67,
            "summary": "B",
        },  # 低于 event 阈值
        {"id": "c", "memory_type": "procedure", "score": 0.63, "summary": "C"},
        {
            "id": "d",
            "memory_type": "procedure",
            "score": 0.57,
            "summary": "D",
        },  # 低于 proc 阈值
    ]

    selected = retriever.select_for_injection(items)
    ids = {i["id"] for i in selected}
    assert "a" in ids
    assert "c" in ids
    assert "b" not in ids
    assert "d" not in ids


def test_retriever_select_for_injection_keeps_protected_procedure():
    retriever = Retriever(
        store=MagicMock(),
        embedder=MagicMock(),
        score_threshold=0.7,
        score_thresholds={
            "procedure": 0.7,
            "preference": 0.7,
            "event": 0.7,
            "profile": 0.7,
        },
    )
    items = [
        {
            "id": "p1",
            "memory_type": "procedure",
            "score": 0.42,
            "summary": "必须先查工具状态",
            "extra_json": {"tool_requirement": "skill_action_status"},
        },
        {"id": "e1", "memory_type": "event", "score": 0.75, "summary": "普通历史"},
    ]

    selected = retriever.select_for_injection(items)
    ids = {i["id"] for i in selected}
    assert "p1" in ids


def test_retriever_select_for_injection_can_drop_protected_when_guard_disabled():
    retriever = Retriever(
        store=MagicMock(),
        embedder=MagicMock(),
        score_threshold=0.7,
        score_thresholds={
            "procedure": 0.7,
            "preference": 0.7,
            "event": 0.7,
            "profile": 0.7,
        },
        sop_guard_enabled=False,
    )
    items = [
        {
            "id": "p1",
            "memory_type": "procedure",
            "score": 0.42,
            "summary": "必须先查工具状态",
            "extra_json": {"tool_requirement": "skill_action_status"},
        },
    ]

    selected = retriever.select_for_injection(items)
    ids = {i["id"] for i in selected}
    assert "p1" not in ids


def test_retriever_forced_limit_and_injected_ids_match_formatted_output():
    retriever = Retriever(
        store=MagicMock(),
        embedder=MagicMock(),
        inject_max_forced=1,
        sop_guard_enabled=True,
    )
    items = [
        {
            "id": "p1",
            "memory_type": "procedure",
            "score": 0.95,
            "summary": "规则1",
            "extra_json": {"tool_requirement": "a"},
        },
        {
            "id": "p2",
            "memory_type": "procedure",
            "score": 0.94,
            "summary": "规则2",
            "extra_json": {"tool_requirement": "b"},
        },
    ]
    block, injected_ids = retriever.format_injection_with_ids(items)
    assert "规则1" in block
    assert "规则2" not in block
    assert injected_ids == ["p1"]


def test_retriever_format_injection_with_ids_empty_input_returns_tuple():
    retriever = Retriever(store=MagicMock(), embedder=MagicMock())
    block, injected_ids = retriever.format_injection_with_ids([])
    assert block == ""
    assert injected_ids == []


def test_retriever_norm_limit_uses_config_without_hardcoded_cap():
    retriever = Retriever(
        store=MagicMock(),
        embedder=MagicMock(),
        inject_max_procedure_preference=6,
        score_threshold=0.0,
    )
    items = [
        {
            "id": f"n{i}",
            "memory_type": "preference",
            "score": 0.9 - i * 0.01,
            "summary": f"偏好{i}",
        }
        for i in range(6)
    ]
    block, injected_ids = retriever.format_injection_with_ids(items)
    for i in range(6):
        assert f"偏好{i}" in block
    assert len(injected_ids) == 6


def test_retriever_forced_block_not_dropped_by_char_budget():
    retriever = Retriever(
        store=MagicMock(),
        embedder=MagicMock(),
        inject_max_chars=120,
        inject_max_forced=1,
    )
    long_summary = "A" * 500
    items = [
        {
            "id": "p1",
            "memory_type": "procedure",
            "score": 0.9,
            "summary": long_summary,
            "extra_json": {"tool_requirement": "web_search"},
        },
        {
            "id": "e1",
            "memory_type": "event",
            "score": 0.89,
            "summary": "普通事件",
        },
    ]
    block, injected_ids = retriever.format_injection_with_ids(items)
    assert "【强制约束】" in block
    assert "p1" in injected_ids
