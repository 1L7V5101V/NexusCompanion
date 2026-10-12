from __future__ import annotations
from typing import Any, cast

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.prompting import is_context_frame
from agent.provider import LLMResponse
from proactive_v2.loop import ProactiveLoop
from proactive_v2.memory_optimizer import (
    MemoryOptimizer,
    MemoryOptimizerLoop,
)
from session.manager import (
    Session,
    SessionManager,
    _TOOL_RESULT_CHAR_BUDGET,
    _safe_filename,
)


@pytest.mark.asyncio
async def test_memory_optimizer_loop_and_memory_port_cover_paths(tmp_path: Path):
    """optimizer 的覆盖路径：合并走该租户的专表，SELF 仍走注入的文件 store。"""
    from tests.memory.profile_write_cases import TenantStores

    stores = TenantStores.create(tmp_path)
    tenant = "c18-opt-logic"
    memory = MagicMock()
    memory.read_self.return_value = "# Nexus 的自我认知\n## 人格与形象\n- x"
    memory.read_history.return_value = "history"
    memory.write_long_term = MagicMock()
    memory.write_self = MagicMock()
    store = stores.resolve(tenant)
    await store.write_section("long_term", "MEM")
    await store.append_entry(
        "pending",
        "- [identity] x",
        happened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    provider = MagicMock()
    provider.chat = AsyncMock(
        side_effect=[
            LLMResponse(content="merged"),
            LLMResponse(content="updated self"),
        ]
    )
    try:
        opt = MemoryOptimizer(
            memory, provider, "m", max_tokens=100, profile_stores=stores.resolve
        )
        opt._STEP_DELAY_SECONDS = 0
        await opt.optimize(tenant)

        assert (await store.read_section("long_term")).strip() == "merged"
        assert await store.read_entries("pending") == "", "合并吃进去的行要被取走"
        memory.write_long_term.assert_not_called()
        memory.write_self.assert_called_once()

        loop = MemoryOptimizerLoop(
            opt, interval_seconds=10, _now_fn=lambda: datetime(2025, 1, 1, 0, 0, 1)
        )
        assert loop._seconds_until_next_tick() >= 1.0
        loop.stop()
    finally:
        stores.close()


@pytest.mark.asyncio
async def test_session_manager_and_proactive_loop_cover_paths(tmp_path: Path):
    session = Session("telegram:1", tenant_id="test")
    session.add_message("user", "hi", media=["/tmp/a.png"])
    session.add_message(
        "assistant",
        "reply",
        proactive=True,
        state_summary_tag="tag",
        source_refs=[{"source_name": "Feed", "title": "T", "url": "https://x"}],
    )
    session.messages[-1]["tool_chain"] = [
        {"calls": [{"call_id": "1", "name": "tool", "arguments": {}, "result": "ok"}]}
    ]
    history = session.get_history()
    assert len(history) == 3
    assert history[0]["role"] == "user"
    assert history[1] == {"role": "assistant", "content": "[主动推送] reply"}
    assert history[2]["role"] == "user"
    assert is_context_frame(str(history[2]["content"]))
    assert _safe_filename("telegram:1") == "telegram_1"

    manager = SessionManager(tmp_path)
    manager.save(session)
    loaded = manager.get_or_create("test", "telegram:1")
    assert loaded.key == "telegram:1"
    await manager.append_messages(session, [{"role": "user", "content": "next"}])
    assert manager.list_sessions("test")
    assert manager.get_channel_metadata("test", "telegram")[0]["chat_id"] == "1"
    manager.invalidate("test", "telegram:1")

    loop = ProactiveLoop.__new__(ProactiveLoop)
    loop._cfg = SimpleNamespace(
        interval_seconds=10,
        score_weight_energy=0.5,
        tick_interval_s1=3,
        tick_interval_s0=4,
        tick_jitter=0.0,
    )
    loop._presence = None
    loop._trace_proactive_rate_decision = MagicMock()
    loop._scheduler = SimpleNamespace(next_interval=lambda base_score: 10)
    assert loop._next_interval() == 10
    loop._presence = SimpleNamespace(
        get_last_user_at=lambda session_key: datetime.now(timezone.utc)
    )
    loop._sense = SimpleNamespace(
        target_session_key=lambda: "telegram:1",
    )
    loop._rng = None
    loop._memory = SimpleNamespace(get_memory_context=lambda: "ctx")
    loop._sessions = SimpleNamespace(workspace=tmp_path)
    (tmp_path / "AGENTS.md").write_text("guide", encoding="utf-8")
    loop._sender = SimpleNamespace(send=AsyncMock(return_value=True))
    loop._engine = SimpleNamespace(tick=AsyncMock(return_value=0.2))
def test_session_get_history_returns_empty_when_window_is_zero():
    session = Session("cli:1")
    session.add_message("user", "hello")
    session.add_message("assistant", "world")

    assert session.get_history(max_messages=0) == []


def test_session_get_history_skips_cached_llm_frame_by_default():
    session = Session("cli:1")
    session.add_message("user", "old")
    session.add_message("assistant", "old reply")
    session.last_consolidated = 2
    user_content = "[当前消息时间: x]\nhello"
    session.add_message(
        "user",
        "hello",
        llm_context_frame="<system-reminder data-system-context-frame=\"true\">\n\n## retrieved_memory\n旧记忆",
        llm_user_content=user_content,
    )
    session.add_message("assistant", "world")

    history = session.get_history(start_index=session.last_consolidated)

    assert history == [
        {"role": "user", "content": user_content},
        {"role": "assistant", "content": "world"},
    ]


def test_session_get_history_replays_proactive_as_short_assistant_with_meta_frame():
    session = Session("cli:1")
    session.add_message(
        "assistant",
        "这是一条主动消息",
        proactive=True,
        source_refs=[
            {
                "source_name": "feed",
                "title": "标题",
                "url": "https://example.com/a",
            }
        ],
    )

    history = session.get_history()

    assert len(history) == 2
    assert history[0] == {"role": "assistant", "content": "[主动推送] 这是一条主动消息"}
    assert history[1]["role"] == "user"
    content = str(history[1]["content"])
    assert is_context_frame(content)
    assert "recent_proactive_message_meta" in content
    assert "proactive_meta" in content


def test_session_get_history_allows_proactive_assistant_boundary():
    session = Session("cli:1")
    session.add_message("user", "old")
    session.add_message("assistant", "old reply")
    session.add_message("assistant", "主动消息", proactive=True)
    session.add_message("user", "刚才那个")
    session.last_consolidated = 2

    history = session.get_history(start_index=session.last_consolidated)

    assert history == [
        {"role": "assistant", "content": "[主动推送] 主动消息"},
        {"role": "user", "content": "刚才那个"},
    ]


def test_session_get_history_rewinds_consolidated_index_to_user_boundary():
    session = Session("cli:1")
    session.add_message("user", "hello")
    session.add_message("assistant", "world")
    session.last_consolidated = 1

    history = session.get_history(start_index=session.last_consolidated)

    assert history[0] == {"role": "user", "content": "hello"}


def test_session_get_history_keeps_full_consolidated_tail():
    session = Session("cli:1")
    for i in range(5):
        session.add_message("user", f"u{i}")

    history = session.get_history(max_messages=2, start_index=0)

    assert session.consolidation_requested is False
    assert history == [
        {"role": "user", "content": "u0"},
        {"role": "user", "content": "u1"},
        {"role": "user", "content": "u2"},
        {"role": "user", "content": "u3"},
        {"role": "user", "content": "u4"},
    ]


def test_session_get_history_assistant_only_returns_empty():
    session = Session("cli:1")
    session.add_message("assistant", "a1")
    session.add_message("assistant", "a2")

    assert session.get_history(start_index=0) == []


def test_session_get_history_skips_legacy_context_frame_by_default():
    session = Session("cli:1")
    session.add_message(
        "user",
        "hello",
        llm_context_frame="[SYSTEM_CONTEXT_FRAME]\n\n## recent_context\n旧内容",
        llm_user_content="hello",
    )

    history = session.get_history(start_index=0)

    assert history == [{"role": "user", "content": "hello"}]


def test_session_get_history_does_not_inject_inference_tag():
    session = Session("cli:1")
    session.add_message("user", "hello")
    session.add_message("assistant", "world")

    history = session.get_history()

    assert history[-1] == {"role": "assistant", "content": "world"}


def test_session_get_history_keeps_reasoning_content():
    session = Session("cli:1")
    session.add_message("user", "hello")
    session.add_message(
        "assistant",
        "world",
        reasoning_content="先想一下",
    )
    session.messages[-1]["tool_chain"] = [
        {
            "text": "",
            "reasoning_content": "准备调用工具",
            "calls": [
                {
                    "call_id": "call-1",
                    "name": "dummy",
                    "arguments": {},
                    "result": "ok",
                }
            ],
        }
    ]

    history = session.get_history()

    assert history[1]["reasoning_content"] == "准备调用工具"
    assert history[-1]["reasoning_content"] == "先想一下"


def test_session_get_history_keeps_short_tool_results_after_consolidation_tail():
    session = Session("cli:1")
    session.last_consolidated = 0
    for i in range(3):
        session.add_message("user", f"u{i}")
        session.add_message("assistant", f"a{i}")
        session.messages[-1]["tool_chain"] = [
            {
                "text": "",
                "calls": [
                    {
                        "call_id": f"call-{i}",
                        "name": "dummy",
                        "arguments": {},
                        "result": f"result-{i}",
                    }
                ],
            }
        ]

    history = session.get_history(start_index=session.last_consolidated)
    tool_contents = [m["content"] for m in history if m.get("role") == "tool"]

    assert tool_contents == ["result-0", "result-1", "result-2"]


def test_session_get_history_truncates_long_tool_results_in_middle():
    session = Session("cli:1")
    long_result = "head-" + "x" * (_TOOL_RESULT_CHAR_BUDGET + 200) + "-tail"
    session.add_message("user", "u")
    session.add_message("assistant", "a")
    session.messages[-1]["tool_chain"] = [
        {
            "text": "",
            "calls": [
                {
                    "call_id": "call-1",
                    "name": "dummy",
                    "arguments": {},
                    "result": long_result,
                }
            ],
        }
    ]

    history = session.get_history()
    tool_content = next(m["content"] for m in history if m.get("role") == "tool")

    assert tool_content.startswith("Total output lines: 1\n\nhead-")
    assert "chars truncated" in tool_content
    assert tool_content.endswith("-tail")
    assert len(tool_content) < len(long_result)
