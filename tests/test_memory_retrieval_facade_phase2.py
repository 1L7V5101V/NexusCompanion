from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.core.types import HistoryMessage
from agent.looping.ports import LLMServices, MemoryConfig, MemoryServices
from agent.provider import LLMResponse
from agent.retrieval.default_pipeline import DefaultMemoryRetrievalPipeline
from agent.retrieval.protocol import RetrievalRequest
from core.memory.runtime_facade import ContextRetrievalResult


class _Provider:
    async def chat(self, **kwargs):
        return LLMResponse(content="ok", tool_calls=[])


@pytest.mark.asyncio
async def test_retrieval_pipeline_prefers_facade_retrieve_context(tmp_path: Path):
    facade = MagicMock()
    facade.retrieve_context = AsyncMock(
        return_value=ContextRetrievalResult(
            text_block="facade block",
            raw={"rag_trace": None},
        )
    )
    pipeline = DefaultMemoryRetrievalPipeline(
        memory=MemoryServices(engine=MagicMock(), facade=cast(Any, facade)),
        memory_config=MemoryConfig(),
        llm=LLMServices(provider=cast(Any, _Provider()), light_provider=cast(Any, _Provider())),
        workspace=tmp_path,
        light_model="test-light",
    )

    result = await pipeline.retrieve(
        RetrievalRequest(
            message="用户提过什么",
            session_key="cli:1",
            channel="cli",
            chat_id="1",
            history=[HistoryMessage(role="user", content="hi")],
            session_metadata={},
        )
    )

    assert result.block == "facade block"
    facade.retrieve_context.assert_awaited_once()


@pytest.mark.asyncio
async def test_retrieval_pipeline_binds_legacy_callback_into_default_facade(tmp_path: Path):
    from core.memory.default_runtime_facade import DefaultMemoryRuntimeFacade

    engine = MagicMock()
    facade = DefaultMemoryRuntimeFacade(
        port=MagicMock(),
        engine=cast(Any, engine),
        profile_maint=MagicMock(),
    )
    pipeline = DefaultMemoryRetrievalPipeline(
        memory=MemoryServices(engine=cast(Any, engine), facade=facade),
        memory_config=MemoryConfig(),
        llm=LLMServices(provider=cast(Any, _Provider()), light_provider=cast(Any, _Provider())),
        workspace=tmp_path,
        light_model="test-light",
    )

    assert facade._context_retriever is not None
    assert callable(facade._context_retriever)
    assert pipeline._memory.facade is facade
