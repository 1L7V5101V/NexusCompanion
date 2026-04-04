from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.core import AgentCore, CoreRunner
from bus.events import InboundMessage
from bus.processing import ProcessingState
from bus.queue import MessageBus


@pytest.mark.asyncio
async def test_core_runner_passive_path_uses_agent_core(tmp_path: Path):
    agent_core = MagicMock(spec=AgentCore)
    agent_core.process = AsyncMock(
        return_value=cast(
            Any,
            type(
                "_CoreOutbound",
                (),
                {
                    "channel": "cli",
                    "session_key": "cli:1",
                    "content": "from-agent-core",
                    "media": [],
                    "metadata": {"thinking": "trace", "tools_used": ["noop"]},
                },
            )(),
        )
    )
    runner = CoreRunner(
        bus=MessageBus(),
        agent_core=cast(AgentCore, agent_core),
        processing_state=ProcessingState(),
    )

    msg = InboundMessage(channel="cli", sender="u", chat_id="1", content="hello")
    outbound = await runner._process_inner(msg, msg.session_key, dispatch_outbound=False)

    agent_core.process.assert_awaited_once()
    assert outbound.content == "from-agent-core"
    assert outbound.thinking == "trace"
    assert outbound.metadata["tools_used"] == ["noop"]


@pytest.mark.asyncio
async def test_core_runner_process_direct_uses_agent_core():
    agent_core = MagicMock(spec=AgentCore)
    agent_core.process = AsyncMock(
        return_value=cast(
            Any,
            type(
                "_CoreOutbound",
                (),
                {
                    "channel": "cli",
                    "session_key": "cli:direct",
                    "content": "direct-result",
                    "media": [],
                    "metadata": {},
                },
            )(),
        )
    )
    runner = CoreRunner(
        bus=MessageBus(),
        agent_core=cast(AgentCore, agent_core),
        processing_state=ProcessingState(),
    )

    result = await runner.process_direct(
        content="hello",
        session_key="cli:direct",
        channel="cli",
        chat_id="direct",
    )

    assert result == "direct-result"
    agent_core.process.assert_awaited_once()


@pytest.mark.asyncio
async def test_core_runner_spawn_completion_uses_agent_core_prompt():
    agent_core = MagicMock(spec=AgentCore)
    agent_core.process = AsyncMock(
        return_value=cast(
            Any,
            type(
                "_CoreOutbound",
                (),
                {
                    "channel": "telegram",
                    "session_key": "telegram:123",
                    "content": "spawn-result",
                    "media": [],
                    "metadata": {},
                },
            )(),
        )
    )
    runner = CoreRunner(
        bus=MessageBus(),
        agent_core=cast(AgentCore, agent_core),
        processing_state=ProcessingState(),
    )

    msg = InboundMessage(
        channel="telegram",
        sender="spawn",
        chat_id="123",
        content="[internal spawn completed]",
        metadata={
            "internal_event": "spawn_completed",
            "spawn": {
                "job_id": "job-1",
                "label": "整理任务",
                "task": "整理资料",
                "status": "incomplete",
                "exit_reason": "forced_summary",
                "result": "后台原始结果",
            },
        },
    )
    await runner._process(msg, dispatch_outbound=False)

    agent_core.process.assert_awaited_once()
    forwarded = agent_core.process.await_args.args[0]
    assert "后台任务回传" in forwarded.content
    assert forwarded.metadata["skip_post_memory"] is True
    assert forwarded.metadata["_skip_retrieval"] is True
    assert forwarded.metadata["_persist_user_content"].startswith("[后台任务完成]")
