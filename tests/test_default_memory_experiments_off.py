"""实验开关关闭态负向测试（C13 验收 5，ADR-5）。

默认配置三个实验开关（hyde / query_rewrite / reranker）全关，零 light-LLM
调用：`_query_answer` 不生成 HyDE 假设、`_maybe_rewrite_query` 原样返回、
retriever 不注入 reranker。用 `__new__` + 手填字段的最小 engine 假造，
chat 全链路不得被调用。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from core.memory.engine import (
    MemoryQuery,
    MemoryQueryFilters,
    MemoryScope,
)
from infra.storage.interfaces import TenantContext
from plugins.default_memory.config import load_default_memory_config
from plugins.default_memory.engine import DefaultMemoryEngine


def _engine(
    *,
    light_provider: Any | None = None,
    retriever: Any | None = None,
    default_config: Any | None = None,
) -> DefaultMemoryEngine:
    engine = DefaultMemoryEngine.__new__(DefaultMemoryEngine)
    engine._config = SimpleNamespace(model="lm", light_model="light-lm")
    engine._default_config = default_config or load_default_memory_config()
    engine._workspace = Path(".")
    engine._provider = None
    engine._light_provider = light_provider
    engine._light_model = "light-lm"
    engine._v2_store = None
    engine._storage_runtime = None
    engine._embedder = None
    engine._memorizer = None
    engine._retriever = retriever
    engine._tagger = None
    engine._post_response_worker = None
    engine._event_bus = None
    engine.closeables = []
    return engine


def _query() -> MemoryQuery:
    return MemoryQuery(
        text="用户偏好中文回复",
        tenant=TenantContext(tenant_id="test"),
        intent="answer",
        scope=MemoryScope(channel="cli", chat_id="1"),
        filters=MemoryQueryFilters(kinds=("preference",), hints={}),
        limit=3,
    )


def test_default_config_has_all_experiments_off() -> None:
    cfg = load_default_memory_config()
    assert cfg.retrieval.experimental.hyde_enabled is False
    assert cfg.retrieval.experimental.query_rewrite_enabled is False
    assert cfg.retrieval.experimental.reranker_enabled is False


async def test_query_answer_makes_zero_light_llm_calls_when_off() -> None:
    chat = AsyncMock(side_effect=AssertionError("light LLM 不应被调用"))
    engine = _engine(
        light_provider=SimpleNamespace(chat=chat),
        retriever=SimpleNamespace(retrieve=AsyncMock(return_value=[])),
    )

    result = await engine._query_answer(_query())

    assert result.records == []
    # HyDE 关：无假设；rewrite 关：query 未改写。
    assert result.trace["hyde_enabled"] is False
    assert result.trace["hyde_hypotheses"] == []
    assert result.trace["query_rewrite_applied"] is False
    chat.assert_not_awaited()


async def test_maybe_rewrite_query_returns_unchanged_when_off() -> None:
    chat = AsyncMock(side_effect=AssertionError("light LLM 不应被调用"))
    engine = _engine(light_provider=SimpleNamespace(chat=chat))

    queried, applied = await engine._maybe_rewrite_query(
        "用户偏好中文回复", intent="answer"
    )

    assert queried == "用户偏好中文回复"
    assert applied is False
    chat.assert_not_awaited()


async def test_query_answer_with_hits_still_zero_llm_calls() -> None:
    chat = AsyncMock(side_effect=AssertionError("light LLM 不应被调用"))
    retriever = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=[
                {
                    "id": "m1",
                    "summary": "用户偏好中文回复",
                    "score": 0.88,
                    "source_ref": "cli:1@seed",
                    "memory_type": "preference",
                    "extra_json": {"origin": "test"},
                }
            ]
        )
    )
    engine = _engine(
        light_provider=SimpleNamespace(chat=chat),
        retriever=retriever,
    )

    result = await engine._query_answer(_query())

    assert len(result.records) == 1
    assert result.records[0].id == "m1"
    assert result.trace["query_rewrite_applied"] is False
    chat.assert_not_awaited()


def test_reranker_wiring_gated_by_config_switch() -> None:
    """reranker 默认关：__init__ 只在开关打开时才构造 LightLLMReranker。"""
    import inspect

    from plugins.default_memory import engine as engine_module

    init_src = inspect.getsource(engine_module.DefaultMemoryEngine.__init__)
    assert "if experimental.reranker_enabled:" in init_src
    assert "LightLLMReranker(chat, model=self._light_model)" in init_src
    # 默认配置关闭状态不变（防配置漂移）。
    cfg = load_default_memory_config()
    assert cfg.retrieval.experimental.reranker_enabled is False


async def test_hyde_gate_short_circuits_hypothesis_calls() -> None:
    """HyDE 关时 _query_answer 不触碰 _gen_hypothesis（async 分支不可达）。"""
    chat = AsyncMock(side_effect=AssertionError("light LLM 不应被调用"))
    calls: list[str] = []

    async def fake_gen(self: Any, query: str, style: str) -> str | None:
        calls.append(style)
        return f"{style}:{query}"

    engine = _engine(
        light_provider=SimpleNamespace(chat=chat),
        retriever=SimpleNamespace(retrieve=AsyncMock(return_value=[])),
    )
    engine._gen_hypothesis = fake_gen  # type: ignore[method-assign]

    await engine._query_answer(_query())

    assert calls == []
    chat.assert_not_awaited()