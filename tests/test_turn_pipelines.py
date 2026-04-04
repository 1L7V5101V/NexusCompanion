import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.core.history_types import ToolCall, ToolCallGroup
from agent.postturn.default_pipeline import DefaultPostTurnPipeline
from agent.postturn.protocol import PostTurnEvent, PostTurnPipeline


@pytest.mark.asyncio
async def test_default_post_turn_pipeline_uses_scheduler_post_mem_callback():
    scheduler = MagicMock()
    worker = MagicMock()
    worker.run = AsyncMock(return_value=None)
    pipeline = DefaultPostTurnPipeline(scheduler=scheduler, post_mem_worker=worker)

    event = PostTurnEvent(
        session_key="cli:1",
        channel="cli",
        chat_id="1",
        user_message="hello",
        assistant_response="ok",
        tools_used=["tool_a"],
        tool_chain=[
            ToolCallGroup(
                text="t",
                calls=[
                    ToolCall(
                        call_id="c1",
                        name="tool_a",
                        arguments={"x": 1},
                        result="done",
                    )
                ],
            )
        ],
        session=MagicMock(),
        timestamp=datetime.now(),
    )
    pipeline.schedule(event)
    scheduler.schedule_consolidation.assert_called_once()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    worker.run.assert_awaited_once()
    assert pipeline._failures == 0
