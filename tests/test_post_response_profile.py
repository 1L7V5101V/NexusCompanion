import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from memory2.post_response_worker import PostResponseMemoryWorker


class _DummyProvider:
    async def chat(self, **kwargs):
        raise AssertionError("provider.chat should not be called in this test")


class _DummyRetriever:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    async def retrieve(self, query: str, memory_types=None, top_k=None):
        self.calls.append((query, tuple(memory_types or []), top_k))
        return list(self._results)


class _DummyMemorizer:
    def __init__(self, store=None):
        self.save_item = AsyncMock(return_value="new:testid")
        self.supersede_batch = MagicMock()
        self._store = store


def test_worker_run_does_not_call_profile_extractor_per_turn():
    """per-turn run() 不再做 profile 提取；profile 已移至 consolidation 窗口期一次性提取。"""
    memorizer = _DummyMemorizer()
    retriever = _DummyRetriever([])
    worker = PostResponseMemoryWorker(
        memorizer=cast(Any, memorizer),
        retriever=cast(Any, retriever),
        light_provider=cast(Any, _DummyProvider()),
        light_model="test",
    )
    worker._handle_invalidations = AsyncMock(side_effect=lambda *args, **kwargs: args[-1])

    asyncio.run(
        worker.run(
            user_msg="我刚买了一个新键盘",
            agent_response="记住了",
            tool_chain=[],
            source_ref="test@post_response",
        )
    )

    memorizer.save_item.assert_not_called()
