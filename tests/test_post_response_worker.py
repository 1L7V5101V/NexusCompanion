import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock

from memory2.post_response_worker import PostResponseMemoryWorker


class _DummyProvider:
    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        raise AssertionError("provider.chat should not be called in this test")


class _DummyRetriever:
    def __init__(self, results):
        self._results = results
        self.calls = []

    async def retrieve(self, query: str, memory_types=None):
        self.calls.append((query, tuple(memory_types or [])))
        return list(self._results)


class _DummyMemorizer:
    def __init__(self):
        self.save_item = AsyncMock(return_value="new:testid")
        self.supersede_batch = AsyncMock()


def test_post_worker_skips_implicit_when_semantic_dup_to_explicit():
    memorizer = _DummyMemorizer()
    retriever = _DummyRetriever(
        [
            {
                "id": "2b1ba4e802bf",
                "memory_type": "procedure",
                "score": 0.93,
                "summary": "记忆冲突时必须实时验证",
            }
        ]
    )
    worker = PostResponseMemoryWorker(
        memorizer=cast(Any, memorizer),
        retriever=cast(Any, retriever),
        light_provider=cast(Any, _DummyProvider()),
        light_model="test",
    )

    worker._collect_explicit_memorized = lambda tool_chain: (
        ["记忆冲突时必须实时验证，禁止推测，必须外部工具验证"],
        {"2b1ba4e802bf"},
    )
    worker._handle_invalidations = AsyncMock(
        side_effect=lambda *args, **kwargs: args[-1] if args else 0
    )
    worker._extract_implicit = AsyncMock(
        return_value=(
            [
                {
                    "summary": "在记忆冲突情况下应优先外部工具验证，不依赖内部推测",
                    "memory_type": "procedure",
                    "tool_requirement": None,
                    "steps": [],
                }
            ],
            256,
        )
    )

    asyncio.run(
        worker.run(
            user_msg="以后遇到冲突不要猜",
            agent_response="已记住",
            tool_chain=[],
            source_ref="test@post_response",
        )
    )

    memorizer.save_item.assert_not_called()


def test_post_worker_saves_implicit_when_not_duplicate_to_explicit():
    memorizer = _DummyMemorizer()
    retriever = _DummyRetriever([])
    worker = PostResponseMemoryWorker(
        memorizer=cast(Any, memorizer),
        retriever=cast(Any, retriever),
        light_provider=cast(Any, _DummyProvider()),
        light_model="test",
    )

    worker._collect_explicit_memorized = lambda tool_chain: (
        ["记得后续查 Steam 要走 MCP"],
        {"abcdef123456"},
    )
    worker._handle_invalidations = AsyncMock(
        side_effect=lambda *args, **kwargs: args[-1] if args else 0
    )
    worker._extract_implicit = AsyncMock(
        return_value=(
            [
                {
                    "summary": "回复结尾要主动追问用户最关心的点",
                    "memory_type": "preference",
                    "tool_requirement": None,
                    "steps": [],
                }
            ],
            256,
        )
    )

    asyncio.run(
        worker.run(
            user_msg="你以后多问我一句",
            agent_response="好的",
            tool_chain=[],
            source_ref="test@post_response",
        )
    )

    memorizer.save_item.assert_called_once()


def test_collect_explicit_memorized_accepts_long_mixed_id():
    worker = PostResponseMemoryWorker(
        memorizer=cast(Any, _DummyMemorizer()),
        retriever=cast(Any, _DummyRetriever([])),
        light_provider=cast(Any, _DummyProvider()),
        light_model="test",
    )
    tool_chain = [
        {
            "calls": [
                {
                    "name": "memorize",
                    "arguments": {"summary": "规则A"},
                    "result": "已记住（new:AbCDef12_34567890）：规则A",
                }
            ]
        }
    ]
    summaries, protected = worker._collect_explicit_memorized(tool_chain)
    assert summaries == ["规则A"]
    assert "AbCDef12_34567890" in protected


def test_extract_invalidation_topics_skips_when_token_budget_exhausted():
    provider = _DummyProvider()
    worker = PostResponseMemoryWorker(
        memorizer=cast(Any, _DummyMemorizer()),
        retriever=cast(Any, _DummyRetriever([])),
        light_provider=cast(Any, provider),
        light_model="test",
    )
    topics, remain = asyncio.run(
        worker._extract_invalidation_topics("也许这个流程不对", token_budget=0)
    )
    assert topics == []
    assert remain == 0
    assert provider.calls == 0


def test_extract_explicit_content_preferences_negative_title():
    worker = PostResponseMemoryWorker(
        memorizer=cast(Any, _DummyMemorizer()),
        retriever=cast(Any, _DummyRetriever([])),
        light_provider=cast(Any, _DummyProvider()),
        light_model="test",
    )

    items = worker._extract_explicit_content_preferences(
        "以后别再给我推《鬼灭之刃》了，我真的很讨厌这个作品。"
    )

    assert len(items) == 1
    assert items[0]["memory_type"] == "preference"
    assert "用户明确厌恶《鬼灭之刃》相关内容" in items[0]["summary"]
    assert "主动消息不要再推送、引用或拿它打比方" in items[0]["summary"]


def test_extract_explicit_content_preferences_positive_title():
    worker = PostResponseMemoryWorker(
        memorizer=cast(Any, _DummyMemorizer()),
        retriever=cast(Any, _DummyRetriever([])),
        light_provider=cast(Any, _DummyProvider()),
        light_model="test",
    )

    items = worker._extract_explicit_content_preferences(
        "我一直都很喜欢《仁王》，这类内容可以多给我发。"
    )

    assert len(items) == 1
    assert items[0]["memory_type"] == "preference"
    assert "用户明确喜欢《仁王》相关内容" in items[0]["summary"]
    assert "主动消息可优先推送相关内容" in items[0]["summary"]


def test_post_worker_saves_explicit_content_preference_even_when_llm_returns_empty():
    memorizer = _DummyMemorizer()
    retriever = _DummyRetriever([])
    worker = PostResponseMemoryWorker(
        memorizer=cast(Any, memorizer),
        retriever=cast(Any, retriever),
        light_provider=cast(Any, _DummyProvider()),
        light_model="test",
    )

    worker._handle_invalidations = AsyncMock(
        side_effect=lambda *args, **kwargs: args[-1] if args else 0
    )
    worker._extract_implicit = AsyncMock(return_value=([], 256))

    asyncio.run(
        worker.run(
            user_msg="以后别再给我推《鬼灭之刃》了，我就讨厌这个。",
            agent_response="收到。",
            tool_chain=[],
            source_ref="test@post_response",
        )
    )

    memorizer.save_item.assert_called_once()
    kwargs = memorizer.save_item.await_args.kwargs
    assert kwargs["memory_type"] == "preference"
    assert "《鬼灭之刃》" in kwargs["summary"]
