import asyncio
from unittest.mock import AsyncMock

from memory2.post_response_worker import PostResponseMemoryWorker


class _DummyProvider:
    async def chat(self, **kwargs):
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
        memorizer=memorizer,
        retriever=retriever,
        light_provider=_DummyProvider(),
        light_model="test",
    )

    worker._collect_explicit_memorized = lambda _tc: (
        ["记忆冲突时必须实时验证，禁止推测，必须外部工具验证"],
        {"2b1ba4e802bf"},
    )
    worker._handle_invalidations = AsyncMock(return_value=None)
    worker._extract_implicit = AsyncMock(
        return_value=[
            {
                "summary": "在记忆冲突情况下应优先外部工具验证，不依赖内部推测",
                "memory_type": "procedure",
                "tool_requirement": None,
                "steps": [],
            }
        ]
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
        memorizer=memorizer,
        retriever=retriever,
        light_provider=_DummyProvider(),
        light_model="test",
    )

    worker._collect_explicit_memorized = lambda _tc: (
        ["记得后续查 Steam 要走 MCP"],
        {"abcdef123456"},
    )
    worker._handle_invalidations = AsyncMock(return_value=None)
    worker._extract_implicit = AsyncMock(
        return_value=[
            {
                "summary": "回复结尾要主动追问用户最关心的点",
                "memory_type": "preference",
                "tool_requirement": None,
                "steps": [],
            }
        ]
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
